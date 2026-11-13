"""FINRA short sale volume and short interest (contract section 5.4).

The FINRA Query API needs no credentials for these two datasets:

    POST https://api.finra.org/data/group/otcMarket/name/regShoDaily
    POST https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest

Every query carries a symbol filter and a date range (without one the API
answers with an old default partition). 204 means no rows. Rows come
unordered, one per reporting facility a day for short volume: they are summed
by date here, which equals FINRA's consolidated daily file exactly.

The daily file CNMSshvolYYYYMMDD.txt on cdn.finra.org is the fallback when the
API fails, read only for the few recent trading days the cache lacks. A day
with no file answers 403 (an S3 AccessDenied page), not 404.
"""
import re
from datetime import date, datetime, timedelta

from stocks.data import FetchError
from stocks.data import trading_days

REGSHO = "https://api.finra.org/data/group/otcMarket/name/regShoDaily"
SI = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
CNMS = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day}.txt"
LIMIT = 5000
MAX_PAGES = 4
HISTORY_DAYS = 365
DELTA_DAYS = 5
KEEP_ROWS = 1500
FALLBACK_FILES = 5


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _day(value):
    s = str(value or "").strip()
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    s = s[:10]
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _query(http, url, symbol_field, symbol, date_field, start, end):
    """Every row of one filtered query (paged by LIMIT)."""
    rows = []
    for page in range(MAX_PAGES):
        payload = {
            "limit": LIMIT,
            "compareFilters": [{"compareType": "equal", "fieldName": symbol_field, "fieldValue": symbol}],
            "dateRangeFilters": [{"fieldName": date_field, "startDate": _as_date(start).isoformat(),
                                  "endDate": _as_date(end).isoformat()}],
        }
        if page:
            payload["offset"] = page * LIMIT
        resp = http.post_json(url, payload, allow=(204,))
        if resp.status == 204 or not resp.body.strip():
            break
        doc = resp.json()
        if not isinstance(doc, list):
            raise ValueError("FINRA sent an unexpected answer")
        rows.extend(r for r in doc if isinstance(r, dict))
        if len(doc) < LIMIT:
            break
    return rows


# ---------------------------------------------------------------- short sale volume

def parse_regsho(rows, symbol=None):
    """ShortVolumeRows (one per date, oldest first) from regShoDaily rows: the
    reporting facilities of a date summed."""
    by_date = {}
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        if symbol is not None and r.get("securitiesInformationProcessorSymbolIdentifier") not in (None, symbol):
            continue
        day = _day(r.get("tradeReportDate"))
        total = _num(r.get("totalParQuantity"))
        if day is None or total is None:
            continue
        acc = by_date.setdefault(day, [0.0, 0.0, 0.0])
        acc[0] += _num(r.get("shortParQuantity")) or 0.0
        acc[1] += _num(r.get("shortExemptParQuantity")) or 0.0
        acc[2] += total
    return [{"date": d, "short_volume": round(v[0], 6), "exempt_volume": round(v[1], 6),
             "total_volume": round(v[2], 6)} for d, v in sorted(by_date.items())]


def short_volume(http, symbol, start, end):
    """ShortVolumeRows for symbol (FINRA spelling, BRK/B) from start to end."""
    rows = _query(http, REGSHO, "securitiesInformationProcessorSymbolIdentifier", symbol, "tradeReportDate",
                  start, end)
    return parse_regsho(rows, symbol)


def parse_cnms(text):
    """{symbol: ShortVolumeRow} from one daily CNMS file."""
    lines = [ln for ln in str(text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return {}
    header = [h.strip() for h in lines[0].split("|")]
    need = ("Date", "Symbol", "ShortVolume", "ShortExemptVolume", "TotalVolume")
    if not all(h in header for h in need):
        raise ValueError("the FINRA daily file has an unknown header")
    idx = {h: header.index(h) for h in need}
    out = {}
    for ln in lines[1:]:
        if "|" not in ln:
            continue  # the trailing record count
        parts = ln.split("|")
        if len(parts) < len(header):
            continue
        day = _day(parts[idx["Date"]])
        sym = parts[idx["Symbol"]].strip()
        total = _num(parts[idx["TotalVolume"]])
        if day is None or not sym or total is None:
            continue
        out[sym] = {"date": day, "short_volume": round(_num(parts[idx["ShortVolume"]]) or 0.0, 6),
                    "exempt_volume": round(_num(parts[idx["ShortExemptVolume"]]) or 0.0, 6),
                    "total_volume": round(total, 6)}
    return out


def short_volume_file(http, day):
    """The CNMS file of one day, or None when FINRA has no file for it."""
    resp = http.get(CNMS.format(day=_as_date(day).strftime("%Y%m%d")), allow=(403, 404))
    if resp.status in (403, 404):
        return None
    return parse_cnms(resp.text())


# ---------------------------------------------------------------- short interest

def parse_short_interest(rows, symbol=None):
    """ShortInterestRows, oldest first, one per settlement date."""
    by_date = {}
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        if symbol is not None and r.get("symbolCode") not in (None, symbol):
            continue
        day = _day(r.get("settlementDate"))
        si = _num(r.get("currentShortPositionQuantity"))
        if day is None or si is None:
            continue
        change = _num(r.get("changePercent"))
        dtc = _num(r.get("daysToCoverQuantity"))
        by_date[day] = {
            "settlement_date": day,
            "short_interest": si,
            "previous": _num(r.get("previousShortPositionQuantity")),
            "change_pct": round(change, 2) if change is not None else None,
            "avg_daily_volume": _num(r.get("averageDailyVolumeQuantity")),
            "days_to_cover": round(dtc, 2) if dtc is not None else None,
        }
    return [by_date[d] for d in sorted(by_date)]


_ONE_LETTER_CLASS_RE = re.compile(r"^([A-Z]{1,6})[/.\-]([A-Z])$")
_PLAIN_RE = re.compile(r"^[A-Z]{1,6}$")


def si_symbol(symbol):
    """The short interest dataset spells class shares without a separator:
    BRK/B (or BRK-B) is BRKB there, while the short volume data uses BRK/B.
    Only a one-letter share class is joined; a longer suffix (a preferred
    share or a warrant, T-PC or NE-WT) gives None, because joining it would
    spell another company's common stock (TPC, NEWT)."""
    s = str(symbol or "").strip().upper()
    if _PLAIN_RE.match(s):
        return s
    m = _ONE_LETTER_CLASS_RE.match(s)
    return m.group(1) + m.group(2) if m else None


def short_interest(http, symbol, start, end):
    """ShortInterestRows for symbol (either spelling) from start to end; [] for
    a symbol the dataset cannot be asked about (see si_symbol)."""
    sym = si_symbol(symbol)
    if sym is None:
        return []
    rows = _query(http, SI, "symbolCode", sym, "settlementDate", start, end)
    return parse_short_interest(rows, sym)


# ---------------------------------------------------------------- caches

def _key(row):
    return row.get("date") or row.get("settlement_date")


def merge_by_date(old, new, keep):
    """old and new rows merged by date (new wins), oldest first, the last keep."""
    by = {}
    for r in list(old or ()) + list(new or ()):
        if isinstance(r, dict) and _key(r):
            by[_key(r)] = r
    rows = [by[k] for k in sorted(by)]
    return rows[-int(keep):] if keep else rows


def _recent_trading_days(today, after, n):
    """Up to n trading days on or before today and after `after` (a date or None), oldest first."""
    days, d = [], today
    if not trading_days.is_trading_day(d):
        d = trading_days.previous_trading_day(d)
    while len(days) < n and (after is None or d > after):
        days.append(d)
        d = trading_days.previous_trading_day(d)
    return list(reversed(days))


def refresh_short_volume(http, symbol, cached, today, keep=KEEP_ROWS, fallback_files=FALLBACK_FILES):
    """(rows, info) for one ticker: the cached rows plus what FINRA has added.

    Asks the API for [last cached date - 5 days, today] (one year for a new
    ticker); when the API fails, reads the daily files of the missing recent
    trading days (at most fallback_files). info: {"source": "api" | "files" |
    "cache", "added": n, "reason": None or a plain reason}."""
    today = _as_date(today)
    cached = [r for r in cached or () if isinstance(r, dict) and _day(r.get("date"))]
    known = {r["date"] for r in cached}
    last = max(known) if known else None
    start = (date.fromisoformat(last) - timedelta(days=DELTA_DAYS)) if last else today - timedelta(days=HISTORY_DAYS)
    try:
        fresh = short_volume(http, symbol, start, today)
        rows = merge_by_date(cached, fresh, keep)
        return rows, {"source": "api", "added": sum(1 for r in fresh if r["date"] not in known), "reason": None}
    except (FetchError, ValueError) as e:
        reason = e.reason if isinstance(e, FetchError) else "bad data"
    fresh = []
    after = date.fromisoformat(last) if last else None
    for d in _recent_trading_days(today, after, fallback_files):
        try:
            table = short_volume_file(http, d)
        except (FetchError, ValueError):
            continue
        if table and symbol in table:
            fresh.append(table[symbol])
    rows = merge_by_date(cached, fresh, keep)
    added = sum(1 for r in fresh if r["date"] not in known)
    return rows, {"source": "files" if fresh else "cache", "added": added, "reason": reason}


def refresh_short_interest(http, symbol, cached, today, keep=60):
    """(rows, info) like refresh_short_volume, from the API only (no fallback)."""
    today = _as_date(today)
    cached = [r for r in cached or () if isinstance(r, dict) and _day(r.get("settlement_date"))]
    known = {r["settlement_date"] for r in cached}
    last = max(known) if known else None
    start = (date.fromisoformat(last) - timedelta(days=DELTA_DAYS)) if last else today - timedelta(days=HISTORY_DAYS)
    try:
        fresh = short_interest(http, symbol, start, today)
    except (FetchError, ValueError) as e:
        return merge_by_date(cached, [], keep), {
            "source": "cache", "added": 0, "reason": e.reason if isinstance(e, FetchError) else "bad data"}
    rows = merge_by_date(cached, fresh, keep)
    return rows, {"source": "api", "added": sum(1 for r in fresh if r["settlement_date"] not in known),
                  "reason": None}


VOLUME_FIELDS = ["date", "short_volume", "exempt_volume", "total_volume"]


def volume_cache(rows):
    """The compact short volume cache kept in a ticker's state."""
    rows = [r for r in rows or () if isinstance(r, dict) and _day(r.get("date"))]
    return {"fields": list(VOLUME_FIELDS), "rows": [[r[f] for f in VOLUME_FIELDS] for r in rows],
            "last_date": rows[-1]["date"] if rows else None}


def volume_rows(cache):
    """ShortVolumeRows from a state cache; [] when it is missing or malformed."""
    if not isinstance(cache, dict) or cache.get("fields") != VOLUME_FIELDS or not isinstance(cache.get("rows"), list):
        return []
    out = []
    for row in cache["rows"]:
        if not isinstance(row, list) or len(row) != len(VOLUME_FIELDS):
            return []
        r = dict(zip(VOLUME_FIELDS, row))
        if _day(r["date"]) != r["date"] or any(_num(r[f]) is None for f in VOLUME_FIELDS[1:]):
            return []
        out.append({"date": r["date"], "short_volume": float(r["short_volume"]),
                    "exempt_volume": float(r["exempt_volume"]), "total_volume": float(r["total_volume"])})
    return merge_by_date([], out, 0)
