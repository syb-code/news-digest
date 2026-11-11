#!/usr/bin/env python3
"""Fetch the feeds listed in config.yml and write the dashboard data.

Outputs:
  docs/data/items.json          items, plus the status of every source
  docs/data/twitter_posts.json  X accounts (posts stay empty unless
                                feeds.nitter_instances is set in config.yml)
  data/youtube_channels.json    cache of YouTube @handle -> channel_id

Every item carries "first_seen", the time a run first listed it. It is copied
from the previous items.json, so it stays the same from run to run; an item
from a file written before first_seen existed gets its published time.

Each file is rewritten only when its content, apart from generated_at, changed,
so a run that finds nothing new leaves the working tree clean and the workflow
commits nothing.

Feeds are untrusted input. They are fetched with requests (timeouts, a size cap,
one retry), parsed from memory, and only http(s) links are kept. Each source has
a wall-clock limit that covers its fetch and its parse, and the run has one for
all sources together, so a slow or hostile host fails its own source only.
"""
import calendar
import contextlib
import hashlib
import html
import io
import json
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_YML = os.path.join(ROOT, "config.yml")
DATA_OUT = os.path.join(ROOT, "docs", "data", "items.json")
TWITTER_OUT = os.path.join(ROOT, "docs", "data", "twitter_posts.json")
CHANNEL_CACHE = os.path.join(ROOT, "data", "youtube_channels.json")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = (10, 20)            # connect, read (seconds); the read timeout applies to each recv
MAX_FETCH_SECONDS = 45        # body deadline, checked only between 64 KB chunks: a host that drips
                              # bytes can outlast it, so SOURCE_SECONDS is the limit that holds
SOURCE_SECONDS = 60           # wall clock for one source: fetch, retry, parse and normalize
RUN_SECONDS = 360             # wall clock for all sources; the build job's timeout-minutes is 12
RUN_SECONDS_ENV = "FEED_RUN_SECONDS"  # a shorter wall clock for one run: daily.yml sets it on the
MIN_RUN_SECONDS = 30                  # morning run, which has the email to send by 6:00
MAX_BYTES = 15 * 1024 * 1024  # a feed bigger than this is refused
RETRY_DELAY = 2.0             # seconds before the single retry

MAX_ITEMS_PER_SOURCE = 50
SUMMARY_LEN = 280
WORDS_PER_MINUTE = 200
QUICK_READ_MINUTES = 5        # "highlight" means about this long or less

CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
YT_HANDLE_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
X_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
CHANNEL_PAGE_PATTERNS = (
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'"externalId":"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{22})"'),
)
TAG_RE = re.compile(r"<[^<>]*>")  # [^<>], not [^>]: linear on a run of unclosed "<"
WORD_RE = re.compile(r"\w+")
SPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- utilities

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def md5_hex(text):
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def load_json(path):
    """The parsed file, or None when it is missing or not valid JSON."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"[warn] could not read {os.path.relpath(path, ROOT)}: {e}")
        return None


def write_json(path, doc):
    """Write atomically, so a crash never leaves half a file behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def write_if_changed(path, content, now):
    """Write {"generated_at": now, **content} unless every key of content is
    already equal in the file. Returns True when the file was written."""
    content = json.loads(json.dumps(content))  # same types as a reloaded file
    old = load_json(path)
    rel = os.path.relpath(path, ROOT)
    if isinstance(old, dict) and all(old.get(k) == v for k, v in content.items()):
        print(f"No change in {rel}; left as is.")
        return False
    write_json(path, {"generated_at": iso(now), **content})
    print(f"Wrote {rel}")
    return True


def web_url(link):
    """The link if it is an absolute http(s) URL, else None."""
    s = str(link or "").strip()
    if not s:
        return None
    try:
        parsed = urlparse(s)
    except ValueError:
        return None
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return s
    return None


def clean_text(value):
    return SPACE_RE.sub(" ", str(value or "")).strip()


# ---------------------------------------------------------------- fetching

class FetchError(Exception):
    """A failed fetch. `reason` is short and safe to publish in items.json;
    `detail` is the longer text for the log."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or reason


class Fetched:
    def __init__(self, status, url, content_type, body):
        self.status = status
        self.url = url
        self.content_type = content_type
        self.body = body


def new_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


@contextlib.contextmanager
def time_limit(seconds):
    """Raise FetchError("timeout") in the block once `seconds` of wall-clock time
    have passed, wherever it is: a socket read, feedparser or a regex. It uses
    SIGALRM, so it holds only in the main thread of a platform with setitimer;
    elsewhere the block runs with the per-request timeouts alone."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return
    armed = True

    def on_alarm(signum, frame):
        if armed:
            raise FetchError("timeout", f"not finished within {seconds:.1f} s")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    # The timer fires again every second, in case code in the block catches the first one.
    signal.setitimer(signal.ITIMER_REAL, seconds, 1.0)
    try:
        yield
    finally:
        armed = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run_seconds(environ=None):
    """The wall clock for this run: RUN_SECONDS, or the whole number of seconds
    in FEED_RUN_SECONDS when that is between MIN_RUN_SECONDS and RUN_SECONDS.
    The variable can shorten a run, never lengthen it past the job's timeout."""
    environ = os.environ if environ is None else environ
    given = str(environ.get(RUN_SECONDS_ENV) or "").strip()
    if not given:
        return RUN_SECONDS
    if given.isdigit() and MIN_RUN_SECONDS <= int(given) <= RUN_SECONDS:
        return int(given)
    print(f"[warn] {RUN_SECONDS_ENV} must be a whole number from {MIN_RUN_SECONDS} to {RUN_SECONDS}; "
          f"using {RUN_SECONDS}")
    return RUN_SECONDS


class RunClock:
    """The run's wall-clock budget, handed out one source at a time."""

    def __init__(self, seconds=None):
        self.seconds = RUN_SECONDS if seconds is None else seconds
        self.deadline = time.monotonic() + self.seconds

    def limit(self):
        """A time_limit of SOURCE_SECONDS, or of what is left of the run when
        that is less. Raises FetchError once the run budget is spent."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise FetchError("skipped, out of time", f"the run's {self.seconds} s budget is spent")
        return time_limit(min(SOURCE_SECONDS, left))


def _read_body(resp):
    start = time.monotonic()
    chunks, size = [], 0
    for chunk in resp.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > MAX_BYTES:
            raise FetchError("response too large", f"response larger than {MAX_BYTES} bytes")
        if time.monotonic() - start > MAX_FETCH_SECONDS:
            raise FetchError("timeout", f"body not received within {MAX_FETCH_SECONDS} s")
        chunks.append(chunk)
    return b"".join(chunks)


def http_get(session, url, label=None):
    """GET url with a timeout, a size cap and one retry on a connection error
    or a 5xx. The body is read only for a 2xx. Raises FetchError."""
    label = label or url
    for attempt in (1, 2):
        last = attempt == 2
        try:
            resp = session.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            try:
                status = resp.status_code
                if status >= 500 and not last:
                    print(f"[warn] {label}: HTTP {status}, retrying once")
                    time.sleep(RETRY_DELAY)
                    continue
                body = _read_body(resp) if 200 <= status < 300 else b""
                return Fetched(status, resp.url or url, resp.headers.get("content-type", ""), body)
            finally:
                resp.close()
        except requests.ConnectionError as e:  # includes connect timeouts
            if not last:
                print(f"[warn] {label}: {type(e).__name__}, retrying once")
                time.sleep(RETRY_DELAY)
                continue
            raise FetchError("connection error", f"{type(e).__name__}: {e}") from e
        except requests.Timeout as e:
            raise FetchError("timeout", f"{type(e).__name__}: {e}") from e
        except requests.RequestException as e:
            raise FetchError(type(e).__name__, f"{type(e).__name__}: {e}") from e
    raise AssertionError("unreachable")


def fetch_feed(session, label, url):
    """Return (entries, error). error is None on success, else a short reason."""
    try:
        res = http_get(session, url, label)
    except FetchError as e:
        print(f"[warn] {label}: {e.detail}")
        return [], e.reason
    if not 200 <= res.status < 300:
        print(f"[warn] {label}: HTTP {res.status}")
        return [], f"HTTP {res.status}"
    # Parse from memory. A bare bytes argument would make feedparser try it as a
    # local file path first, so it is wrapped in BytesIO.
    parsed = feedparser.parse(
        io.BytesIO(res.body),
        response_headers={"content-location": res.url, "content-type": res.content_type},
    )
    entries = list(parsed.get("entries") or [])
    if not entries:
        exc = parsed.get("bozo_exception")
        detail = f" ({type(exc).__name__}: {exc})" if exc else ""
        if not parsed.get("version"):  # feedparser found no RSS or Atom at all
            print(f"[warn] {label}: HTTP {res.status} / not a feed{detail}")
            return [], "not a feed"
        print(f"[warn] {label}: HTTP {res.status} / no entries{detail}")
    return entries, None


# ---------------------------------------------------------------- YouTube

def youtube_feed_url(channel_id):
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def resolve_youtube_channel_id(session, handle):
    """Find a handle's channel_id on its channel page. No API key needed; this
    can break when YouTube changes the page, which is why results are cached."""
    url = f"https://www.youtube.com/@{quote(handle, safe='')}/about"
    try:
        res = http_get(session, url, f"@{handle} channel page")
    except FetchError as e:
        print(f"[warn] @{handle}: channel page: {e.detail}")
        return None
    if not 200 <= res.status < 300:
        print(f"[warn] @{handle}: channel page: HTTP {res.status}")
        return None
    text = res.body.decode("utf-8", errors="replace")
    for pattern in CHANNEL_PAGE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    print(f"[warn] @{handle}: no channel id on the channel page")
    return None


def load_channel_cache():
    data = load_json(CHANNEL_CACHE)
    if not isinstance(data, dict):
        return {}
    return {str(h): v for h, v in data.items() if isinstance(v, str) and CHANNEL_ID_RE.fullmatch(v)}


# ---------------------------------------------------------------- themes and buckets

def _theme_pattern(alternatives, flags=0):
    alternatives = sorted(alternatives, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")s?(?!\w)", flags)


def compile_themes(themes_map):
    """Precompile one matcher per theme: whole words, an optional plural s.
    A keyword written in ALL CAPS (like COIN) matches case-sensitively, every
    other keyword case-insensitively."""
    compiled = []
    for theme, keywords in (themes_map or {}).items():
        if isinstance(keywords, str):
            keywords = [keywords]
        insensitive, sensitive = [], []
        for kw in keywords or []:
            kw = clean_text(kw)
            if not kw:
                continue
            alt = r"\s+".join(re.escape(word) for word in kw.split(" "))
            (sensitive if kw.isupper() else insensitive).append(alt)
        patterns = []
        if insensitive:
            patterns.append(_theme_pattern(insensitive, re.IGNORECASE))
        if sensitive:
            patterns.append(_theme_pattern(sensitive))
        if patterns:
            compiled.append((str(theme), patterns))
    return compiled


def assign_themes(text, compiled):
    return sorted({theme for theme, patterns in compiled if any(p.search(text) for p in patterns)})


def estimate_read_time(text):
    """Minutes at WORDS_PER_MINUTE, counted on the text without its HTML tags."""
    plain = html.unescape(TAG_RE.sub(" ", text or ""))
    words = len(WORD_RE.findall(plain))
    return max(1, int(round(words / float(WORDS_PER_MINUTE))))


def classify_bucket(source_type, url, read_minutes):
    """"highlight" is a quick read (about 5 minutes or less), "deeper" a long
    read or a video. YouTube Shorts count as quick."""
    if source_type == "youtube":
        return "highlight" if "/shorts/" in urlparse(url or "").path else "deeper"
    return "deeper" if (read_minutes or 0) > QUICK_READ_MINUTES else "highlight"


def extract_summary(text, max_len=SUMMARY_LEN):
    if not text:
        return ""
    clean = clean_text(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
    if len(clean) <= max_len:
        return clean
    cut = clean[: max_len - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "..."


# ---------------------------------------------------------------- items

def entry_published(raw):
    st = raw.get("published_parsed") or raw.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def entry_body(raw):
    """The full post when the feed carries it (content:encoded), else the summary."""
    for content in raw.get("content") or []:
        value = content.get("value") if hasattr(content, "get") else None
        if value:
            return value
    return raw.get("summary") or raw.get("description") or ""


def stored_time(prev):
    """The published time of an item from the previous run, or None."""
    return parse_iso(prev.get("published")) if prev else None


def first_seen_time(prev, published, now):
    """When an item was first listed: the previous file's first_seen, else its
    published time when the previous file has the item without one (a file
    from before first_seen existed), else now. Never later than now."""
    if prev:
        seen = parse_iso(prev.get("first_seen"))
        if seen is None:
            seen = stored_time(prev) or published
        return min(seen, now)
    return now


def item_id(url, raw, label, title):
    if url:
        return md5_hex(url)
    # No usable link: hash what identifies the entry, so link-less items from
    # different entries never share an id or collapse at dedupe.
    return md5_hex("\n".join([label, str(raw.get("id") or ""), str(raw.get("link") or ""), title]))


def normalize_entry(raw, label, source_type, themes, prev_by_id, now):
    """One feed entry as an item dict, or None when it has no title and no link."""
    url = web_url(raw.get("link") or raw.get("url"))
    title = clean_text(raw.get("title"))
    if not title and not url:
        return None
    title = title or "Untitled"
    iid = item_id(url, raw, label, title)
    prev = prev_by_id.get(iid)
    published = entry_published(raw)
    if published is None:
        # Keep the made-up time stable, or every run would change the file.
        published = stored_time(prev) or now
    summary = extract_summary(raw.get("summary") or raw.get("description") or entry_body(raw))
    read_minutes = estimate_read_time(entry_body(raw))
    return {
        "id": iid,
        "title": title,
        "url": url,
        "source": label,
        "source_type": source_type,
        "published": iso(published),
        "first_seen": iso(first_seen_time(prev, published, now)),
        "summary": summary,
        "read_minutes": read_minutes,
        "duration_sec": None,  # feeds do not carry it
        "themes": assign_themes(f"{title} {summary}", themes),
        "bucket": classify_bucket(source_type, url, read_minutes),
    }


def fresh_items(entries, src, themes, prev_by_id, now):
    """The source's entries as items, plus the ids of those whose time was made
    up this run (an undated entry seen for the first time gets now). Feeds list
    newest first, so of the undated entries only the first MAX_ITEMS_PER_SOURCE
    in feed order are kept: the same set every run."""
    items, guessed, undated = [], set(), 0
    for raw in entries:
        item = normalize_entry(raw, src.label, src.type, themes, prev_by_id, now)
        if not item:
            continue
        if entry_published(raw) is None:
            undated += 1
            if undated > MAX_ITEMS_PER_SOURCE:
                continue
            if stored_time(prev_by_id.get(item["id"])) is None:
                guessed.add(item["id"])
        items.append(item)
    return items, guessed


def carry_over(prev_items, source_type, themes, now=None):
    """A failed source's previous items, marked stale. They stay until the source
    works again or is removed from config.yml; meanwhile the source is listed with
    its error in "sources", on the dashboard and in the email footer."""
    now = now or utc_now()
    out = []
    for it in prev_items:
        published = parse_iso(it.get("published"))
        if not it.get("id") or not it.get("title") or published is None:
            continue
        url = web_url(it.get("url"))
        try:
            read_minutes = max(1, int(it.get("read_minutes") or 1))
        except (TypeError, ValueError):
            read_minutes = 1
        title = clean_text(it.get("title"))
        summary = clean_text(it.get("summary"))
        stype = it.get("source_type") if it.get("source_type") in ("youtube", "rss") else source_type
        duration = it.get("duration_sec")
        out.append({
            "id": str(it["id"]),
            "title": title,
            "url": url,
            "source": str(it.get("source") or ""),
            "source_type": stype,
            "published": iso(published),
            "first_seen": iso(first_seen_time(it, published, now)),
            "summary": summary,
            "read_minutes": read_minutes,
            "duration_sec": duration if isinstance(duration, int) else None,
            "themes": assign_themes(f"{title} {summary}", themes),
            "bucket": classify_bucket(stype, url, read_minutes),
            "stale": True,
        })
    return out


def dedupe_key(item):
    return item["url"] or item["id"]


# ---------------------------------------------------------------- sources

class Source:
    def __init__(self, label, source_type, url=None, error=None, cached_handle=None):
        self.label = label
        self.type = source_type
        self.url = url
        self.error = error
        self.cached_handle = cached_handle  # set when the channel_id came from the cache
        self.drop_from_cache = False


def youtube_sources(entries, cache, session, clock=None):
    """Yield a Source per YouTube entry. channel_id in config.yml wins; else the
    handle is looked up in the cache, and only a missing handle is scraped."""
    clock = clock or RunClock()
    for yt in entries or []:
        if not isinstance(yt, dict):
            print(f"[warn] skipping YouTube entry that is not a mapping: {yt!r}")
            continue
        channel_id = clean_text(yt.get("channel_id"))
        handle = clean_text(yt.get("handle")).lstrip("@")
        label = clean_text(yt.get("label")) or (f"@{handle}" if handle else channel_id) or "YouTube"
        if channel_id:
            if CHANNEL_ID_RE.fullmatch(channel_id):
                yield Source(label, "youtube", youtube_feed_url(channel_id))
            else:
                print(f"[warn] {label}: channel_id {channel_id!r} is not a YouTube channel id")
                yield Source(label, "youtube", error="invalid channel_id")
            continue
        if not handle:
            print(f"[warn] skipping YouTube entry with no handle or channel_id: {yt!r}")
            continue
        if not YT_HANDLE_RE.fullmatch(handle):
            print(f"[warn] {label}: {handle!r} is not a YouTube handle")
            yield Source(label, "youtube", error="invalid handle")
            continue
        if handle in cache:
            yield Source(label, "youtube", youtube_feed_url(cache[handle]), cached_handle=handle)
            continue
        try:
            with clock.limit():
                channel_id = resolve_youtube_channel_id(session, handle)
        except FetchError as e:
            print(f"[warn] @{handle}: channel page: {e.detail}")
            channel_id = None
        if channel_id:
            cache[handle] = channel_id
            yield Source(label, "youtube", youtube_feed_url(channel_id))
        else:
            yield Source(label, "youtube", error="handle not resolved")


def feed_sources(entries, default_label):
    for rs in entries or []:
        if not isinstance(rs, dict):
            print(f"[warn] skipping feed entry that is not a mapping: {rs!r}")
            continue
        label = clean_text(rs.get("label")) or default_label
        url = clean_text(rs.get("feed_url"))
        if not url:
            print(f"[warn] {label}: no feed_url, skipped")
            continue
        if not web_url(url):
            print(f"[warn] {label}: feed_url is not an http(s) URL")
            yield Source(label, "rss", error="invalid feed_url")
            continue
        yield Source(label, "rss", url)


def make_labels_unique(sources):
    """Items carry their source's label, and a failed source's earlier items are
    found by it, so two sources with one label would mix their items. A label
    that repeats gets the feed host added, then a number if it still repeats."""
    counts = {}
    for src in sources:
        counts[src.label] = counts.get(src.label, 0) + 1
    taken = {label for label, n in counts.items() if n == 1}
    for src in sources:
        if counts[src.label] == 1:
            continue
        host = (urlparse(src.url).hostname or "").removeprefix("www.") if src.url else ""
        base = f"{src.label} ({host})" if host else src.label
        label, n = base, 1
        while label in taken:
            n += 1
            label = f"{base} {n}"
        taken.add(label)
        print(f"[warn] more than one source is labeled {src.label!r}; this one ({src.url or src.error}) is shown as {label!r}")
        src.label = label
    return sources


# ---------------------------------------------------------------- X (Twitter)

def normalize_twitter_url(url, handle):
    fallback = f"https://x.com/{handle}"
    try:
        path = urlparse(url or "").path or ""
    except ValueError:
        return fallback
    if re.fullmatch(r"/[A-Za-z0-9_]{1,15}/status/[0-9]+/?", path):
        return f"https://x.com{path}"
    return fallback


def fetch_twitter_posts(session, handles, instances, limit_per_handle=5, clock=None):
    """One account per handle. Posts are read from the configured Nitter
    instances only; with none configured, no request is made."""
    clock = clock or RunClock()
    accounts = []
    for tw in handles or []:
        raw_handle = tw.get("handle") if isinstance(tw, dict) else tw
        handle = clean_text(raw_handle).lstrip("@")
        if not X_HANDLE_RE.fullmatch(handle):
            if handle:
                print(f"[warn] skipping X handle {handle!r}: not a valid handle")
            continue
        posts = []
        for base in instances:
            try:
                with clock.limit():
                    entries, _ = fetch_feed(session, f"@{handle} via {base}", f"{base}/{handle}/rss")
                    for e in entries[:limit_per_handle]:
                        title = BeautifulSoup(e.get("title") or "", "html.parser").get_text(" ", strip=True)
                        summary = BeautifulSoup(e.get("summary") or "", "html.parser").get_text(" ", strip=True)
                        text = title or summary
                        if ": " in text:
                            text = text.split(": ", 1)[1]
                        published = entry_published(e) or utc_now()
                        posts.append({
                            "id": md5_hex((e.get("link") or "") + text),
                            "url": normalize_twitter_url(e.get("link"), handle),
                            "text": text,
                            "published": iso(published),
                        })
            except FetchError as e:
                print(f"[warn] @{handle} via {base}: {e.detail}")
                posts = []
            if posts:
                break
        accounts.append({"handle": handle, "url": f"https://x.com/{handle}", "posts": posts})
    return accounts


def nitter_instances(feeds_cfg):
    configured = feeds_cfg.get("nitter_instances") or []
    if isinstance(configured, str):
        configured = [configured]
    out = []
    for base in configured:
        url = web_url(base)
        if url and urlparse(url).scheme.lower() == "https":
            out.append(url.rstrip("/"))
        else:
            print(f"[warn] ignoring nitter instance {base!r}: use an https:// URL")
    return out


# ---------------------------------------------------------------- main

def build_items(sources, session, themes, prev_items, now, clock=None):
    clock = clock or RunClock()
    prev_by_source, prev_by_id = {}, {}
    for it in prev_items:
        prev_by_source.setdefault(it.get("source"), []).append(it)
        prev_by_id[it.get("id")] = it

    items, status, seen = [], [], set()
    for src in sources:
        previous = prev_by_source.get(src.label, [])
        error, fresh, guessed = src.error, [], set()
        if error is None:
            try:
                with clock.limit():  # a slow or hostile source fails alone
                    entries, error = fetch_feed(session, src.label, src.url)
                    if error is None and not entries and previous:
                        error = "no entries"
                    if error is None:
                        fresh, guessed = fresh_items(entries, src, themes, prev_by_id, now)
            except FetchError as e:  # the source's time limit, or the run's
                print(f"[warn] {src.label}: {e.detail}")
                error, fresh = e.reason, []
            except Exception as e:  # one malformed feed must not stop the run
                print(f"[warn] {src.label}: could not read entries: {type(e).__name__}: {e}")
                error, fresh = "parse error", []

        if error is not None:
            if src.cached_handle and error == "HTTP 404":
                src.drop_from_cache = True
            source_items = carry_over(previous, src.type, themes, now)[:MAX_ITEMS_PER_SOURCE]
            if source_items:
                print(f"[warn] {src.label}: kept {len(source_items)} earlier items, marked stale")
        else:
            # A time made up this run must not push out a stored item, so those
            # entries only fill the slots that are left, in feed order.
            known = [i for i in fresh if i["id"] not in guessed]
            known.sort(key=lambda i: (i["published"], i["id"]), reverse=True)
            source_items = (known + [i for i in fresh if i["id"] in guessed])[:MAX_ITEMS_PER_SOURCE]

        added = 0
        for item in source_items:
            key = dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            added += 1
        status.append({"label": src.label, "type": src.type, "ok": error is None, "count": added, "error": error})

    items.sort(key=lambda i: (parse_iso(i["published"]), i["id"]), reverse=True)
    return items, status


def main(session=None):
    now = utc_now()
    try:
        # Only the sections this script uses: a mistake elsewhere (learn, say)
        # must not stop the feeds.
        cfg = config.load_config(CONFIG_YML, check=("feeds", "themes"))
    except config.ConfigError as e:
        raise SystemExit(str(e)) from None
    session = session or new_session()
    feeds_cfg = cfg["feeds"]
    themes = compile_themes(cfg["themes"])

    prev_doc = load_json(DATA_OUT)
    prev_items = [i for i in ((prev_doc or {}).get("items") or []) if isinstance(i, dict)]

    clock = RunClock(run_seconds())
    cache = load_channel_cache()
    sources = list(youtube_sources(feeds_cfg.get("youtube"), cache, session, clock))
    sources += list(feed_sources(feeds_cfg.get("rss"), "Newsletter"))
    sources += list(feed_sources(feeds_cfg.get("websites"), "Website"))
    make_labels_unique(sources)

    items, status = build_items(sources, session, themes, prev_items, now, clock)

    # A cached channel_id whose feed is gone (404) is resolved again next run.
    for src in sources:
        if src.drop_from_cache:
            cache.pop(src.cached_handle, None)
    configured = {clean_text(y.get("handle")).lstrip("@") for y in feeds_cfg.get("youtube") or [] if isinstance(y, dict)}
    cache = {h: cache[h] for h in sorted(cache) if h in configured}
    if load_json(CHANNEL_CACHE) != cache:
        write_json(CHANNEL_CACHE, cache)
        print(f"Wrote {os.path.relpath(CHANNEL_CACHE, ROOT)}")

    failed = [s["label"] for s in status if not s["ok"]]
    stale = sum(1 for i in items if i.get("stale"))
    print(f"{len(items)} items from {len(status)} sources ({len(failed)} failed, {stale} stale items kept)")
    if failed:
        print("Failed sources: " + "; ".join(f"{s['label']} ({s['error']})" for s in status if not s["ok"]))
    write_if_changed(DATA_OUT, {"items": items, "sources": status}, now)

    accounts = fetch_twitter_posts(session, feeds_cfg.get("twitter_embeds"), nitter_instances(feeds_cfg), clock=clock)
    write_if_changed(TWITTER_OUT, {"accounts": accounts}, now)


if __name__ == "__main__":
    main()
