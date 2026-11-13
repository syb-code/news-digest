"""Macro news for the market overview (contract section 5.7): Federal Reserve
monetary policy press releases, BLS release feeds, BEA's current releases,
and the Macro/Fed items the daily digest already collected.

Every item keeps only an https link on its source's own host (the digest's
items: any https link), a plain-text summary of at most 300 characters, and a
UTC timestamp. Items are deduplicated by URL, newest first.
"""
import email.utils
import html as htmllib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from stocks import plain
from stocks.data import FetchError

FED_PRESS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
BLS_FEEDS = {"cpi": "https://www.bls.gov/feed/cpi.rss", "jobs": "https://www.bls.gov/feed/empsit.rss",
             "ppi": "https://www.bls.gov/feed/ppi.rss", "jolts": "https://www.bls.gov/feed/jolts.rss"}
BEA_RELEASES = "https://www.bea.gov/news/current-releases"
BEA_ORIGIN = "https://www.bea.gov"
SUMMARY_LEN = 300
TITLE_LEN = 200

_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _clean(text, limit):
    s = htmllib.unescape(_TAG_RE.sub(" ", str(text or "")))
    return plain(" ".join(s.split()), limit)


def _https_on(url, host):
    """url when it is https on host (or any host when host is None), else None."""
    u = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if host is not None and parts.hostname.lower() != host:
        return None
    if any(c in u for c in ' "<>\\') or any(ord(c) < 32 for c in u):
        return None
    return u


def _parse_xml(xml):
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    data = bytes(xml or b"")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.lstrip()
    if _DTD_RE.search(data):
        raise ValueError("the feed declares entities")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("the feed cannot be read") from None


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _find(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name):
    c = _find(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""


def _item(title, url, source, kind, published, summary=""):
    return {"title": _clean(title, TITLE_LEN), "url": url, "source": source, "kind": kind,
            "published": published, "summary": _clean(summary, SUMMARY_LEN)}


def parse_fed_rss(xml):
    """NewsItems of the Federal Reserve's monetary policy feed (RSS 2.0, links
    and dates in CDATA, dates in RFC 822 GMT, a byte order mark)."""
    root = _parse_xml(xml)
    channel = _find(root, "channel")
    out = []
    for it in (c for c in (channel if channel is not None else []) if _local(c.tag) == "item"):
        url = _https_on(_text(it, "link"), "www.federalreserve.gov")
        title = _text(it, "title")
        try:
            when = email.utils.parsedate_to_datetime(_text(it, "pubDate"))
        except (TypeError, ValueError):
            when = None
        if not url or not title or when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        desc = _text(it, "description")
        out.append(_item(title, url, "Federal Reserve", "fed", _iso(when), "" if desc == title else desc))
    return out


def _parse_time(value):
    s = str(value or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", s)
    if not m:
        return None
    tz = m.group(3) or "+00:00"
    if tz == "Z":
        tz = "+00:00"
    elif ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    try:
        return datetime.fromisoformat(m.group(1) + tz)
    except ValueError:
        return None


def parse_bls_atom(xml, kind):
    """NewsItems of one BLS release feed (Atom, despite the .rss name)."""
    if kind not in BLS_FEEDS:
        raise ValueError("not a BLS feed")
    root = _parse_xml(xml)
    out = []
    for entry in (c for c in root if _local(c.tag) == "entry"):
        link = _find(entry, "link")
        url = _https_on(link.get("href") if link is not None else "", "www.bls.gov")
        title = _text(entry, "title")
        when = _parse_time(_text(entry, "updated") or _text(entry, "published"))
        if not url or not title or when is None:
            continue
        summary = _text(entry, "content") or _text(entry, "summary")
        out.append(_item(title, url, "Bureau of Labor Statistics", "bls", _iso(when), summary))
    return out


_BEA_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_BEA_LINK_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_BEA_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"[^>]*>(.*?)</time>', re.S | re.I)
_BEA_DATE_RE = re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b")


def _ny_morning(day_text):
    """'November 4, 2026' at 08:30 New York as an aware datetime."""
    try:
        d = datetime.strptime(day_text, "%B %d, %Y").date()
    except ValueError:
        return None
    try:
        from zoneinfo import ZoneInfo

        return datetime(d.year, d.month, d.day, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        from stocks.data import trading_days

        guess = datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc)
        return datetime(d.year, d.month, d.day, 8, 30, tzinfo=timezone(trading_days._us_eastern_offset(guess)))


def parse_bea_releases(html):
    """NewsItems of BEA's current releases table (published at 08:30 New York)."""
    out = []
    for row in _BEA_ROW_RE.findall(str(html or "")):
        link = _BEA_LINK_RE.search(row)
        if not link:
            continue
        href = htmllib.unescape(link.group(1).strip())
        url = _https_on(urllib.parse.urljoin(BEA_ORIGIN + "/", href), "www.bea.gov")
        title = _TAG_RE.sub(" ", link.group(2))
        when = None
        tm = _BEA_TIME_RE.search(row)
        if tm:
            when = _parse_time(tm.group(1))
            if when is None:
                dm = _BEA_DATE_RE.search(_TAG_RE.sub(" ", tm.group(2)))
                when = _ny_morning(dm.group(1)) if dm else None
        if when is None:
            dm = _BEA_DATE_RE.search(_TAG_RE.sub(" ", row))
            when = _ny_morning(dm.group(1)) if dm else None
        if not url or not title.strip() or when is None:
            continue
        out.append(_item(title, url, "Bureau of Economic Analysis", "bea", _iso(when)))
    return out


def digest_items(path, now, theme="Macro/Fed", hours=96):
    """NewsItems from the digest's docs/data/items.json with the theme,
    published within hours before now."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return []
    lo = now - timedelta(hours=int(hours))
    hi = now + timedelta(hours=1)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        themes = it.get("themes")
        if not isinstance(themes, list) or theme not in themes:
            continue
        when = _parse_time(it.get("published"))
        url = _https_on(it.get("url"), None)
        title = it.get("title")
        if when is None or not url or not title or not (lo <= when <= hi):
            continue
        source = " ".join(str(it.get("source") or "Digest").split())[:80]
        out.append(_item(title, url, source, "digest", _iso(when), it.get("summary") or ""))
    return out


def _as_aware(now):
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    raise ValueError("now must be a datetime")


def finish(items, now, days=14, limit=30):
    """Items within days before now, deduplicated by URL, newest first, at most limit."""
    lo = _iso(now - timedelta(days=int(days)))
    hi = _iso(now + timedelta(hours=1))
    best = {}
    for it in items:
        if not (lo <= it["published"] <= hi):
            continue
        cur = best.get(it["url"])
        if cur is None or it["published"] > cur["published"]:
            best[it["url"]] = it
    out = sorted(best.values(), key=lambda x: (x["published"], x["url"]), reverse=True)
    return out[: int(limit)]


def collect(http, items_path, now, days=14, limit=30):
    """(NewsItems, {source: reason}) from every source; a failed source is
    reported and skipped."""
    now = _as_aware(now)
    items, errors = [], {}
    try:
        items += parse_fed_rss(http.get(FED_PRESS).body)
    except (FetchError, ValueError) as e:
        errors["fed"] = e.reason if isinstance(e, FetchError) else "bad data"
    for kind, url in BLS_FEEDS.items():
        try:
            items += parse_bls_atom(http.get(url).body, kind)
        except (FetchError, ValueError) as e:
            errors[f"bls_{kind}"] = e.reason if isinstance(e, FetchError) else "bad data"
    try:
        items += parse_bea_releases(http.get(BEA_RELEASES).text())
    except (FetchError, ValueError) as e:
        errors["bea"] = e.reason if isinstance(e, FetchError) else "bad data"
    if items_path:
        items += digest_items(items_path, now)
    return finish(items, now, days, limit), errors
