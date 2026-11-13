"""Technical indicators on adjusted daily bars (contract 6.1, 8.2, 8.3).

Every series function returns a list as long as its input, with None during
warm-up or where an input is missing. Bars are oldest first.
"""
import math

from stocks import round_pct, round_price
from stocks.analysis import num

REL_PERIODS = (20, 60, 252)
RANGE_DAYS = 252
CROSS_WINDOW = 10


# ---------------------------------------------------------------- series

def sma(values, n):
    """sma[i] = mean of values[i-n+1 .. i], from index n-1; None when any is missing."""
    if n < 1:
        raise ValueError("n must be at least 1")
    out = [None] * len(values)
    missing = 0
    for i, v in enumerate(values):
        if v is None:
            missing += 1
        if i >= n and values[i - n] is None:
            missing -= 1
        if i >= n - 1 and missing == 0:
            out[i] = math.fsum(values[i - n + 1:i + 1]) / n
    return out


def ema(values, n):
    """Seeded with the SMA of the first n defined values in a row, then
    e = e_prev + (x - e_prev) * 2 / (n + 1). A missing value after the seed
    gives None for that index and leaves the average unchanged."""
    if n < 1:
        raise ValueError("n must be at least 1")
    out = [None] * len(values)
    alpha = 2.0 / (n + 1)
    e = None
    run = 0
    for i, v in enumerate(values):
        if e is None:
            if v is None:
                run = 0
                continue
            run += 1
            if run == n:
                e = math.fsum(values[i - n + 1:i + 1]) / n
                out[i] = e
        elif v is not None:
            e = e + (v - e) * alpha
            out[i] = e
    return out


def _rsi_value(gain, loss):
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def rsi(closes, n=14):
    """Wilder's RSI: the first average gain and loss are the simple means of
    the first n changes (value at index n), then avg = (avg_prev * (n - 1) + x) / n."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = _rsi_value(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_value(ag, al)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """{"macd", "signal", "hist"}: ema(fast) - ema(slow), its EMA(signal)
    seeded on its first defined values, and their difference."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}


def bollinger(closes, n=20, k=2.0):
    """{"mid", "upper", "lower", "pct_b", "bandwidth_pct"} with the SMA as the
    middle band and the population standard deviation of the same window."""
    mid = sma(closes, n)
    size = len(closes)
    upper, lower, pct_b, bw = [None] * size, [None] * size, [None] * size, [None] * size
    for i, m in enumerate(mid):
        if m is None:
            continue
        window = closes[i - n + 1:i + 1]
        sd = math.sqrt(math.fsum((x - m) * (x - m) for x in window) / n)
        up, lo = m + k * sd, m - k * sd
        upper[i], lower[i] = up, lo
        if up > lo:
            pct_b[i] = (closes[i] - lo) / (up - lo)
        if m:
            bw[i] = (up - lo) / m * 100.0
    return {"mid": mid, "upper": upper, "lower": lower, "pct_b": pct_b, "bandwidth_pct": bw}


def true_range(bars):
    out = []
    for i, b in enumerate(bars):
        h, l = b["high"], b["low"]
        if i == 0:
            out.append(h - l)
        else:
            c = bars[i - 1]["close"]
            out.append(max(h - l, abs(h - c), abs(l - c)))
    return out


def atr(bars, n=14):
    """Wilder's average true range; the first value at index n-1 is the mean
    of the first n true ranges (the first bar's is high - low)."""
    tr = true_range(bars)
    out = [None] * len(tr)
    if len(tr) < n:
        return out
    a = math.fsum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def log_return(values, n):
    """log(values[i] / values[i-n]) from index n; None when either is missing or not positive."""
    out = [None] * len(values)
    for i in range(n, len(values)):
        a, b = values[i], values[i - n]
        if a is not None and b is not None and a > 0 and b > 0:
            out[i] = math.log(a / b)
    return out


# ---------------------------------------------------------------- helpers

def clean_bars(bars):
    """Bars with a positive close, sorted by date, one per date. A missing
    open, high or low falls back to the close, a missing volume to 0."""
    by_date = {}
    for b in bars or []:
        if not isinstance(b, dict) or not isinstance(b.get("date"), str):
            continue
        c = num(b.get("close"))
        if c is None or c <= 0:
            continue
        h, l, o = num(b.get("high")), num(b.get("low")), num(b.get("open"))
        h = c if h is None or h <= 0 else h
        l = c if l is None or l <= 0 else l
        row = dict(b)
        row.update({"date": b["date"][:10], "close": c, "high": max(h, c, l), "low": min(l, c, h), "open": c if o is None else o,
                    "volume": num(b.get("volume")) or 0.0})
        by_date[b["date"][:10]] = row
    return [by_date[d] for d in sorted(by_date)]


def _last(values):
    return values[-1] if values else None


def _r(x, digits):
    x = num(x)
    return None if x is None else round(x, digits)


def _pct_change(a, b):
    """Percent change from b to a."""
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def relative_strength(bars, spy):
    """Percent returns over 20, 60 and 252 trading days for the stock and for
    the benchmark over the same dates, and the stock's excess."""
    out = {"vs": "SPY"}
    spy_close = {}
    for b in clean_bars(spy or []):
        spy_close[b["date"]] = b["close"]
    last = len(bars) - 1
    for n in REL_PERIODS:
        ret = spy_ret = excess = None
        if last - n >= 0:
            ret = _pct_change(bars[last]["close"], bars[last - n]["close"])
            s1, s0 = spy_close.get(bars[last]["date"]), spy_close.get(bars[last - n]["date"])
            spy_ret = _pct_change(s1, s0)
            if ret is not None and spy_ret is not None:
                excess = ret - spy_ret
        out[f"ret_{n}d_pct"] = round_pct(ret)
        out[f"spy_ret_{n}d_pct"] = round_pct(spy_ret)
        out[f"excess_{n}d_pct"] = round_pct(excess)
    return out


def range_52w(bars, days=RANGE_DAYS):
    window = bars[-days:]
    if not window:
        return {"high": None, "low": None, "high_date": None, "low_date": None, "from_high_pct": None,
                "from_low_pct": None, "days": 0}
    hi = max(window, key=lambda b: b["high"])          # max() keeps the earliest on ties
    lo = min(window, key=lambda b: b["low"])
    close = bars[-1]["close"]
    return {"high": round_price(hi["high"]), "low": round_price(lo["low"]),
            "high_date": hi["date"], "low_date": lo["date"],
            "from_high_pct": round_pct(_pct_change(close, hi["high"])),
            "from_low_pct": round_pct(_pct_change(close, lo["low"])), "days": len(window)}


def macd_cross(hist, window=CROSS_WINDOW):
    """The latest sign change of the histogram within the last `window` bars:
    ("bullish" | "bearish", bars ago), or (None, None)."""
    last = len(hist) - 1
    for k in range(last, max(0, last - window + 1) - 1, -1):
        if k < 1:
            break
        a, b = hist[k - 1], hist[k]
        if a is None or b is None:
            continue
        if b > 0 >= a:
            return "bullish", last - k
        if b < 0 <= a:
            return "bearish", last - k
    return None, None


def trend(close, s50, s200, n_bars):
    if s50 is None:
        return None
    if n_bars < 200 or s200 is None:
        return "up" if close > s50 else "down" if close < s50 else "sideways"
    if close > s50 > s200:
        return "up"
    if close < s50 < s200:
        return "down"
    return "sideways"


# ---------------------------------------------------------------- blocks

def technicals(bars, spy):
    """The Technicals block (8.3) for the last bar."""
    bars = clean_bars(bars)
    if not bars:
        raise ValueError("no bars")
    closes = [b["close"] for b in bars]
    close = closes[-1]
    s = {n: _last(sma(closes, n)) for n in (20, 50, 200)}
    e = {n: _last(ema(closes, n)) for n in (12, 26, 50)}
    m = macd(closes)
    cross, ago = macd_cross(m["hist"])
    bb = bollinger(closes)
    a14 = _last(atr(bars, 14))
    vols = [b["volume"] for b in bars]
    avg20, avg50 = _last(sma(vols, 20)), _last(sma(vols, 50))
    pct_b = _last(bb["pct_b"])
    return {
        "as_of": bars[-1]["date"],
        "close": round_price(close),
        "sma": {str(n): round_price(v) for n, v in s.items()},
        "ema": {str(n): round_price(v) for n, v in e.items()},
        "distance_pct": {f"sma{n}": round_pct(_pct_change(close, v)) for n, v in s.items()},
        "rsi14": _r(_last(rsi(closes, 14)), 2),
        "macd": {"macd": round_price(_last(m["macd"])), "signal": round_price(_last(m["signal"])),
                 "hist": round_price(_last(m["hist"])), "cross": cross, "cross_days_ago": ago},
        "bollinger": {"mid": round_price(_last(bb["mid"])), "upper": round_price(_last(bb["upper"])),
                      "lower": round_price(_last(bb["lower"])), "pct_b": _r(pct_b, 4),
                      "bandwidth_pct": round_pct(_last(bb["bandwidth_pct"]))},
        "atr14": round_price(a14),
        "atr_pct": round_pct(None if a14 is None else a14 / close * 100.0),
        "range_52w": range_52w(bars),
        "rel_strength": relative_strength(bars, spy),
        "volume": {"last": _r(vols[-1], 0), "avg20": _r(avg20, 0), "avg50": _r(avg50, 0),
                   "ratio20": _r(None if not avg20 else vols[-1] / avg20, 2)},
        "trend": trend(close, s[50], s[200], len(bars)),
        "bars": len(bars),
    }


def chart(bars, days=400):
    """The Chart block (8.2): the last `days` bars with indicators computed on
    the whole history first, so the first values are filled when history allows."""
    bars = clean_bars(bars)
    closes = [b["close"] for b in bars]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    r14 = rsi(closes, 14)
    start = max(0, len(bars) - days)
    sl = slice(start, len(bars))
    return {
        "dates": [b["date"] for b in bars[sl]],
        "open": [round_price(b["open"]) for b in bars[sl]],
        "high": [round_price(b["high"]) for b in bars[sl]],
        "low": [round_price(b["low"]) for b in bars[sl]],
        "close": [round_price(b["close"]) for b in bars[sl]],
        "volume": [_r(b["volume"], 0) for b in bars[sl]],
        "sma20": [round_price(v) for v in s20[sl]],
        "sma50": [round_price(v) for v in s50[sl]],
        "sma200": [round_price(v) for v in s200[sl]],
        "rsi14": [_r(v, 2) for v in r14[sl]],
    }
