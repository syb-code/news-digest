"""Support and resistance, the candidate price list and the code's plan
(contract 6.2, 8.4).

The candidates are the only prices a plan may use: the model's entry zones,
targets and stop are checked against them with `match_candidate`, and the
fallback plan is built from them alone.
"""
from stocks import round_price
from stocks.analysis import num
from stocks.analysis.indicators import clean_bars

SUPPORT_SHARE = 0.995
RESIST_SHARE = 1.005
MAX_LEVELS = 4
TOL_PCT = 0.5          # percent of the candidate price
TOL_ATR_SHARE = 0.10   # share of ATR14
QUANTILE_WORDS = {
    "dd": {"25": ("Deep", "25th percentile"), "50": ("Typical", "median"), "75": ("Shallow", "75th percentile")},
    "ru": {"25": ("Small", "25th percentile"), "50": ("Typical", "median"), "75": ("Large", "75th percentile")},
}
TARGET_KINDS = ("resistance", "range_52w", "bollinger", "runup_quantile", "atr_band")
TARGET_IDS = {"range_52w": ("hi52",), "bollinger": ("bb_upper",), "atr_band": ("atr_p2",)}
RATIONALE_ENTRY = "Two to one ATR below the last close."
RATIONALE_TARGET = "The nearest level at least one ATR above the close."
RATIONALE_STOP = "Three ATR below the last close."


def pivots(bars, lookback=252, k=5):
    """Swing pivots among the last `lookback` bars: [(date, price, "high"|"low")].
    A pivot high at i has the highest high of bars i-k .. i+k (the earliest
    wins a tie); the first k bars of the history and the last k bars cannot be pivots."""
    n = len(bars)
    out = []
    for i in range(max(k, n - lookback), n - k):
        window = bars[i - k:i + k + 1]
        hi = bars[i]["high"]
        if hi >= max(b["high"] for b in window) and all(b["high"] < hi for b in bars[i - k:i]):
            out.append((bars[i]["date"], hi, "high"))
        lo = bars[i]["low"]
        if lo <= min(b["low"] for b in window) and all(b["low"] > lo for b in bars[i - k:i]):
            out.append((bars[i]["date"], lo, "low"))
    return out


def cluster(points, width):
    """Clusters of (date, price, kind) sorted by price: a price joins the
    current cluster when it is within `width` of the cluster mean."""
    clusters = []
    for p in sorted(points, key=lambda t: (t[1], t[0])):
        if clusters:
            c = clusters[-1]
            if abs(p[1] - c["sum"] / c["n"]) <= width:
                c["sum"] += p[1]
                c["n"] += 1
                c["last"] = max(c["last"], p[0])
                continue
        clusters.append({"sum": p[1], "n": 1, "last": p[0]})
    return [{"price": c["sum"] / c["n"], "touches": c["n"], "last_touch": c["last"]} for c in clusters]


def find_levels(bars, atr14, lookback=252, k=5):
    """{"supports": [...], "resistances": [...]}: pivot clusters below and
    above the close, nearest first, at most four each."""
    bars = clean_bars(bars)
    if not bars:
        return {"supports": [], "resistances": []}
    close = bars[-1]["close"]
    a = num(atr14) or 0.0
    width = max(0.6 * a, 0.01 * close)
    levels = cluster(pivots(bars, lookback, k), width)
    below = sorted((c for c in levels if c["price"] < close * SUPPORT_SHARE), key=lambda c: -c["price"])
    above = sorted((c for c in levels if c["price"] > close * RESIST_SHARE), key=lambda c: c["price"])

    def rows(items, prefix):
        return [{"id": f"{prefix}{i + 1}", "price": round_price(c["price"]), "touches": c["touches"],
                 "last_touch": c["last_touch"]} for i, c in enumerate(items[:MAX_LEVELS])]

    return {"supports": rows(below, "s"), "resistances": rows(above, "r")}


def _touches(n):
    return "1 touch" if n == 1 else f"{n} touches"


def candidates(bars, tech, levels, excursions):
    """Every price a plan may use, as {"id", "kind", "label", "price"}."""
    out = []

    def add(id_, kind, label, price):
        p = round_price(price)
        if p is not None and p > 0:
            out.append({"id": id_, "kind": kind, "label": label, "price": p})

    for i, s in enumerate(levels.get("supports") or []):
        add(s["id"], "support", f"Support {i + 1}, {_touches(s['touches'])}", s["price"])
    for i, r in enumerate(levels.get("resistances") or []):
        add(r["id"], "resistance", f"Resistance {i + 1}, {_touches(r['touches'])}", r["price"])
    sma = tech.get("sma") or {}
    for n in ("20", "50", "200"):
        add(f"sma{n}", "moving_average", f"{n} day average", sma.get(n))
    bb = tech.get("bollinger") or {}
    add("bb_upper", "bollinger", "Upper Bollinger band", bb.get("upper"))
    add("bb_lower", "bollinger", "Lower Bollinger band", bb.get("lower"))
    close, a = num(tech.get("close")), num(tech.get("atr14"))
    if close is not None and a is not None and a > 0:
        for m in (1, 2, 3):
            add(f"atr_m{m}", "atr_band", f"Close minus {m} ATR", close - m * a)
        for m in (1, 2, 3):
            add(f"atr_p{m}", "atr_band", f"Close plus {m} ATR", close + m * a)
    rng = tech.get("range_52w") or {}
    add("hi52", "range_52w", "52 week high", rng.get("high"))
    add("lo52", "range_52w", "52 week low", rng.get("low"))
    if excursions and close is not None:
        h = excursions.get("horizon", 20)
        where = " in similar setups" if excursions.get("scope") == "similar" else ""
        for key, kind, prefix, noun in (("drawdown_q", "drawdown_quantile", "dd", "dip"),
                                         ("runup_q", "runup_quantile", "ru", "rise")):
            qs = excursions.get(key) or {}
            for q in ("25", "50", "75"):
                v = num(qs.get(q))
                if v is None:
                    continue
                adj, words = QUANTILE_WORDS[prefix][q]
                add(f"{prefix}{q}", kind, f"{adj} {h} day {noun}{where} ({words})", close * (1.0 + v))
    return out


def tolerance(price, atr14):
    """How far a price may be from a candidate and still match it."""
    a = num(atr14)
    if a is None or a <= 0:
        return 0.01 * price
    return max(TOL_PCT / 100.0 * price, TOL_ATR_SHARE * a)


def match_candidate(price, cands, atr14):
    """The closest candidate within its tolerance of price, or None (the
    first in list order wins a tie)."""
    p = num(price)
    if p is None:
        return None
    best, best_d = None, None
    for c in cands:
        d = abs(p - c["price"])
        if d <= tolerance(c["price"], atr14) and (best_d is None or d < best_d):
            best, best_d = c, d
    return best


def fallback_plan(cands, close, atr14):
    """The code's plan from candidates only: entry zone from close minus two
    ATR to close minus one ATR, the nearest level at least one ATR above the
    close as the target (20 days), close minus three ATR as the stop. Empty
    lists and a null stop when there is no ATR yet (fewer than 14 bars)."""
    by_id = {c["id"]: c for c in cands}
    lo, hi, st = by_id.get("atr_m2"), by_id.get("atr_m1"), by_id.get("atr_m3")
    a, c = num(atr14), num(close)
    if not (lo and hi and st and a and c):
        return {"entry_zones": [], "targets": [], "stop": None, "source": "code"}
    eligible = []
    for cand in cands:
        if cand["kind"] not in TARGET_KINDS:
            continue
        only = TARGET_IDS.get(cand["kind"])
        if only and cand["id"] not in only:
            continue
        if cand["price"] >= c + a:
            eligible.append(cand)
    targets = []
    if eligible:
        t = min(eligible, key=lambda x: (x["price"], x["id"]))
        targets.append({"price": t["price"], "horizon_days": 20, "rationale": RATIONALE_TARGET, "ref": t["id"]})
    return {
        "entry_zones": [{"low": lo["price"], "high": hi["price"], "rationale": RATIONALE_ENTRY,
                         "low_ref": lo["id"], "high_ref": hi["id"]}],
        "targets": targets,
        "stop": {"price": st["price"], "rationale": RATIONALE_STOP, "ref": st["id"]},
        "source": "code",
    }


def levels_block(bars, tech, excursions):
    """The Levels block (8.4)."""
    a = tech.get("atr14")
    lv = find_levels(bars, a)
    return {"supports": lv["supports"], "resistances": lv["resistances"],
            "candidates": candidates(bars, tech, lv, excursions), "atr14": a,
            "tolerance": {"pct": TOL_PCT, "atr_share": TOL_ATR_SHARE}}
