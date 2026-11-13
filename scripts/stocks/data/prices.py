"""Daily prices from Tiingo (contract section 5.2).

    GET https://api.tiingo.com/tiingo/daily/<ticker>/prices?startDate=YYYY-MM-DD[&endDate=...]&format=json
    Authorization: Token <key>

The answer is a JSON list of {date, open, high, low, close, volume, adjOpen,
adjHigh, adjLow, adjClose, adjVolume, divCash, splitFactor}. A Bar keeps the
adjusted values as open/high/low/close/volume (every indicator uses them) and
the raw close and volume as raw_close/raw_volume.

The key travels only in the Authorization header: never in a URL, a log, an
error or a stored value. A ticker's history is cached in its own state and
refreshed with a small delta request each day; a dividend or split that the
cache does not hold, or adjusted closes that moved, mean Tiingo re-adjusted
the history, so the whole window is fetched again.
"""
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from stocks import TICKER_RE, round_price
from stocks.data import FetchError
from stocks.data import trading_days

TIINGO_PRICES = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
FIELDS = ["date", "open", "high", "low", "close", "volume", "raw_close", "raw_volume", "div", "split"]
DELTA_DAYS = 7
REFETCH_TOLERANCE = 0.0005  # 0.05 percent
WARMUP_DAYS = 300


class PriceError(FetchError):
    """kind: "auth" | "not_found" | "rate" | "network" | "bad_data"."""

    def __init__(self, reason, kind, status=None):
        super().__init__(reason, status)
        self.kind = kind


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _valid_day(value):
    s = str(value or "")[:10]
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def parse_tiingo(rows):
    """Tiingo rows as Bars, sorted by date, one per date. Rows without a positive
    adjusted close are dropped. ValueError when rows is not a list."""
    if not isinstance(rows, list):
        raise ValueError("the price answer is not a list")
    by_date = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        day = _valid_day(r.get("date"))
        close = _num(r.get("adjClose"))
        if day is None or close is None or close <= 0:
            continue

        def adj(key, fallback):
            v = _num(r.get(key))
            return v if v is not None and v > 0 else fallback

        high = adj("adjHigh", close)
        low = adj("adjLow", close)
        open_ = adj("adjOpen", close)
        volume = _num(r.get("adjVolume"))
        raw_close = _num(r.get("close"))
        raw_volume = _num(r.get("volume"))
        div = _num(r.get("divCash"))
        split = _num(r.get("splitFactor"))
        by_date[day] = {
            "date": day,
            "open": round_price(open_),
            "high": round_price(max(high, open_, close)),
            "low": round_price(min(low, open_, close)),
            "close": round_price(close),
            "volume": float(round(volume)) if volume is not None and volume >= 0 else 0.0,
            "raw_close": round_price(raw_close) if raw_close is not None and raw_close > 0 else round_price(close),
            "raw_volume": int(round(raw_volume)) if raw_volume is not None and raw_volume >= 0 else 0,
            "div": round(div, 6) if div is not None else 0.0,
            "split": split if split is not None and split > 0 else 1.0,
        }
    return [by_date[d] for d in sorted(by_date)]


def _price_error(e):
    status = getattr(e, "status", None)
    if status in (401, 403):
        return PriceError("Tiingo refused the key", "auth", status)
    if status == 404:
        return PriceError("Tiingo does not know this symbol", "not_found", status)
    if status == 429:
        return PriceError("Tiingo rate limit", "rate", status)
    return PriceError(e.reason if isinstance(e, FetchError) else "connection error", "network", status)


def fetch_bars(http, ticker, api_key, start, end=None):
    """Bars from start (a date) to end (a date or None for the latest)."""
    if not isinstance(ticker, str) or not TICKER_RE.match(ticker):
        raise ValueError("not a ticker")
    api_key = str(api_key or "").strip()
    if not api_key or any(c in api_key for c in "\r\n\x00"):
        raise PriceError("no Tiingo key", "auth")
    params = {"startDate": start.isoformat()}
    if end is not None:
        params["endDate"] = end.isoformat()
    params["format"] = "json"
    url = TIINGO_PRICES.format(ticker=urllib.parse.quote(ticker, safe="")) + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    try:
        resp = http.get(url, headers=headers)
    except PriceError:
        raise
    except FetchError as e:
        raise _price_error(e) from None
    try:
        doc = resp.json()
    except ValueError:
        raise PriceError("Tiingo sent something that is not JSON", "bad_data") from None
    if isinstance(doc, dict):
        # Tiingo answers some errors with 200 and {"detail": "..."}; never pass the text on.
        detail = str(doc.get("detail") or "").lower()
        if "token" in detail or "auth" in detail or "permission" in detail:
            raise PriceError("Tiingo refused the key", "auth")
        if "not found" in detail:
            raise PriceError("Tiingo does not know this symbol", "not_found")
        if "limit" in detail or "rate" in detail:
            raise PriceError("Tiingo rate limit", "rate")
        raise PriceError("Tiingo sent an unexpected answer", "bad_data")
    try:
        return parse_tiingo(doc)
    except ValueError:
        raise PriceError("Tiingo sent an unexpected answer", "bad_data") from None


# ---------------------------------------------------------------- cache

def cache_from_bars(bars, fetched_at):
    """The compact cache kept in the ticker's state (rows of FIELDS)."""
    rows = [[b[f] for f in FIELDS] for b in bars]
    return {
        "fields": list(FIELDS),
        "rows": rows,
        "first_date": bars[0]["date"] if bars else None,
        "last_date": bars[-1]["date"] if bars else None,
        "fetched_at": fetched_at,
    }


def bars_from_cache(cache):
    """Bars from a cache; ValueError when the cache is malformed."""
    if not isinstance(cache, dict) or cache.get("fields") != FIELDS or not isinstance(cache.get("rows"), list):
        raise ValueError("the price cache is malformed")
    bars, last = [], None
    for row in cache["rows"]:
        if not isinstance(row, list) or len(row) != len(FIELDS):
            raise ValueError("the price cache is malformed")
        b = dict(zip(FIELDS, row))
        day = _valid_day(b["date"])
        if day is None or day != b["date"] or (last is not None and day <= last):
            raise ValueError("the price cache is malformed")
        for k in ("open", "high", "low", "close", "volume", "raw_close", "div", "split"):
            if _num(b[k]) is None:
                raise ValueError("the price cache is malformed")
            b[k] = float(b[k])
        if b["close"] <= 0:
            raise ValueError("the price cache is malformed")
        if isinstance(b["raw_volume"], bool) or _num(b["raw_volume"]) is None:
            raise ValueError("the price cache is malformed")
        b["raw_volume"] = int(b["raw_volume"])
        bars.append(b)
        last = day
    if not bars:
        raise ValueError("the price cache is empty")
    return bars


def _ny_today(now):
    return trading_days.ny_time(now).date()


def full_start(now, years):
    """The first day of a full fetch: today (New York) minus years * 366 + 300 days."""
    return _ny_today(now) - timedelta(days=int(years) * 366 + WARMUP_DAYS)


def max_bars(years):
    return int(years) * 252 + WARMUP_DAYS


def _needs_full(cached_by_date, fresh):
    """Why a delta cannot be merged (Tiingo re-adjusted the history), or None."""
    for b in fresh:
        old = cached_by_date.get(b["date"])
        if b["div"] != 0 or b["split"] != 1:
            if old is None or old["div"] != b["div"] or old["split"] != b["split"]:
                return "dividend or split"
        if old is not None and old["close"] > 0:
            if abs(b["close"] - old["close"]) / old["close"] > REFETCH_TOLERANCE:
                return "adjusted prices changed"
    return None


def update_cache(cache, http, ticker, api_key, now, years, data_ready="18:00"):
    """(new cache, full bar history, info) for one ticker.

    info: {"mode": "full" | "delta" | "cached", "rows": n, "added": new dates,
    "reason": why a full fetch ran, or None}. Raises PriceError when Tiingo
    fails; the caller can then fall back to bars_from_cache(cache)."""
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    fetched_at = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    keep = max_bars(years)

    old_bars, reason = None, None
    if cache is None:
        reason = "no cache"
    else:
        try:
            old_bars = bars_from_cache(cache)
        except ValueError:
            reason = "cache unreadable"

    if old_bars is not None:
        last_date = old_bars[-1]["date"]
        if last_date >= trading_days.last_closed_day(now, data_ready).isoformat():
            bars = old_bars[-keep:]
            return cache_from_bars(bars, cache.get("fetched_at") or fetched_at), bars, {
                "mode": "cached", "rows": len(bars), "added": 0, "reason": None}
        start = date.fromisoformat(last_date) - timedelta(days=DELTA_DAYS)
        fresh = fetch_bars(http, ticker, api_key, start)
        by_date = {b["date"]: b for b in old_bars}
        reason = _needs_full(by_date, fresh)
        if reason is None:
            added = 0
            for b in fresh:
                if b["date"] not in by_date:
                    added += 1
                by_date[b["date"]] = b
            bars = [by_date[d] for d in sorted(by_date)][-keep:]
            return cache_from_bars(bars, fetched_at), bars, {
                "mode": "delta", "rows": len(bars), "added": added, "reason": None}

    bars = fetch_bars(http, ticker, api_key, full_start(now, years))[-keep:]
    if not bars:
        raise PriceError("Tiingo has no prices for this symbol", "not_found")
    known = {b["date"] for b in old_bars or ()}
    added = sum(1 for b in bars if b["date"] not in known)
    return cache_from_bars(bars, fetched_at), bars, {
        "mode": "full", "rows": len(bars), "added": added, "reason": reason}


def spy_bars(http, api_key, now, years, symbol="SPY"):
    """The market benchmark over the same window as a full fetch (fetched every
    run, never cached in a ticker's state)."""
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return fetch_bars(http, symbol, api_key, full_start(now, years))[-max_bars(years):]
