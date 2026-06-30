#!/usr/bin/env python3
"""
post_carousel.py - Free manual-review edition (no Anthropic API key required)
--------------------------------------------------------------------------------
Flow per run:
  A) verified_ok=true → post immediately (this is how you approve, after review)
  B) pending_review=true, verified_ok=false → skip (waiting for you)
  C) approved, not pending_review, not posted → fetch recent headlines via
     Google News RSS (free, no key), open a GitHub Issue showing the original
     carousel claims next to the fresh headlines, set pending_review=true, stop.

     You read the Issue, compare headlines to claims yourself, decide.
     If fine: edit carousels_queue.json → set "verified_ok": true → commit.
     Next run posts it automatically.

Note: every approved carousel goes through human review under this version —
there is no automated "clean, skip the human" path, since there's no AI
judgment step. That's the tradeoff for not using a paid API.

Required secrets:
  IG_ACCESS_TOKEN       long-lived Instagram token
  IG_USER_ID            Instagram-scoped user ID (17841448717123725)
  MY_GITHUB_TOKEN       for opening Issues
  MY_GITHUB_REPO        e.g. "PoliticalOpinion/Carousel-publisher"
"""

import json, os, sys, time, subprocess, requests
import xml.etree.ElementTree as ET

API_HOST    = "https://graph.instagram.com"
API_VERSION = "v25.0"
QUEUE_PATH  = os.path.join(os.path.dirname(__file__), "carousels_queue.json")
POLL_TIMEOUT   = 90
POLL_INTERVAL  = 5
MAX_HEADLINES  = 5


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


# ── free headline fetch via Google News RSS (no API key) ─────────────────────

def fetch_headlines(name, max_results=MAX_HEADLINES):
    """
    Free, no-key headline fetch. Returns a list of dicts:
    [{"title": ..., "link": ..., "pubdate": ...}, ...]
    On failure, returns a single entry explaining the failure so the
    GitHub Issue still gets opened with useful context.
    """
    query = f"{name} India politics"
    try:
        resp = requests.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:max_results]
        headlines = []
        for item in items:
            title_el   = item.find("title")
            link_el    = item.find("link")
            pubdate_el = item.find("pubDate")
            headlines.append({
                "title":   title_el.text   if title_el   is not None else "(no title)",
                "link":    link_el.text    if link_el    is not None else "",
                "pubdate": pubdate_el.text if pubdate_el is not None else "",
            })
        return headlines
    except Exception as e:
        return [{
            "title": f"Headline fetch failed ({e}) — check manually before posting.",
            "link": "", "pubdate": ""
        }]


# ── GitHub Issue ──────────────────────────────────────────────────────────────

def open_review_issue(entry, headlines):
    token = os.environ.get("MY_GITHUB_TOKEN")
    repo  = os.environ.get("MY_GITHUB_REPO")
    if not token or not repo:
        print("Warning: MY_GITHUB_TOKEN or MY_GITHUB_REPO not set — skipping Issue.")
        return None

    if headlines:
        headlines_md = "\n".join(
            f"- [{h['title']}]({h['link']})" + (f" — _{h['pubdate']}_" if h['pubdate'] else "")
            if h["link"] else f"- {h['title']}"
            for h in headlines
        )
    else:
        headlines_md = "_No recent headlines found._"

    body = f"""## Review before posting

**Carousel:** {entry['name']}
**Checked:** {now_utc()}

### What the carousel claims
{entry.get('slide_summaries', '_No slide_summaries set._')}

### Recent headlines (read these and compare against the claims above)
{headlines_md}

---

### What to do

1. Skim the headlines above. Look specifically for anything that contradicts
   the claims — a resignation, a new conviction, a custody change, an election
   result reversal, anything that means the carousel would be misleading if
   posted as-is today.
2. If something's changed — update the carousel images and re-upload them to
   the `images/` folder before approving.
3. If everything still holds up:
   - Open `carousels_queue.json` in the repo (pencil icon to edit inline)
   - Find **{entry['name']}**
   - Set `"verified_ok": true`
   - Commit directly to main
4. The next scheduled Routine run will publish it automatically.

_This page does not use an AI judgment step — you're reading these headlines
and deciding, not an automated check. Opened automatically by the Carousel
Publisher Routine._"""

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": f"Review before posting: {entry['name']}", "body": body},
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

    if action == "post":
        print(f"Publishing (manually approved): {entry['name']}")
        media_id           = do_post(entry, ig_user_id, token)
        entry["posted_at"] = now_utc()
        entry["media_id"]  = media_id
        save_queue(queue)
        git_commit_push(f"Posted: {entry['name']} ({media_id})")
        print(f"Done. media_id={media_id}")
        return 0

    if action == "precheck":
        print(f"Fetching headlines for: {entry['name']}")
        headlines = fetch_headlines(entry["name"])
        issue_url = open_review_issue(entry, headlines)

        entry["pending_review"] = True
        if issue_url:
            entry["issue_url"] = issue_url
        save_queue(queue)
        git_commit_push(f"Opened review for: {entry['name']}")
        print("Stopped. Review the headlines in the Issue, then set verified_ok=true.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
