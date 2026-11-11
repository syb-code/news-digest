"""Shared pieces of the learning briefing (scripts/learn/*.py).

The briefing is built in three steps, each its own job in the daily workflow:

  candidates.py  no secrets. Reads config.yml, docs/data/items.json, the lane
                 sources in learn/lanes.md and learn/seen.json, and writes the
                 ranked candidate topics as JSON.
  brief.py       the only step with ANTHROPIC_API_KEY. Turns the candidates into
                 briefing.json with one Claude call, or writes an extractive
                 briefing without a model when there is no key or the call fails.
  render.py      no secrets. Writes the slide deck, the dated folder under
                 docs/learn/ and docs/learn/index.json, and logs the day's
                 topics in learn/seen.json.

This module uses the Python standard library only, because brief.py runs in a
job that installs nothing but the anthropic SDK (and PyYAML, for config.yml).
"""
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, "scripts")
LANES_MD = os.path.join(ROOT, "learn", "lanes.md")
SEEN_JSON = os.path.join(ROOT, "learn", "seen.json")
ITEMS_JSON = os.path.join(ROOT, "docs", "data", "items.json")
DOCS_DIR = os.path.join(ROOT, "docs")

DEFAULT_TIMEZONE = "America/Los_Angeles"
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The learn block of config.yml (contract 1), used for any key config.yml
# leaves out, or for all of them when config.yml cannot be read.
LEARN_DEFAULTS = {
    "model": "claude-opus-5-5",
    "effort": "medium",
    "max_output_tokens": 12000,
    "max_input_chars": 40000,
    "topics_min": 3,
    "topics_max": 5,
    "lookback_hours": 36,
    "price_per_mtok": {"input": 4.0, "output": 20.0},
    # Dollars per million tokens by model id, so an answer written by a fallback
    # model is priced at that model's rates. A model that is not listed is
    # priced at price_per_mtok and its cost is marked as an estimate.
    "prices": {
        "claude-opus-5-5": {"input": 4.0, "output": 20.0},
        "claude-opus-5": {"input": 5.0, "output": 25.0},
        "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    },
    "fallbacks": "default",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,63}$")
SPACE_RE = re.compile(r"\s+")


def log(message):
    print(message, flush=True)


def warn(message):
    log(f"[warn] {message}")


# ---------------------------------------------------------------- time

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    """A timezone-aware UTC datetime, or None."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def zone(name):
    """The named time zone. Falls back to UTC with a warning when the system has
    no tz database (GitHub's Ubuntu runners have one)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception as e:  # ZoneInfoNotFoundError, ValueError, ImportError
        warn(f"time zone {name!r} is not available ({type(e).__name__}); using UTC")
        return timezone.utc


def local_date(now, tz_name):
    """now (an aware datetime) as a YYYY-MM-DD date in the named time zone."""
    return now.astimezone(zone(tz_name)).date().isoformat()


def valid_date(value):
    """value if it is a real YYYY-MM-DD date, else None."""
    s = str(value or "")
    if not DATE_RE.match(s):
        return None
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


# ---------------------------------------------------------------- files

def read_json(path):
    """The parsed file, or None when it is missing or not valid JSON."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warn(f"could not read {path}: {e}")
        return None


def write_json(path, doc):
    """Write atomically, so a crash never leaves half a file behind."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- settings

def _int_in(value, default, low, high):
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, n))


def _price(value, default):
    if isinstance(value, bool):
        return default
    try:
        p = float(value)
    except (TypeError, ValueError):
        return default
    return p if 0 <= p <= 1000 else default


def clean_learn_settings(raw):
    """The learn block with defaults filled and every value checked and clamped,
    so a typo in config.yml cannot ask for a huge or invalid request."""
    raw = raw if isinstance(raw, dict) else {}
    d = LEARN_DEFAULTS
    out = {}
    model = str(raw.get("model") or d["model"]).strip()
    if not MODEL_RE.match(model):
        warn(f"learn.model {model!r} is not a model id; using {d['model']}")
        model = d["model"]
    out["model"] = model
    effort = str(raw.get("effort") or d["effort"]).strip().lower()
    if effort not in EFFORTS:
        warn(f"learn.effort {effort!r} is not one of {', '.join(EFFORTS)}; using {d['effort']}")
        effort = d["effort"]
    out["effort"] = effort
    out["max_output_tokens"] = _int_in(raw.get("max_output_tokens"), d["max_output_tokens"], 1024, 64000)
    out["max_input_chars"] = _int_in(raw.get("max_input_chars"), d["max_input_chars"], 2000, 400000)
    out["topics_max"] = _int_in(raw.get("topics_max"), d["topics_max"], 1, 8)
    out["topics_min"] = _int_in(raw.get("topics_min"), d["topics_min"], 1, out["topics_max"])
    out["lookback_hours"] = _int_in(raw.get("lookback_hours"), d["lookback_hours"], 6, 168)
    prices = raw.get("price_per_mtok") if isinstance(raw.get("price_per_mtok"), dict) else {}
    out["price_per_mtok"] = {
        "input": _price(prices.get("input"), d["price_per_mtok"]["input"]),
        "output": _price(prices.get("output"), d["price_per_mtok"]["output"]),
    }
    out["prices"] = clean_prices(raw.get("prices"), d["prices"])
    out["fallbacks"] = clean_fallbacks(raw.get("fallbacks", d["fallbacks"]))
    return out


def clean_prices(value, default):
    """learn.prices as {model id: {"input": dollars, "output": dollars}}, each
    per million tokens, with an optional "cache_read" price. Entries that are
    not a model id with both prices are left out with a warning. The built-in
    table is used when config.yml has no prices block."""
    if value is None:
        value = default
    if not isinstance(value, dict):
        warn(f"learn.prices {value!r} is not a mapping of model id to prices; using the built-in prices")
        value = default
    out = {}
    for model, entry in value.items():
        model = str(model).strip()
        entry = entry if isinstance(entry, dict) else {}
        p_in, p_out = _price(entry.get("input"), None), _price(entry.get("output"), None)
        if not MODEL_RE.match(model) or p_in is None or p_out is None:
            warn(f"learn.prices entry {model!r} needs a model id with input and output prices; left out")
            continue
        out[model] = {"input": p_in, "output": p_out}
        cache_read = _price(entry.get("cache_read"), None)
        if cache_read is not None:
            out[model]["cache_read"] = cache_read
    return out


def clean_fallbacks(value):
    """"default" (Anthropic picks the fallback model by refusal category), None
    (no fallback), or a list of model ids tried in order."""
    if value is None or value is False:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "none", "off", "false", "no"):
            return None
        if v == "default":
            return "default"
        value = [value]
    if isinstance(value, (list, tuple)):
        models = []
        for m in value:
            m = str((m.get("model") if isinstance(m, dict) else m) or "").strip()
            if MODEL_RE.match(m) and m not in models:
                models.append(m)
        if models:
            return models
    warn(f"learn.fallbacks {value!r} is not understood; using default")
    return "default"


def load_settings(config_path=None):
    """{"timezone": str, "learn": {...}} from config.yml through
    scripts/config.py, with defaults for anything missing. When config.py or
    config.yml cannot be loaded, the defaults are used and a warning is logged,
    so the learning steps never fail on configuration alone."""
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    cfg = {}
    try:
        import config as config_module  # scripts/config.py

        cfg = config_module.load_config(config_path) or {}
    except Exception as e:  # ImportError (no PyYAML in this job), a bad file, ...
        warn(f"config.yml not loaded ({type(e).__name__}: {e}); using built-in learn settings")
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    tz_name = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
    return {"timezone": tz_name, "learn": clean_learn_settings(cfg.get("learn"))}


# ---------------------------------------------------------------- copy

_RANGE_DASH_RE = re.compile(r"(?<=\d)\s*[\u2013\u2014]\s*(?=\d)")
_DASH_RE = re.compile(r"\s*[\u2013\u2014\u2015]+\s*")
_DOUBLE_PUNCT_RE = re.compile(r",\s*(?=[,.;:!?)])")


def plain_punctuation(text):
    """Copy a person reads uses no em or en dashes: a dash between numbers
    becomes "to", any other becomes a comma."""
    s = _RANGE_DASH_RE.sub(" to ", str(text or ""))
    s = _DASH_RE.sub(", ", s)
    s = _DOUBLE_PUNCT_RE.sub("", s)
    s = s.strip()
    while s.startswith(","):
        s = s[1:].strip()
    return s


def one_line(text):
    return SPACE_RE.sub(" ", str(text or "")).strip()


def truncate_words(text, limit):
    """text cut to at most limit characters at a word boundary, with "..."."""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: max(0, limit - 3)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "..."


def word_count(text):
    return len(re.findall(r"\w+(?:['\u2019.\-]\w+)*", str(text or "")))


def slugify(text, limit=60):
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > limit:
        s = s[:limit]
        if "-" in s:
            s = s.rsplit("-", 1)[0]
    return s or "topic"


_URL_BAD_RE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff\\ufffe\\uffff]")


def is_https(url):
    from urllib.parse import urlparse

    s = str(url or "").strip()
    if not s or len(s) > 2000:
        return False
    # No whitespace, control characters or other characters XML cannot hold: a
    # feed can carry a link with a stray \x01 in it, and python-pptx refuses
    # to write one as a hyperlink.
    if any(c.isspace() for c in s) or _URL_BAD_RE.search(s):
        return False
    try:
        p = urlparse(s)
    except ValueError:
        return False
    return p.scheme == "https" and bool(p.netloc) and "@" not in p.netloc


# ---------------------------------------------------------------- terms

# Words that say nothing about which story an item is about. Lane relevance is
# matched separately, on the keywords in learn/lanes.md.
STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren as at be because been before being
below between both but by can cannot could did do does doing down during each few for from further had
has have having he her here hers herself him himself his how i if in into is it its itself just let me
more most my myself no nor not now of off on once only or other our ours ourselves out over own same
she should so some such than that the their theirs them themselves then there these they this those
through to too under until up very was we were what when where which while who whom why will with would
you your yours yourself yourselves
ai new news week weekly today daily episode podcast video videos newsletter issue update updates edition
part via says said say using use used uses how get gets got make makes made one two three first last
next big top best better more most way ways thing things time year years day days people really
here's what's it's that's don't can't won't you're we're they're i'm let's vs inside behind
""".split())

_URL_RE = re.compile(r"https?://\S+")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")
_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)*$")
_VERSION_RE = re.compile(r"^(?:\d{1,2}|\d+(?:\.\d+)+)$")  # 5, 12, 5.5, 3.1.4: joined to the word before
_APOSTROPHE_S_RE = re.compile("['\u2019\u02bc]s\\b")
_APOSTROPHE_RE = re.compile("['\u2019\u02bc]")


def _is_year(tok):
    return len(tok) == 4 and tok[:2] in ("19", "20") and tok.isdigit()


def tokens(text):
    """Normalized tokens of text, in order: lower case, accents folded, URLs and
    possessive 's dropped, stopwords and bare small numbers removed. A word
    followed by a version number also yields the joined token ("GPT-5.5" and
    "GPT 5.5" both give gpt, 5.5 and gpt5.5)."""
    s = _URL_RE.sub(" ", str(text or ""))
    s = _APOSTROPHE_S_RE.sub("", s)
    s = _APOSTROPHE_RE.sub("", s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    raw = _TOKEN_RE.findall(s)
    out = []
    for i, tok in enumerate(raw):
        if _NUMERIC_RE.match(tok):
            # Keep versions (5.5) and long numbers that are not years.
            if "." in tok or (len(tok) >= 3 and not _is_year(tok)):
                out.append(tok)
            continue
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if tok not in STOPWORDS and _VERSION_RE.match(nxt):
            out.append(tok + nxt)
        if tok in STOPWORDS:
            continue
        if len(tok) < 3 and not any(c.isdigit() for c in tok):
            continue
        out.append(tok)
    return out


def terms_overlap(a, b):
    """True when two term sets name the same story: they share at least 3
    terms, and at least half of the smaller set. Two sets of fewer than 3
    terms never match on terms alone (only on a shared link)."""
    a, b = set(a), set(b)
    small = min(len(a), len(b))
    if small < 3:
        return False
    shared = len(a & b)
    return shared >= 3 and shared / small >= 0.5


# ---------------------------------------------------------------- lanes.md

class Lane:
    def __init__(self, name):
        self.name = name
        self.watch = ""
        self.keywords = []
        self.sources = []  # dicts: label, url, primary

    def as_dict(self):
        return {"name": self.name, "watch": self.watch, "keywords": list(self.keywords),
                "sources": [dict(s) for s in self.sources]}


def parse_lanes(text):
    """Parse learn/lanes.md. Returns (lanes, problems): problems are
    "line N: ..." strings for lines that do not fit the format, which are
    skipped. Everything above the first "## " heading is ignored."""
    lanes, problems = [], []
    lane, in_sources = None, False
    by_name = {}
    for n, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if line.startswith("## "):
            name = one_line(line[3:])
            if not name:
                problems.append(f"line {n}: a lane heading with no name")
                lane, in_sources = None, False
                continue
            if name.lower() in by_name:
                problems.append(f"line {n}: lane {name!r} appears twice; the second one is merged into the first")
                lane = by_name[name.lower()]
            else:
                lane = Lane(name)
                by_name[name.lower()] = lane
                lanes.append(lane)
            in_sources = False
            continue
        if lane is None or not line or line.startswith("<!--"):
            continue
        low = line.lower()
        if low.startswith("watch:"):
            lane.watch = one_line(line[6:])
            in_sources = False
        elif low.startswith("keywords:"):
            for kw in line[9:].split(","):
                kw = one_line(kw)
                if kw and kw not in lane.keywords:
                    lane.keywords.append(kw)
            in_sources = False
        elif low.startswith("sources:"):
            in_sources = True
            if one_line(line[8:]):
                problems.append(f"line {n}: put each source on its own '- label | url' line under Sources:")
        elif line.startswith("- ") and in_sources:
            parts = [one_line(p) for p in line[2:].split("|")]
            label = parts[0] if parts else ""
            url = parts[1] if len(parts) > 1 else ""
            flags = [p.lower() for p in parts[2:] if p]
            if not label or not url:
                problems.append(f"line {n}: a source needs '- label | feed url'")
                continue
            if not is_https(url):
                problems.append(f"line {n}: {label}: the feed url must start with https://")
                continue
            if any(f != "primary" for f in flags):
                problems.append(f"line {n}: {label}: only 'primary' may follow the url")
            lane.sources.append({"label": label, "url": url, "primary": "primary" in flags})
        else:
            problems.append(f"line {n}: not a Watch:, Keywords: or Sources: line, skipped")
    for lane in lanes:
        if not lane.keywords:
            problems.append(f"lane {lane.name!r} has no keywords")
    return lanes, problems


def load_lanes(path=None):
    path = path or LANES_MD
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        warn(f"could not read {path}: {e}; no lanes")
        return []
    lanes, problems = parse_lanes(text)
    for p in problems:
        warn(f"lanes.md {p}")
    return lanes


# ---------------------------------------------------------------- seen.json

SEEN_KEEP_DAYS = 45   # entries older than this are pruned
SEEN_GATE_DAYS = 30   # a topic logged within this many days is not repeated
SEEN_MAX_ENTRIES = 300
SEEN_MAX_TERMS = 12
SEEN_MAX_URLS = 12


def load_seen(path=None):
    """The valid entries of learn/seen.json, oldest first."""
    doc = read_json(path or SEEN_JSON)
    raw = doc.get("topics") if isinstance(doc, dict) else None
    entries = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not valid_date(e.get("date")):
            continue
        entries.append({
            "id": slugify(e.get("id") or e.get("title")),
            "title": one_line(e.get("title"))[:200],
            "date": e["date"],
            "logged_at": iso(parse_iso(e.get("logged_at")) or datetime.fromisoformat(e["date"] + "T23:59:59+00:00")),
            "terms": [str(t) for t in (e.get("terms") or []) if isinstance(t, str)][:SEEN_MAX_TERMS],
            "urls": [str(u) for u in (e.get("urls") or []) if is_https(u)][:SEEN_MAX_URLS],
            "sources": [one_line(s)[:120] for s in (e.get("sources") or []) if isinstance(s, str)][:SEEN_MAX_URLS],
            "primary": bool(e.get("primary")),
        })
    entries.sort(key=lambda e: (e["logged_at"], e["id"]))
    return entries


def seen_before(entries, day):
    """The entries logged on an earlier day than day (YYYY-MM-DD). A re-run of
    the morning run must pick the same topics again, so what the first run of
    the day logged does not count as seen."""
    return [e for e in entries if e["date"] != day]


def seen_matches(entries, terms, urls):
    """The seen entries that are the same story as a topic with these terms and
    urls: a shared url, or terms_overlap()."""
    urls = set(urls)
    return [e for e in entries if urls & set(e["urls"]) or terms_overlap(terms, e["terms"])]


def updated_seen(entries, topics, day, now):
    """entries plus one entry per topic logged on day, pruned to the last
    SEEN_KEEP_DAYS days and SEEN_MAX_ENTRIES entries. A re-run replaces
    everything logged for day, so the log holds only the topics of the
    briefing that is published for that day."""
    keep_from = (date.fromisoformat(day) - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    out = [e for e in entries if e["date"] >= keep_from and e["date"] != day]
    for t in topics:
        out.append({
            "id": t["id"],
            "title": one_line(t.get("title"))[:200],
            "date": day,
            "logged_at": iso(now),
            "terms": list(t.get("terms") or [])[:SEEN_MAX_TERMS],
            "urls": [u for u in (t.get("urls") or []) if is_https(u)][:SEEN_MAX_URLS],
            "sources": list(t.get("sources") or [])[:SEEN_MAX_URLS],
            "primary": bool(t.get("primary")),
        })
    out.sort(key=lambda e: (e["logged_at"], e["id"]))
    return out[-SEEN_MAX_ENTRIES:]
