"""The private stock section (contract section 3.1).

A minimal version of the shared surface, written by the data layer so its
modules import. The run layer owns this file and may replace it with the full
version; the names and behavior below follow the contract.
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPTS = os.path.join(ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import learn  # noqa: E402

TICKER_RE = re.compile(r"^[A-Z]{1,6}(?:-[A-Z]{1,3})?$")
MAX_TICKERS = 30
ALLOWED_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
NY_TZ = "America/New_York"
STATE_VERSION = 1
DOC_VERSION = 1
OVERVIEW_VERSION = 1
CONGRESS_VERSION = 1
STATE_MAX_BYTES = 1_048_576
DOC_MAX_BYTES = 524_288
OVERVIEW_MAX_BYTES = 262_144
CONFIG_MAX_BYTES = 16_384
DISCLAIMER = ("Not financial advice. This is an automated reading of public data for one person, "
              "and it can be wrong. Do your own research before you buy or sell anything.")


class StocksError(Exception):
    pass


def normalize_ticker(value):
    """The ticker in SEC and Tiingo spelling ("BRK-B"), or None."""
    if not isinstance(value, str):
        return None
    s = value.strip().upper().replace(".", "-").replace("/", "-")
    return s if TICKER_RE.match(s) else None


def finra_symbol(ticker):
    """FINRA spells share classes with a slash: "BRK-B" -> "BRK/B"."""
    return str(ticker).replace("-", "/")


def log(message):
    print(message, flush=True)


def warn(message):
    log("[warn] " + message)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def today_ny(now=None):
    from zoneinfo import ZoneInfo

    now = now or utc_now()
    return now.astimezone(ZoneInfo(NY_TZ)).date()


def _finite(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def round_price(x):
    f = _finite(x)
    return None if f is None else round(f, 4)


def round_pct(x):
    f = _finite(x)
    return None if f is None else round(f, 2)


def dumps(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def size_of(doc):
    return len(dumps(doc).encode("utf-8"))


_SPACES_RE = re.compile(r"[ \t\r\f\v]+")


def plain(text, limit):
    """Plain punctuation, no markdown emphasis, paragraph breaks kept, cut to limit."""
    s = str(text or "").replace("**", "").replace("__", "")
    paragraphs = []
    for para in re.split(r"\n\s*\n", s):
        p = learn.plain_punctuation(learn.one_line(para))
        if p:
            paragraphs.append(p)
    return learn.truncate_words("\n\n".join(paragraphs), limit)

