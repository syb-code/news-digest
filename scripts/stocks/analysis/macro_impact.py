"""The stock's own measured sensitivity to macro moves (contract 6.4, 8.8).

Regressions use the stock's daily percent returns, the market's (SPY) return
on the same day and the factor's daily change, so a factor coefficient is the
move left after the market's move. Event studies take the close-to-close
move on FOMC statement days and CPI release days.
"""
import math

from stocks import round_pct
from stocks.analysis import inverse, mean, median, num, sstdev
from stocks.analysis.indicators import clean_bars

FACTORS = [  # id, series, label, kind, unit size, unit label
    ("dgs10", "DGS10", "10 year yield", "diff", 0.10, "per 0.10 point rise"),
    ("dgs2", "DGS2", "2 year yield", "diff", 0.10, "per 0.10 point rise"),
    ("dollar", "DTWEXBGS", "Dollar index (broad)", "pct", 1.0, "per 1 percent rise"),
    ("oil", "DCOILWTICO", "WTI oil", "pct", 1.0, "per 1 percent rise"),
    ("vix", "VIXCLS", "VIX", "diff", 1.0, "per 1 point rise"),
]
WINDOW = 504
EVENT_WINDOW = 756
MIN_N = 120
MIN_EVENTS = 4
NOT_ENOUGH = "not enough history"
NOTE_PAST = ("Sensitivities are measured on this stock's own daily moves, after the market's move. "
             "They describe the past, not the future.")
NOTE_BANDS = ("Strength of evidence: strong when the estimate is at least three standard errors from zero, "
              "clear at two, weak at one, otherwise no clear link.")
NOTE_EVENTS = ("Event days use the close-to-close move: CPI comes out at 8:30 and the FOMC statement at 2:00 "
               "in New York, both inside that day's move.")


def _points(points):
    out = {}
    for p in points or []:
        try:
            d, v = p[0], num(p[1])
        except (TypeError, IndexError, KeyError):
            continue
        if isinstance(d, str) and v is not None:
            out[d[:10]] = v
    return sorted(out.items())


def daily_returns(bars):
    """{date: percent return close to close} from the second bar on."""
    bars = clean_bars(bars)
    return {bars[i]["date"]: (bars[i]["close"] / bars[i - 1]["close"] - 1.0) * 100.0 for i in range(1, len(bars))}


def factor_changes(points, kind):
    """{date: change from the previous observation}: "diff" is the difference,
    "pct" the percent change."""
    pts = _points(points)
    out = {}
    for (_, a), (d, b) in zip(pts, pts[1:]):
        if kind == "pct":
            if a:
                out[d] = (b / a - 1.0) * 100.0
        else:
            out[d] = b - a
    return out


def ols(y, xs):
    """Least squares of y on the regressors in xs (a list of columns, each as
    long as y) with an intercept first. Returns {"coef", "se", "t", "r2", "n"}
    where coef, se and t start with the intercept; classical standard errors."""
    n = len(y)
    cols = [[1.0] * n] + [list(c) for c in xs]
    p = len(cols)
    if any(len(c) != n for c in cols):
        raise ValueError("columns differ in length")
    if n <= p:
        raise ValueError("not enough observations")
    xtx = [[math.fsum(a * b for a, b in zip(cols[i], cols[j])) for j in range(p)] for i in range(p)]
    xty = [math.fsum(a * b for a, b in zip(cols[i], y)) for i in range(p)]
    inv = inverse(xtx)
    coef = [math.fsum(inv[i][j] * xty[j] for j in range(p)) for i in range(p)]
    fitted = [math.fsum(coef[j] * cols[j][k] for j in range(p)) for k in range(n)]
    resid = [a - b for a, b in zip(y, fitted)]
    ssr = math.fsum(r * r for r in resid)
    ybar = mean(y)
    sst = math.fsum((v - ybar) * (v - ybar) for v in y)
    sigma2 = ssr / (n - p)
    se = [math.sqrt(max(inv[i][i] * sigma2, 0.0)) for i in range(p)]
    t = [None if s == 0 else c / s for c, s in zip(coef, se)]
    r2 = None if sst == 0 else 1.0 - ssr / sst
    return {"coef": coef, "se": se, "t": t, "r2": r2, "n": n}


def band(t):
    """Plain words for how far an estimate is from zero, in standard errors."""
    if t is None:
        return "no clear link"
    a = abs(t)
    if a >= 3:
        return "strong"
    if a >= 2:
        return "clear"
    if a >= 1:
        return "weak"
    return "no clear link"


NOUNS = {"dgs10": "10 year yield", "dgs2": "2 year yield", "dollar": "broad dollar index",
         "oil": "WTI oil price", "vix": "VIX"}


def _sentence(fid, unit, move, b):
    noun = NOUNS.get(fid, fid)
    if move is None or b == "no clear link":
        return f"No clear link to the {noun} after the market's move."
    direction = "rise" if move > 0 else "fall"
    return (f"Tends to {direction} about {abs(move):.2f} percent {unit} in the {noun}, "
            f"after the market's move ({b} evidence).")


def sensitivities(bars, spy, fred, window=WINDOW, min_n=MIN_N):
    """{"market_beta": {...} | None, "factors": [...], "n_days": int}."""
    r = daily_returns(bars)
    m = daily_returns(spy or [])
    both = sorted(d for d in r if d in m)[-window:]
    beta = None
    if len(both) >= min_n:
        try:
            fit = ols([r[d] for d in both], [[m[d] for d in both]])
        except ValueError:
            fit = None
        if fit is not None:
            beta = {"beta": round(fit["coef"][1], 2), "t": None if fit["t"][1] is None else round(fit["t"][1], 1),
                    "n": fit["n"], "r2": None if fit["r2"] is None else round(fit["r2"], 3)}
    factors = []
    fred = fred or {}
    for fid, series, label, kind, unit, unit_label in FACTORS:
        row = {"id": fid, "label": label, "unit": unit_label, "move_pct": None, "se_pct": None, "low_pct": None,
               "high_pct": None, "t": None, "n": 0, "band": None, "text": None}
        ch = factor_changes(fred.get(series) or [], kind)
        if not ch:
            row.update({"status": "not_available", "text": f"No {NOUNS[fid]} data today."})
            factors.append(row)
            continue
        dates = sorted(d for d in r if d in m and d in ch)[-window:]
        row["n"] = len(dates)
        if len(dates) < min_n:
            row.update({"status": NOT_ENOUGH,
                        "text": f"Not enough history yet: {len(dates)} days, at least {min_n} needed."})
            factors.append(row)
            continue
        try:
            fit = ols([r[d] for d in dates], [[m[d] for d in dates], [ch[d] for d in dates]])
        except ValueError:
            row.update({"status": "not_available", "text": f"The {NOUNS[fid]} did not move enough to measure."})
            factors.append(row)
            continue
        move, se, t = fit["coef"][2] * unit, fit["se"][2] * unit, fit["t"][2]
        b = band(t)
        row.update({"status": "ok", "move_pct": round(move, 3), "se_pct": round(se, 3),
                    "low_pct": round(move - 1.96 * se, 3), "high_pct": round(move + 1.96 * se, 3),
                    "t": None if t is None else round(t, 1), "band": b, "text": _sentence(fid, unit_label, move, b)})
        factors.append(row)
    return {"market_beta": beta, "factors": factors, "n_days": len(both)}


def event_moves(bars, dates, label, window=EVENT_WINDOW):
    """The stock's close-to-close move on event days among its last `window`
    trading days, against the median absolute move on all other days."""
    bars = clean_bars(bars)
    r = daily_returns(bars)
    days = [b["date"] for b in bars][-window:]
    in_window = set(days)
    events = sorted({d for d in (dates or []) if isinstance(d, str) and d in in_window and d in r})
    moves = [r[d] for d in events]
    event_set = set(events)
    others = [abs(r[d]) for d in days if d in r and d not in event_set]
    normal = median(others) if others else None
    out = {"event": label, "n": len(moves), "avg_move_pct": None, "avg_abs_move_pct": None,
           "std_move_pct": None, "up_share": None, "normal_abs_move_pct": round_pct(normal), "ratio": None,
           "last": None, "status": "ok" if len(moves) >= MIN_EVENTS else NOT_ENOUGH}
    if moves:
        avg_abs = mean([abs(v) for v in moves])
        sd = sstdev(moves)
        out.update({"avg_move_pct": round_pct(mean(moves)), "avg_abs_move_pct": round_pct(avg_abs),
                    "std_move_pct": round_pct(sd), "up_share": round(sum(1 for v in moves if v > 0) / len(moves), 4),
                    "ratio": None if not normal else round(avg_abs / normal, 2),
                    "last": {"date": events[-1], "move_pct": round_pct(moves[-1])}})
    return out


def macro_impact(bars, spy, fred, history):
    """The MacroImpact block (8.8). history is {"fomc": [dates], "cpi": [dates]}."""
    clean = clean_bars(bars)
    sens = sensitivities(clean, spy, fred)
    history = history or {}
    events = [event_moves(clean, history.get("fomc") or [], "FOMC"),
              event_moves(clean, history.get("cpi") or [], "CPI")]
    notes = [NOTE_PAST, NOTE_BANDS, NOTE_EVENTS]
    if sens["market_beta"] is None:
        status = "not_available" if not spy else NOT_ENOUGH
        if status == NOT_ENOUGH:
            notes.insert(0, f"Low confidence, {len(clean)} days of history: the measurements need at least "
                            f"{MIN_N} days in common with the market.")
        else:
            notes.insert(0, "The market series could not be read today.")
    else:
        status = "ok"
        if len(clean) < WINDOW:
            notes.insert(0, f"Low confidence, {len(clean)} days of history.")
    return {"status": status, "window_days": WINDOW, "market_beta": sens["market_beta"],
            "factors": sens["factors"], "events": events, "notes": notes}
