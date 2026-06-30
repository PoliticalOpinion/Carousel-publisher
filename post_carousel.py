#!/usr/bin/env python3
"""
post_carousel.py - Smart pre-check edition
--------------------------------------------
Flow per run:
  A) verified_ok=true → post immediately (manual override path)
  B) pending_review=true, verified_ok=false → skip (waiting for you)
  C) approved, not pending_review, not posted → web-search verify:
       - CLEAN   → post immediately, no human step needed
       - FLAGGED → open GitHub Issue, set pending_review=true, stop.
                   You review, update slides if needed, set verified_ok=true.
                   Next run posts it.

Required secrets:
  IG_ACCESS_TOKEN       long-lived Instagram token
  IG_USER_ID            Instagram-scoped user ID (17841448717123725)
  ANTHROPIC_API_KEY     for web-search verification
  MY_GITHUB_TOKEN       for opening Issues (only used when flagging)
  MY_GITHUB_REPO         e.g. "PoliticalOpinion/Carousel-publisher"
"""

import json, os, re, sys, time, subprocess, requests

API_HOST    = "https://graph.instagram.com"
API_VERSION = "v25.0"
QUEUE_PATH  = os.path.join(os.path.dirname(__file__), "carousels_queue.json")
POLL_TIMEOUT   = 90
POLL_INTERVAL  = 5


# ── helpers ──────────────────────────────────────────────────────────────────

def api_url(path):
    return f"{API_HOST}/{API_VERSION}/{path}"

def load_queue():
    with open(QUEUE_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_queue(queue):
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)

def git_commit_push(message):
    try:
        subprocess.run(["git", "add", QUEUE_PATH], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Warning: git commit/push failed: {e}")

def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── queue logic ───────────────────────────────────────────────────────────────

def find_next(queue):
    """
    Returns ("post", entry) | ("precheck", entry) | (None, None)
    verified_ok=true entries go straight to post (manual override).
    """
    precheck_candidate = None
    for entry in queue:
        if not entry.get("approved") or entry.get("posted_at"):
            continue
        if entry.get("verified_ok"):
            return "post", entry
        if entry.get("pending_review"):
            print(f"  Skipping '{entry['name']}' — awaiting your review.")
            print(f"  Issue: {entry.get('issue_url', 'check GitHub Issues')}")
            continue
        if precheck_candidate is None:
            precheck_candidate = entry

    if precheck_candidate:
        return "precheck", precheck_candidate
    return None, None


# ── verification via Claude + web search ─────────────────────────────────────

def verify_claims(name, slide_summaries):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # No API key — can't verify, flag for manual review to be safe
        return {
            "status": "needs_review",
            "summary": "ANTHROPIC_API_KEY not set — cannot auto-verify.",
            "concerns": ["Set ANTHROPIC_API_KEY secret to enable auto-verification."]
        }

    prompt = f"""You are fact-checking an Instagram carousel about {name} for a political journalism page.

Key factual claims in this carousel:
{slide_summaries}

Search the web for current news about {name} and verify whether these claims are still accurate today.

Focus ONLY on facts that could have changed since the carousel was built:
- Current office or role held
- Custody or bail status (for legal cases)
- Whether ongoing cases have concluded
- Whether the person has resigned, died, or been removed
- Election results that may have changed the situation

Do NOT flag: historical facts, editorial framing, stylistic choices, or minor wording differences.

If everything checks out, return clean. Only return needs_review if there is a genuine factual discrepancy that would make the carousel misleading to publish today.

Respond ONLY in this exact JSON format with no text outside it:
{{
  "status": "clean",
  "summary": "All key claims verified accurate as of today.",
  "concerns": []
}}

Or if genuine issues found:
{{
  "status": "needs_review",
  "summary": "One or two sentence explanation of what specifically changed.",
  "concerns": ["Specific concern 1", "Specific concern 2"]
}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        full_text = "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        )
        match = re.search(r'\{[\s\S]*?\}', full_text)
        if match:
            return json.loads(match.group())
    except Exception as e:
        # Verification failed — flag for safety rather than posting blind
        return {
            "status": "needs_review",
            "summary": f"Verification error: {e}",
            "concerns": ["Manual check required due to API error."]
        }

    return {
        "status": "needs_review",
        "summary": "Could not parse verification result.",
        "concerns": ["Manual check required."]
    }


# ── GitHub Issue (only opened when flagged) ───────────────────────────────────

def open_github_issue(entry, result):
    token = os.environ.get("MY_GITHUB_TOKEN")
    repo  = os.environ.get("MY_GITHUB_REPO")
    if not token or not repo:
        print("Warning: MY_GITHUB_TOKEN or MY_GITHUB_REPO not set — skipping Issue.")
        return None

    concerns_md = "\n".join(f"- {c}" for c in result["concerns"])

    body = f"""## ⚠️ Carousel flagged before posting

**Carousel:** {entry['name']}
**Checked:** {now_utc()}

### What the verification found
{result['summary']}

### Specific concerns
{concerns_md}

---

### Claims that were checked
{entry.get('slide_summaries', '_No slide_summaries set._')}

---

### What to do

1. Review the concerns above.
2. If slides need updating — fix and re-upload the images to the `images/` folder in the repo.
3. When the carousel is accurate and ready to post:
   - Open `carousels_queue.json` in the repo (pencil icon to edit inline)
   - Find **{entry['name']}**
   - Set `"verified_ok": true`
   - Commit directly to main
4. The next scheduled Routine run will publish it automatically.

_Note: carousels that pass verification with no concerns are posted automatically without opening an Issue._

_Opened automatically by the Carousel Publisher Routine._"""

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": f"⚠️ Needs update before posting: {entry['name']}",
            "body": body
        },
        timeout=30,
    )
    resp.raise_for_status()
    url = resp.json()["html_url"]
    print(f"GitHub Issue opened: {url}")
    return url


# ── Instagram publishing ──────────────────────────────────────────────────────

def check_rate_limit(ig_user_id, token):
    resp = requests.get(
        api_url(f"{ig_user_id}/content_publishing_limit"),
        params={"access_token": token},
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if data:
        used  = data[0].get("quota_usage", 0)
        total = data[0].get("config", {}).get("quota_total", 50)
        print(f"Publishing quota: {used}/{total}")
        if used >= total:
            raise RuntimeError("Rate limit reached — skipping this run.")

def create_child(ig_user_id, token, image_url):
    resp = requests.post(
        api_url(f"{ig_user_id}/media"),
        data={"image_url": image_url, "is_carousel_item": "true", "access_token": token},
    )
    resp.raise_for_status()
    return resp.json()["id"]

def poll(container_id, token):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = requests.get(
            api_url(container_id),
            params={"fields": "status_code", "access_token": token},
        )
        resp.raise_for_status()
        s = resp.json().get("status_code")
        if s == "FINISHED": return
        if s == "ERROR": raise RuntimeError(f"Container {container_id} failed.")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Container {container_id} timed out.")

def create_carousel_container(ig_user_id, token, children, caption):
    resp = requests.post(
        api_url(f"{ig_user_id}/media"),
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
            "access_token": token,
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]

def publish_container(ig_user_id, token, creation_id):
    resp = requests.post(
        api_url(f"{ig_user_id}/media_publish"),
        data={"creation_id": creation_id, "access_token": token},
    )
    resp.raise_for_status()
    return resp.json()["id"]

def do_post(entry, ig_user_id, token):
    if len(entry["image_urls"]) > 10:
        raise ValueError("Instagram carousels support max 10 images.")
    check_rate_limit(ig_user_id, token)
    children = []
    for url in entry["image_urls"]:
        cid = create_child(ig_user_id, token, url)
        poll(cid, token)
        children.append(cid)
    carousel_id = create_carousel_container(ig_user_id, token, children, entry["caption"])
    poll(carousel_id, token)
    return publish_container(ig_user_id, token, carousel_id)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    token      = os.environ.get("IG_ACCESS_TOKEN")
    ig_user_id = os.environ.get("IG_USER_ID")
    if not token or not ig_user_id:
        print("Missing IG_ACCESS_TOKEN or IG_USER_ID.")
        return 1

    queue = load_queue()
    action, entry = find_next(queue)

    if action is None:
        print("Nothing to do.")
        return 0

    # ── manual override: post directly ───────────────────────────────────────
    if action == "post":
        print(f"Publishing (manually approved): {entry['name']}")
        media_id           = do_post(entry, ig_user_id, token)
        entry["posted_at"] = now_utc()
        entry["media_id"]  = media_id
        save_queue(queue)
        git_commit_push(f"Posted: {entry['name']} ({media_id})")
        print(f"Done. media_id={media_id}")
        return 0

    # ── auto pre-check ────────────────────────────────────────────────────────
    if action == "precheck":
        print(f"Verifying: {entry['name']}")
        result = verify_claims(entry["name"], entry.get("slide_summaries", ""))
        print(f"Result: {result['status']}")

        if result["status"] == "clean":
            # No issues found — post immediately, no human step
            print("Clean. Posting now.")
            media_id           = do_post(entry, ig_user_id, token)
            entry["posted_at"] = now_utc()
            entry["media_id"]  = media_id
            entry["auto_verified_at"] = now_utc()
            save_queue(queue)
            git_commit_push(f"Auto-verified and posted: {entry['name']} ({media_id})")
            print(f"Done. media_id={media_id}")
        else:
            # Issues found — flag for your review, do not post
            print("Issues found. Opening GitHub Issue for your review.")
            issue_url              = open_github_issue(entry, result)
            entry["pending_review"] = True
            entry["flag_reason"]    = result["summary"]
            entry["concerns"]       = result.get("concerns", [])
            if issue_url:
                entry["issue_url"] = issue_url
            save_queue(queue)
            git_commit_push(f"Flagged for review: {entry['name']}")
            print("Stopped. Fix the carousel, set verified_ok=true, next run will post.")

        return 0


if __name__ == "__main__":
    sys.exit(main())
