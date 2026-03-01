#!/usr/bin/env python3
import os, json, re, time, hashlib
from datetime import datetime, timezone
import yaml, requests, feedparser
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA_OUT = os.path.join(DOCS, "data", "items.json")
TWITTER_OUT = os.path.join(DOCS, "data", "twitter_posts.json")

def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')

def resolve_youtube_channel_id(handle):
    '''
    Resolve a YouTube @handle to channel_id by parsing the About page.
    No API key required. May break if YouTube changes page structure.
    '''
    url = f"https://www.youtube.com/@{handle}/about"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        m = re.search(r'"channelId":"(UC[0-9A-Za-z_-]{22})"', r.text)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"[warn] handle {handle} -> channel_id failed: {e}")
    return None

def youtube_feed_url(channel_id):
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

def estimate_read_time(text):
    # Simple heuristic: 200 wpm
    words = len(re.findall(r'\w+', text or ""))
    return max(1, int(round(words / 200.0)))

def classify_bucket(item):
    # If we have an explicit duration in seconds, use it.
    dur = item.get("duration_sec")
    if dur is not None:
        return "highlight" if dur <= 300 else "deeper"
    # Otherwise fallback to read-time heuristic
    rt = item.get("read_minutes", 1)
    return "highlight" if rt <= 5 else "deeper"

def extract_summary(text, max_len=280):
    if not text:
        return ""
    clean = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"

def assign_themes(title, summary, themes):
    text = f"{title} {summary}".lower()
    assigned = []
    for theme, keywords in (themes or {}).items():
        for kw in (keywords or []):
            if kw.lower() in text:
                assigned.append(theme)
                break
    return sorted(set(assigned))

def normalize_entry(raw, source_label, source_type, themes_map):
    title = (raw.get("title") or "").strip()
    link = raw.get("link") or raw.get("url")
    summary = raw.get("summary") or raw.get("description") or ""
    published = raw.get("published_parsed") or raw.get("updated_parsed")
    if published:
        dt = datetime.utcfromtimestamp(time.mktime(published)).replace(tzinfo=timezone.utc)
    else:
        dt = datetime.now(tz=timezone.utc)

    duration_sec = None  # not available in RSS by default
    read_minutes = estimate_read_time(summary)
    item = {
        "id": hashlib.md5((link or title).encode("utf-8")).hexdigest(),
        "title": title,
        "url": link,
        "source": source_label,
        "source_type": source_type,
        "published": dt.isoformat(),
        "summary": extract_summary(summary),
        "read_minutes": read_minutes,
        "duration_sec": duration_sec,
        "themes": [],
    }
    item["themes"] = assign_themes(item["title"], item["summary"], themes_map)
    item["bucket"] = classify_bucket(item)
    return item

def fetch_feed(url):
    d = feedparser.parse(url)
    return d.entries or []

def fetch_twitter_posts(handles, limit_per_handle=5):
    instances = [
        "https://nitter.poast.org",
        "https://nitter.privacydev.net",
        "https://nitter.net",
    ]
    posts = []

    for tw in (handles or []):
        handle = (tw.get("handle") or "").strip().lstrip("@")
        if not handle:
            continue

        entries = []
        for base in instances:
            try:
                entries = fetch_feed(f"{base}/{handle}/rss")
                if entries:
                    break
            except Exception:
                continue

        if not entries:
            print(f"[warn] no twitter posts found for @{handle}")
            posts.append({
                "handle": handle,
                "url": f"https://x.com/{handle}",
                "posts": [],
            })
            continue

        normalized = []
        for e in entries[:limit_per_handle]:
            title = BeautifulSoup(e.get("title") or "", "html.parser").get_text(" ", strip=True)
            summary = BeautifulSoup(e.get("summary") or "", "html.parser").get_text(" ", strip=True)
            text = title or summary
            if ": " in text:
                text = text.split(": ", 1)[1]

            published = e.get("published_parsed") or e.get("updated_parsed")
            if published:
                dt = datetime.utcfromtimestamp(time.mktime(published)).replace(tzinfo=timezone.utc)
                published_iso = dt.isoformat()
            else:
                published_iso = datetime.now(tz=timezone.utc).isoformat()

            normalized.append({
                "id": hashlib.md5(((e.get("link") or "") + text).encode("utf-8")).hexdigest(),
                "url": e.get("link") or f"https://x.com/{handle}",
                "text": text,
                "published": published_iso,
            })

        posts.append({
            "handle": handle,
            "url": f"https://x.com/{handle}",
            "posts": normalized,
        })

    return posts

def main():
    feeds_cfg = load_yaml(os.path.join(ROOT, "feeds.yml")) or {}
    themes_cfg = load_yaml(os.path.join(ROOT, "themes.yml")) or {}
    themes_map = themes_cfg.get("themes", {})

    items = []
    seen = set()

    # YouTube
    for yt in (feeds_cfg.get("youtube") or []):
        channel_id = yt.get("channel_id")
        handle = yt.get("handle")
        label = yt.get("label") or (f"@{handle}" if handle else (channel_id or "YouTube"))
        if not channel_id and handle:
            channel_id = resolve_youtube_channel_id(handle)
        if not channel_id:
            print(f"[warn] skipping YouTube source (no channel_id/handle could be resolved): {yt}")
            continue
        url = youtube_feed_url(channel_id)
        entries = fetch_feed(url)
        for e in entries:
            item = normalize_entry(e, label, "youtube", themes_map)
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)

    # RSS (newsletters)
    for rs in (feeds_cfg.get("rss") or []):
        label = rs.get("label") or "Newsletter"
        url = rs.get("feed_url")
        if not url:
            continue
        entries = fetch_feed(url)
        for e in entries:
            item = normalize_entry(e, label, "rss", themes_map)
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)

    # Websites with RSS
    for ws in (feeds_cfg.get("websites") or []):
        label = ws.get("label") or "Website"
        url = ws.get("feed_url")
        if not url:
            continue
        entries = fetch_feed(url)
        for e in entries:
            item = normalize_entry(e, label, "rss", themes_map)
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            items.append(item)

    # Sort by published desc
    items.sort(key=lambda x: x["published"], reverse=True)

    os.makedirs(os.path.dirname(DATA_OUT), exist_ok=True)
    with open(DATA_OUT, "w", encoding="utf-8") as f:
        json.dump({"items": items, "generated_at": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(items)} items to {DATA_OUT}")

    twitter_posts = fetch_twitter_posts(feeds_cfg.get("twitter_embeds"), limit_per_handle=5)
    with open(TWITTER_OUT, "w", encoding="utf-8") as f:
        json.dump({"accounts": twitter_posts, "generated_at": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(twitter_posts)} twitter accounts to {TWITTER_OUT}")

if __name__ == "__main__":
    main()
