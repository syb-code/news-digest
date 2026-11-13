#!/usr/bin/env python3
"""Trades of members of Congress, best effort (contract section 5.8).

House: the Clerk publishes a yearly index of financial disclosures
(<year>FD.zip) and one PDF per periodic transaction report (PTR). This module
reads the index, downloads the PTRs of the window, extracts their text with
pypdf and finds every trade by anchoring on the transaction type and the two
dates. Paper filings are scanned images with no text and are skipped. The
public congress job runs this with no secret and uploads every member's trades
as an artifact; the private job checks that artifact with check_bundle before
using it, because the job that made it ran third-party code (pypdf).

    python -I scripts/stocks/data/congress.py house --out PATH --days 365 --max-seconds 300

Senate (optional): Financial Modeling Prep's senate-trades endpoint when the
owner sets FMP_API_KEY. The key is added to the URL at request time only and
is never stored or logged. The endpoint shape comes from FMP's documentation
and was not verified from the build sandbox.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__":
    # Run as a script: this folder holds calendar.py, which must not shadow the
    # standard library, and the package root is scripts/.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]
    _SCRIPTS = os.path.dirname(os.path.dirname(_HERE))
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)

import argparse  # noqa: E402
import concurrent.futures  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
import zipfile  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

from stocks import CONGRESS_VERSION, TICKER_RE, normalize_ticker  # noqa: E402
from stocks.data import FetchError, HttpClient  # noqa: E402

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
HOUSE_HOST = "disclosures-clerk.house.gov"
FMP_SENATE = "https://financialmodelingprep.com/stable/senate-trades?symbol={symbol}"
PUBLIC_USER_AGENT = "news-digest-stocks/1.0"
MAX_BUNDLE_BYTES = 20_000_000
MAX_TRADES = 50_000
MAX_INDEX_BYTES = 50_000_000
SCANNED_CHARS = 50
WORKERS = 4

TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent"}
TRADE_TYPES = frozenset(TYPES.values())
TRADE_OWNERS = frozenset(("self", "spouse", "joint", "dependent"))
TRADE_KEYS = ("member", "chamber", "district", "ticker", "asset", "asset_type", "type", "owner", "date",
              "notified", "filed", "amount_range", "source")
COUNT_KEYS = ("window_days", "filings", "electronic", "scanned", "failed", "skipped_for_time")

HEADER_RE = re.compile(r"ID Owner Asset Transaction Type Date Notification Date Amount Cap\. Gains > \$200\?")
FILING_ID_RE = re.compile(r"Filing ID #\d+")
ANCHOR_RE = re.compile(r"(P|S \(partial\)|S|E)\s*(\d{2}/\d{2}/\d{4})\s*(\d{2}/\d{2}/\d{4})\s*"
                       r"(\$[\d,]+ ?- ?\$[\d,]+|(?:Spouse/DC )?Over \$[\d,]+|\$[\d,]+ ?-|\$[\d,]+)?")
ASSET_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9]{0,5}(?:[.\-/][A-Z]{1,2})?)\)\s*(?:\[[A-Z]{2,3}\])?\s*$")
ASSET_TAG_RE = re.compile(r"\[([A-Z]{2,3})\]\s*$")
COMMENT_LINE_RE = re.compile(r"^(?:F\s*S|S\s*O|L|D|C)\s*:(?:\s|$)")
INLINE_COMMENT_RE = re.compile(r"(?:^|\s)(?:F S|S O|L|D|C) ?: ")
SEP = ""  # stands for a comment line; str.split() keeps it (it is not whitespace)
DISTRICT_RE = re.compile(r"^[A-Z]{2}\d{2}$")
DOC_ID_RE = re.compile(r"^\d{5,12}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _mdy(value):
    """M/D/YYYY or MM/DD/YYYY as YYYY-MM-DD, or None."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# ---------------------------------------------------------------- House index

def house_index(zip_bytes):
    """The periodic transaction reports (FilingType P) of one year's index:
    [{"member", "district", "filed" (None when the index leaves it empty),
    "doc_id", "year"}]."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, TypeError, ValueError):
        raise ValueError("the House index is not a zip file") from None
    names = [i for i in zf.infolist() if i.filename.lower().endswith("fd.xml")]
    if not names:
        raise ValueError("the House index has no XML file")
    info = names[0]
    if info.file_size > MAX_INDEX_BYTES:
        raise ValueError("the House index is too large")
    data = zf.read(info)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if _DTD_RE.search(data):
        raise ValueError("the House index declares entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("the House index cannot be read") from None
    out = []
    for m in root.iter():
        if m.tag.rsplit("}", 1)[-1] != "Member":
            continue
        f = {c.tag.rsplit("}", 1)[-1]: " ".join((c.text or "").split()) for c in m}
        if f.get("FilingType") != "P":
            continue
        doc_id = f.get("DocID") or ""
        try:
            year = int(f.get("Year") or 0)
        except ValueError:
            year = 0
        if not DOC_ID_RE.match(doc_id) or not (1990 <= year <= 2100):
            continue
        member = " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last"), f.get("Suffix")) if x)
        district = f.get("StateDst") or None
        out.append({"member": member, "district": district if district and DISTRICT_RE.match(district) else None,
                    "filed": _mdy(f.get("FilingDate")), "doc_id": doc_id, "year": year})
    return out


# ---------------------------------------------------------------- PTR text

def ptr_text(pdf_bytes):
    """The PDF's text, pages joined with a newline; "" when it has no text layer
    (a scanned paper filing). pypdf is imported here only."""
    import logging

    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is not installed") from None
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception:  # pypdf raises many types on a broken file
        raise ValueError("the PDF cannot be read") from None
    return "\n".join(pages).replace("\x00", "")


def _asset_part(chunk):
    """The owner code and asset at the end of the text between two trades: what
    follows the previous trade's last comment line."""
    part = chunk.rsplit(SEP, 1)[-1]
    inline = list(INLINE_COMMENT_RE.finditer(part))
    if inline:  # a comment that did not start its own line
        tail = part[inline[-1].end():]
        owner = re.search(r"(?:^|\s)(SP|JT|DC)\s+\S", tail)
        part = tail[owner.start():] if owner else tail
    return " ".join(part.split())


def parse_ptr(text, filing):
    """Trades of one House periodic transaction report from its text."""
    raw = str(text or "").replace("\x00", "")
    if len(raw.strip()) < SCANNED_CHARS:
        return []
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        lines.append(SEP if COMMENT_LINE_RE.match(s) else s)
    flat = " ".join(" ".join(lines).split())
    head = flat.replace(SEP, " ")
    name = re.search(r"Name:\s*(.+?)\s+Status:", head)
    district = re.search(r"State/District:\s*([A-Z]{2}\d{2})", head)
    first = HEADER_RE.search(flat)
    if not first:
        return []
    end = flat.find("* For the complete list", first.end())
    body = flat[first.end(): end if end >= 0 else len(flat)]
    body = HEADER_RE.sub(" ", FILING_ID_RE.sub(" ", body))
    body = " ".join(body.split())

    member = (filing or {}).get("member") or (" ".join(name.group(1).split()) if name else None)
    dist = (filing or {}).get("district") or (district.group(1) if district else None)
    year, doc_id = (filing or {}).get("year"), (filing or {}).get("doc_id")
    source = HOUSE_PTR.format(year=year, doc_id=doc_id) if year and doc_id else None
    trades, prev = [], 0
    for a in ANCHOR_RE.finditer(body):
        part = _asset_part(body[prev:a.start()])
        prev = a.end()
        tm = ASSET_TICKER_RE.search(part)
        ticker = normalize_ticker(tm.group(1)) if tm else None
        tag = ASSET_TAG_RE.search(part)
        owner = "self"
        om = re.match(r"^(SP|JT|DC)\s+", part)
        if om:
            owner = OWNERS[om.group(1)]
            part = part[om.end():]
        asset = ASSET_TAG_RE.sub("", part).strip()
        asset = re.sub(r"\s*\(([A-Z][A-Z0-9]{0,5}(?:[.\-/][A-Z]{1,2})?)\)\s*$", "", asset).strip()
        amount = a.group(4)
        if amount:
            amount = re.sub(r"\s*-\s*", " - ", amount).strip()
        trades.append({
            "member": member,
            "chamber": "House",
            "district": dist,
            "ticker": ticker,
            "asset": asset[:300] or None,
            "asset_type": tag.group(1) if tag else None,
            "type": TYPES[a.group(1)],
            "owner": owner,
            "date": _mdy(a.group(2)),
            "notified": _mdy(a.group(3)),
            "filed": (filing or {}).get("filed"),
            "amount_range": amount or None,
            "source": source,
        })
    return trades


# ---------------------------------------------------------------- House download

def _years(today, days):
    start = today - timedelta(days=int(days))
    return list(range(today.year, start.year - 1, -1))


def fetch_house(http, today, days, max_seconds, clock=time.monotonic, workers=WORKERS, text_of=None):
    """The CongressBundle of every House PTR filed in the last `days` days.
    PDFs are downloaded newest first, `workers` at a time, until max_seconds
    have passed; the rest are counted as skipped_for_time. text_of (tests)
    replaces ptr_text."""
    if text_of is None:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            raise RuntimeError("pypdf is not installed") from None
        text_of = ptr_text
    started = clock()
    today = _as_date(today)
    since = (today - timedelta(days=int(days))).isoformat()
    filings = []
    for year in _years(today, days):
        resp = http.get(HOUSE_ZIP.format(year=year), allow=(404,) if year != today.year else ())
        if resp.status == 404:
            continue
        filings += house_index(resp.body)
    seen, window = set(), []
    for f in filings:
        if f["filed"] and since <= f["filed"] <= today.isoformat() and f["doc_id"] not in seen:
            seen.add(f["doc_id"])
            window.append(f)
    window.sort(key=lambda f: (f["filed"], f["doc_id"]), reverse=True)

    counts = {"electronic": 0, "scanned": 0, "failed": 0, "skipped_for_time": 0}
    trades = []

    def download(f):
        return http.get(HOUSE_PTR.format(year=f["year"], doc_id=f["doc_id"])).body

    pending = list(window)
    running = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        while pending or running:
            while pending and len(running) < workers and clock() - started < max_seconds:
                f = pending.pop(0)
                running[pool.submit(download, f)] = f
            if not running:
                break
            done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                f = running.pop(fut)
                try:
                    text = text_of(fut.result())
                except (FetchError, ValueError):
                    counts["failed"] += 1
                    continue
                if len(text.strip()) < SCANNED_CHARS:
                    counts["scanned"] += 1
                    continue
                counts["electronic"] += 1
                trades += parse_ptr(text, f)
    counts["skipped_for_time"] = len(pending)
    trades.sort(key=lambda t: (t["date"] or "", t["filed"] or "", t["member"] or ""), reverse=True)
    return {"kind": "stocks.congress", "version": CONGRESS_VERSION,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "window_days": int(days), "filings": len(window), **counts, "trades": trades[:MAX_TRADES]}


# ---------------------------------------------------------------- the artifact check

def _str_or_none(value, limit):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("a trade field has the wrong type")
    return value


def _date_or_none(value):
    if value is None:
        return None
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError("a trade date is not a date")
    date.fromisoformat(value)
    return value


def _check_trade(t):
    if not isinstance(t, dict) or set(t) - set(TRADE_KEYS):
        raise ValueError("a trade has unknown fields")
    ticker = t.get("ticker")
    if ticker is not None and (not isinstance(ticker, str) or not TICKER_RE.match(ticker)):
        raise ValueError("a trade has a bad ticker")
    if t.get("chamber") != "House":
        raise ValueError("a trade is not from the House")
    if t.get("type") not in TRADE_TYPES or t.get("owner") not in TRADE_OWNERS:
        raise ValueError("a trade has a bad type or owner")
    district = t.get("district")
    if district is not None and (not isinstance(district, str) or not DISTRICT_RE.match(district)):
        raise ValueError("a trade has a bad district")
    asset_type = t.get("asset_type")
    if asset_type is not None and (not isinstance(asset_type, str) or not re.match(r"^[A-Z]{2,3}$", asset_type)):
        raise ValueError("a trade has a bad asset type")
    source = t.get("source")
    if source is not None:
        parts = urllib.parse.urlsplit(source) if isinstance(source, str) else None
        if not parts or parts.scheme != "https" or parts.hostname != HOUSE_HOST or len(source) > 200 \
                or not re.match(r"^/public_disc/ptr-pdfs/\d{4}/\d{5,12}\.pdf$", parts.path) or parts.query:
            raise ValueError("a trade has a bad source link")
    return {
        "member": _str_or_none(t.get("member"), 200),
        "chamber": "House",
        "district": district,
        "ticker": ticker,
        "asset": _str_or_none(t.get("asset"), 300),
        "asset_type": asset_type,
        "type": t["type"],
        "owner": t["owner"],
        "date": _date_or_none(t.get("date")),
        "notified": _date_or_none(t.get("notified")),
        "filed": _date_or_none(t.get("filed")),
        "amount_range": _str_or_none(t.get("amount_range"), 60),
        "source": source,
    }


def check_bundle(doc):
    """The congress artifact, checked field by field and rebuilt from the known
    keys only; ValueError when anything is off."""
    if not isinstance(doc, dict) or doc.get("kind") != "stocks.congress" or doc.get("version") != CONGRESS_VERSION:
        raise ValueError("not a congress bundle")
    out = {"kind": "stocks.congress", "version": CONGRESS_VERSION}
    gen = doc.get("generated_at")
    if not isinstance(gen, str) or len(gen) > 40:
        raise ValueError("the bundle has no generation time")
    try:
        datetime.fromisoformat(gen)
    except ValueError:
        raise ValueError("the bundle has no generation time") from None
    out["generated_at"] = gen
    for k in COUNT_KEYS:
        v = doc.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 10_000_000):
            raise ValueError("a bundle count is not a number")
        out[k] = v
    trades = doc.get("trades")
    if not isinstance(trades, list) or len(trades) > MAX_TRADES:
        raise ValueError("the bundle has too many trades")
    out["trades"] = [_check_trade(t) for t in trades]
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise ValueError("the bundle is too large")
    return out


def load_bundle(path):
    """The checked bundle from a file (at most 20 MB), or ValueError."""
    try:
        if os.path.getsize(path) > MAX_BUNDLE_BYTES:
            raise ValueError("the bundle is too large")
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except OSError:
        raise ValueError("the bundle cannot be read") from None
    except UnicodeDecodeError:
        raise ValueError("the bundle is not text") from None
    return check_bundle(doc)


def for_ticker(trades, ticker, since):
    """The trades of one ticker on or after since (a date), newest first."""
    t = normalize_ticker(ticker)
    s = _as_date(since).isoformat()
    out = [x for x in trades or () if isinstance(x, dict) and x.get("ticker") == t
           and (x.get("date") or x.get("filed") or "") >= s]
    return sorted(out, key=lambda x: (x.get("date") or "", x.get("filed") or "", x.get("member") or ""),
                  reverse=True)


# ---------------------------------------------------------------- Senate (optional, FMP)

_FMP_TYPES = {"purchase": "purchase", "sale": "sale", "sale (full)": "sale", "sale (partial)": "partial sale",
              "exchange": "exchange"}
_FMP_OWNERS = {"self": "self", "spouse": "spouse", "joint": "joint", "child": "dependent",
               "dependent": "dependent", "dependent child": "dependent"}
_FMP_ASSET_TYPES = {"stock": "ST", "stock option": "OP", "corporate bond": "CS", "municipal security": "GS"}


def parse_fmp(rows):
    """Trades from FMP senate-trades rows (shape from FMP's documentation)."""
    out = []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        kind = _FMP_TYPES.get(str(r.get("type") or "").strip().lower())
        if kind is None:
            continue
        first = " ".join(str(r.get("firstName") or "").split())
        last = " ".join(str(r.get("lastName") or "").split())
        member = (f"{first} {last}".strip() or " ".join(str(r.get("office") or "").split()))[:200] or None
        link = str(r.get("link") or "").strip()
        parts = urllib.parse.urlsplit(link) if link else None
        source = link if parts and parts.scheme == "https" and parts.hostname and len(link) <= 300 else None
        amount = " ".join(str(r.get("amount") or "").split())[:60] or None
        out.append({
            "member": member,
            "chamber": "Senate",
            "district": None,
            "ticker": normalize_ticker(r.get("symbol")),
            "asset": " ".join(str(r.get("assetDescription") or "").split())[:300] or None,
            "asset_type": _FMP_ASSET_TYPES.get(str(r.get("assetType") or "").strip().lower()),
            "type": kind,
            "owner": _FMP_OWNERS.get(str(r.get("owner") or "").strip().lower(), "self"),
            "date": _iso_day(r.get("transactionDate")),
            "notified": None,
            "filed": _iso_day(r.get("disclosureDate")),
            "amount_range": amount,
            "source": source,
        })
    return out


def _iso_day(value):
    s = str(value or "").strip()[:10]
    if not DATE_RE.match(s):
        return _mdy(value)
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def senate_fmp(http, symbol, api_key):
    """Senate trades of one symbol from FMP. The key is added to the URL here
    only; FetchError reasons never hold the URL."""
    if not api_key:
        raise FetchError("no FMP key")
    url = FMP_SENATE.format(symbol=urllib.parse.quote(str(symbol), safe="")) + "&apikey=" + \
        urllib.parse.quote(str(api_key), safe="")
    doc = http.get(url).json()
    if isinstance(doc, dict):
        raise FetchError("FMP refused the request", 403)
    if not isinstance(doc, list):
        raise ValueError("FMP sent an unexpected answer")
    return parse_fmp(doc)


def senate_trades(http, symbol, api_key):
    """(trades, coverage): coverage "off" without a key, "ok", "not available
    on this plan" (401, 402 or 403) or "error"."""
    if not api_key:
        return [], "off"
    try:
        return senate_fmp(http, symbol, api_key), "ok"
    except FetchError as e:
        if e.status in (401, 402, 403):
            return [], "not available on this plan"
        return [], "error"
    except ValueError:
        return [], "error"


# ---------------------------------------------------------------- CLI

def _write_json(path, doc):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main(argv=None, http=None, today=None):
    parser = argparse.ArgumentParser(description="Trades of members of Congress, best effort.")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("house", help="Read the House periodic transaction reports of the window.")
    h.add_argument("--out", required=True)
    h.add_argument("--days", type=int, default=365)
    h.add_argument("--max-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    if not (1 <= args.days <= 800) or args.max_seconds <= 0:
        parser.error("--days must be 1 to 800 and --max-seconds positive")
    http = http or HttpClient(PUBLIC_USER_AGENT, timeout=30.0, max_bytes=30_000_000)
    if today is None:
        from stocks.data import trading_days

        today = trading_days.ny_time(datetime.now(timezone.utc)).date()
    try:
        bundle = fetch_house(http, today, args.days, args.max_seconds)
    except RuntimeError:
        print("Stopped: pypdf is not installed.", file=sys.stderr, flush=True)
        return 1
    except (FetchError, ValueError) as e:
        reason = e.reason if isinstance(e, FetchError) else "bad data"
        print(f"Could not read the House index: {reason}.", file=sys.stderr, flush=True)
        return 1
    _write_json(args.out, bundle)
    print(f"Read {bundle['electronic']:,} electronic reports, {bundle['scanned']:,} scanned reports skipped, "
          f"{len(bundle['trades']):,} trades.", flush=True)
    if bundle["failed"] or bundle["skipped_for_time"]:
        print(f"{bundle['failed']:,} reports could not be read and {bundle['skipped_for_time']:,} were left "
              "for lack of time.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
