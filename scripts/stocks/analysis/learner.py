"""The per-ticker learner (contract 6.5, 7 and 8.9).

Each ticker learns alone. Every function that takes a learning state takes
exactly one, together with that ticker's own bars and short volume and the
shared read-only market inputs (SPY bars, FRED series). Shared inputs are
never written into a state, nothing is cached at module level, and every
function returns a new state that shares no object with its input.

The model is an L2-regularized logistic regression per horizon (5 and 20
trading days by default): will the close h bars ahead be above today's close?
It is fitted with Newton steps in pure Python. On the first day, when the
feature list changes, and about once a quarter, a walk forward backtest over
the ticker's own history gives an out-of-sample scorecard and picks how
strongly that ticker's model is regularized (LAMBDAS: the most cautious
value stands until a weaker one has proved better out of sample). Every
trading day the learner grades the calls whose horizon has passed, refits
from the previous weights, logs a new call and recomputes the live
scorecard. A ticker with too little history predicts its own base rate,
shrunk toward one half, and says so.
"""
import bisect
import copy
import math
import operator
import re
from datetime import date

from stocks import STATE_VERSION, plain, round_pct, round_price, size_of
from stocks.analysis import mean, median, num, quantile, solve, sstdev
from stocks.analysis.indicators import atr, bollinger, clean_bars, log_return, macd, rsi, sma
from stocks.analysis.shorts import ratio_series

FEATURES = ("ret_5", "ret_20", "ret_60", "dist_sma20", "dist_sma50", "dist_sma200", "rsi14",
            "macd_hist", "pct_b", "atr_pct", "vol_ratio", "rs_spy_20", "spy_ret_5",
            "d_dgs10_5", "vix_rel", "short_ratio_z")
STATE_KEYS = ("kind", "version", "ticker", "created", "updated", "identity", "caches", "features", "model",
              "backtest", "predictions", "scorecard", "lessons", "last_as_of", "last_run")
DEFAULTS = {"horizons": [5, 20], "bootstrap_years": 5, "min_train_rows": 150, "refit_every": 21,
            "lessons_cap": 12, "insider_days": 365, "congress_days": 365}

LAMBDA = 1.0               # the default of fit(); the learner picks its lambda from LAMBDAS
LAMBDAS = (100.0, 1000.0, 10000.0)
REBOOT_BARS = 63           # a fresh backtest and lambda choice about once a quarter
MIN_CHOICE_N = 120         # out-of-sample outcomes needed before a less cautious lambda is chosen
MAX_ITER = 25
STEP_TOL = 1e-8
STD_FLOOR = 1e-8
P_MIN, P_MAX = 0.02, 0.98
PRIOR_MEAN = 0.5            # the base rate before any history
PRIOR_STRENGTH = 20.0       # pseudo-observations behind the prior while warming up
SMA200_MIN_BARS = 260
SMA200_MIN_ROWS = 504       # labeled rows dist_sma200 must leave (two years)
SHORT_MIN_ROWS = 250
SHORT_WINDOW = 60
SHORT_MIN_N = 40
FRED_MAX_GAP_DAYS = 10
EXCURSION_HORIZON = 20
EXCURSION_MIN_N = 120
ASSESS_HORIZON = 20
LOW_CONFIDENCE_BARS = 504
KEEP_OLD_GRADED = 400
SHORT_KEEP = 300
LESSONS_PER_RUN = 2
LESSON_CHARS = 200
BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))
CORPORATE_WORDS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
                   "llc", "lp", "l", "p", "holdings", "holding", "group", "sa", "s", "a", "nv", "n", "v", "ag",
                   "se", "spa", "the", "class", "cl", "com", "de", "new"}


class IsolationError(ValueError):
    """A state that is not this ticker's, or that holds another ticker's data."""


# ---------------------------------------------------------------- configuration and state

def _cfg(cfg):
    c = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if k in DEFAULTS and v is not None:
            c[k] = v
    hs = sorted({int(h) for h in c["horizons"] if int(h) > 0})
    if not hs:
        raise ValueError("No horizons.")
    c["horizons"] = hs
    for k in ("bootstrap_years", "min_train_rows", "refit_every", "lessons_cap", "insider_days", "congress_days"):
        c[k] = max(1, int(c[k]))
    return c


def _empty_assessments():
    return {"n": 0, "stance_hit_rate": None, "target": 0, "stop": 0, "neither": 0}


def _empty_model():
    return {"kind": "base_rate", "status": "warming_up", "horizons": [], "params": {}, "excursions": None,
            "training": {"rows": 0, "first_date": None, "last_date": None, "refits": 0, "bars": 0}}


def new_state(ticker, now):
    """An empty state for ticker; now is an ISO timestamp string."""
    return {"kind": "stocks.state", "version": STATE_VERSION, "ticker": ticker, "created": now, "updated": now,
            "identity": {}, "caches": {}, "features": [], "model": _empty_model(), "backtest": None,
            "predictions": [], "scorecard": {"live": {}, "assessments": _empty_assessments()}, "lessons": [],
            "last_as_of": None, "last_run": None}


def _fill(st):
    """Missing optional keys of a checked state get their empty values."""
    base = new_state(st["ticker"], st.get("created"))
    for k in STATE_KEYS:
        if st.get(k) is None and base[k] is not None:
            st[k] = copy.deepcopy(base[k])
    if not isinstance(st.get("model"), dict):
        st["model"] = _empty_model()
    if not isinstance(st.get("scorecard"), dict):
        st["scorecard"] = {"live": {}, "assessments": _empty_assessments()}


def _word_re(word, flags=0):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", flags)


def _ticker_spellings(sym):
    return {sym, sym.replace("-", "."), sym.replace("-", "/")}


def names_ticker(text, others):
    """True when text names one of the tickers in others as a whole word
    (case-sensitive; "BRK-B" also as "BRK.B" and "BRK/B")."""
    s = str(text or "")
    for sym in others:
        if sym and any(_word_re(sp).search(s) for sp in _ticker_spellings(str(sym))):
            return True
    return False


def company_core(name):
    """A company name without its trailing corporate words: "Apple Inc." -> "Apple"."""
    words = re.sub(r"[.,'()]", " ", str(name or "")).split()
    while words and words[-1].lower() in CORPORATE_WORDS:
        words.pop()
    return " ".join(words)


def names_company(text, names):
    """True when text names one of the companies, in full or without its
    corporate suffix, as whole words, ignoring case."""
    s = str(text or "")
    for name in names:
        for cand in {str(name or "").strip(), company_core(name)}:
            if len(cand) >= 3 and _word_re(cand, re.IGNORECASE).search(s):
                return True
    return False


def check_state(ticker, state, others=(), *, cik=None):
    """Refuse a state that is not ticker's own. Raises IsolationError (wrong
    ticker, a CIK that is not the ticker's, an unknown top-level key, a
    lesson naming another tracked ticker) or ValueError (not a state of this
    kind and version); returns the state unchanged."""
    if not isinstance(state, dict):
        raise ValueError("The state is not an object.")
    if state.get("kind") != "stocks.state" or state.get("version") != STATE_VERSION:
        raise ValueError("The state has another kind or version.")
    if set(state) - set(STATE_KEYS):
        raise IsolationError("The state holds keys outside the schema.")
    if not ticker or state.get("ticker") != ticker:
        raise IsolationError("The state belongs to another ticker.")
    ident = state.get("identity") or {}
    if not isinstance(ident, dict):
        raise ValueError("The identity is not an object.")
    if ident.get("ticker") not in (None, "", ticker):
        raise IsolationError("The identity belongs to another ticker.")
    if cik not in (None, "") and ident.get("cik") not in (None, ""):
        try:
            same = int(ident["cik"]) == int(cik)
        except (TypeError, ValueError):
            same = False
        if not same:
            raise IsolationError("The state's CIK is not this ticker's.")
    for key, kind in (("predictions", list), ("lessons", list), ("features", list), ("caches", dict),
                      ("model", dict), ("scorecard", dict)):
        if state.get(key) is not None and not isinstance(state[key], kind):
            raise ValueError("The state has a field of the wrong type.")
    if any(f not in FEATURES for f in state.get("features") or []):
        raise ValueError("The state names an unknown feature.")
    rest = tuple(o for o in others if o and o != ticker)
    for lesson in state.get("lessons") or []:
        text = lesson.get("text") if isinstance(lesson, dict) else lesson
        if names_ticker(text, rest):
            raise IsolationError("A lesson names another tracked ticker.")
    return state


# ---------------------------------------------------------------- features

def _fred_points(fred, series):
    pts = {}
    for p in (fred or {}).get(series) or []:
        try:
            d, v = p[0], num(p[1])
        except (TypeError, IndexError, KeyError):
            continue
        if isinstance(d, str) and v is not None:
            pts[d[:10]] = v
    return sorted(pts.items())


def _asof_index(obs_dates, obs_ord, d, d_ord):
    """Index of the last observation on or before d and no more than
    FRED_MAX_GAP_DAYS older, or None."""
    j = bisect.bisect_right(obs_dates, d) - 1
    if j < 0 or d_ord - obs_ord[j] > FRED_MAX_GAP_DAYS:
        return None
    return j


def short_z_by_date(short_volume, window=SHORT_WINDOW, min_n=SHORT_MIN_N):
    """{date: z of that day's short volume ratio against its trailing window}."""
    series = ratio_series(short_volume or [])
    vals = [v for _, v in series]
    out = {}
    for k, (d, v) in enumerate(series):
        base = vals[max(0, k - window):k]
        if len(base) >= min_n:
            mu = mean(base)
            sd = sstdev(base, mu)
            if sd:
                out[d] = (v - mu) / sd
    return out


def feature_columns(bars, spy, fred, short_volume):
    """{feature: [value or None per bar]} for every feature in FEATURES, each
    value using data up to and including that bar only."""
    n = len(bars)
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    cols = {}
    rets = {k: log_return(closes, k) for k in (5, 20, 60)}
    for k in (5, 20, 60):
        cols[f"ret_{k}"] = rets[k]
    for k in (20, 50, 200):
        s = sma(closes, k)
        cols[f"dist_sma{k}"] = [None if m is None or m <= 0 else c / m - 1.0 for c, m in zip(closes, s)]
    cols["rsi14"] = [None if v is None else (v - 50.0) / 50.0 for v in rsi(closes, 14)]
    cols["macd_hist"] = [None if h is None else h / c for h, c in zip(macd(closes)["hist"], closes)]
    cols["pct_b"] = [None if p is None else p - 0.5 for p in bollinger(closes)["pct_b"]]
    cols["atr_pct"] = [None if a is None else a / c for a, c in zip(atr(bars, 14), closes)]
    cols["vol_ratio"] = [math.log(v / a) if a and v and v > 0 and a > 0 else None
                         for v, a in zip(vols, sma(vols, 20))]
    spy_bars = clean_bars(spy or [])
    spy_pos = {b["date"]: i for i, b in enumerate(spy_bars)}
    spy_close = [b["close"] for b in spy_bars]
    rs20, sr5 = [None] * n, [None] * n
    for i, b in enumerate(bars):
        k = spy_pos.get(b["date"])
        if k is None:
            continue
        if k >= 5:
            sr5[i] = math.log(spy_close[k] / spy_close[k - 5])
        if k >= 20 and rets[20][i] is not None:
            rs20[i] = rets[20][i] - math.log(spy_close[k] / spy_close[k - 20])
    cols["rs_spy_20"], cols["spy_ret_5"] = rs20, sr5
    ords = [date.fromisoformat(b["date"]).toordinal() for b in bars]
    d10, vix = [None] * n, [None] * n
    for series, out, kind in (("DGS10", d10, "chg5"), ("VIXCLS", vix, "rel")):
        pts = _fred_points(fred, series)
        if not pts:
            continue
        od = [p[0] for p in pts]
        oo = [date.fromisoformat(p[0]).toordinal() for p in pts]
        ov = [p[1] for p in pts]
        for i, b in enumerate(bars):
            j = _asof_index(od, oo, b["date"], ords[i])
            if j is None:
                continue
            if kind == "chg5":
                if j >= 5:
                    out[i] = ov[j] - ov[j - 5]
            else:
                out[i] = ov[j] / 20.0 - 1.0
    cols["d_dgs10_5"], cols["vix_rel"] = d10, vix
    zmap = short_z_by_date(short_volume)
    cols["short_ratio_z"] = [zmap.get(b["date"]) for b in bars]
    return cols


def select_features(cols, n_bars, horizons=(5, 20), min_train_rows=150):
    """The features this ticker uses, in FEATURES order.

    A feature with no data at all is left out. dist_sma200 needs at least
    260 bars and, because its 200 bars of warm-up cost training rows, enough
    bars with a 200 day average for two years of labeled rows (and at least
    min_train_rows): a young ticker keeps more rows instead of one feature.
    short_ratio_z joins when the ticker's own short volume covers at least
    250 of the rows the other features allow; older rows get 0 (a normal day)."""
    names = [f for f in FEATURES if f != "short_ratio_z" and any(v is not None for v in cols[f])]
    if "dist_sma200" in names:
        defined = sum(1 for v in cols["dist_sma200"] if v is not None)
        if n_bars < SMA200_MIN_BARS or defined - max(horizons) < max(min_train_rows, SMA200_MIN_ROWS):
            names.remove("dist_sma200")
    covered = sum(1 for i in range(n_bars)
                  if cols["short_ratio_z"][i] is not None and all(cols[f][i] is not None for f in names))
    if covered >= SHORT_MIN_ROWS:
        names.append("short_ratio_z")
    return [f for f in FEATURES if f in names]


def _value(cols, f, i):
    v = cols[f][i]
    if v is None and f == "short_ratio_z":
        return 0.0
    return v


def _rows(bars, cols, names, horizons):
    closes = [b["close"] for b in bars]
    n = len(bars)
    rows = []
    for i in range(n):
        x = [_value(cols, f, i) for f in names]
        if any(v is None for v in x):
            continue
        y = {str(h): (1 if closes[i + h] > closes[i] else 0) if i + h < n else None for h in horizons}
        rows.append({"date": bars[i]["date"], "close": closes[i], "i": i, "x": x, "y": y})
    return rows


def feature_rows(bars, spy, fred, short_volume, *, horizons=(5, 20), min_train_rows=150):
    """(names in use, rows [{"date", "close", "i", "x", "y"}]) for every bar
    with all selected features defined; "i" is the bar index and "y" holds
    the label per horizon ("5": 1 when the close 5 bars later is higher, None
    when that bar is not there yet)."""
    bars = clean_bars(bars)
    cols = feature_columns(bars, spy, fred, short_volume)
    names = select_features(cols, len(bars), horizons, min_train_rows)
    return names, _rows(bars, cols, names, horizons)


# ---------------------------------------------------------------- model

def _sigmoid(s):
    if s >= 0:
        return 1.0 / (1.0 + math.exp(-min(s, 40.0)))
    e = math.exp(max(s, -40.0))
    return e / (1.0 + e)


def _clip(p):
    return min(P_MAX, max(P_MIN, p))


def _logit(p):
    p = min(1.0 - 1e-6, max(1e-6, p))
    return math.log(p / (1.0 - p))


def _standardize(cols):
    """(z columns, means, standard deviations) with the population standard
    deviation floored at 1e-8."""
    n = len(cols[0]) if cols else 0
    mu = [math.fsum(c) / n for c in cols]
    sd = [max(math.sqrt(math.fsum((v - m) * (v - m) for v in c) / n), STD_FLOOR) for c, m in zip(cols, mu)]
    z = [[(v - m) / s for v in c] for c, m, s in zip(cols, mu, sd)]
    return z, mu, sd


def _newton(z, mu, sd, y, lam, warm=None):
    """Newton (IRLS) for L2 logistic regression on standardized columns z;
    lam on the weights, not the intercept; at most 25 steps, stop when every
    step is below 1e-8; warm start from a previous fit with as many weights."""
    n, d = len(y), len(z)
    if n == 0:
        raise ValueError("No training rows.")
    base = math.fsum(y) / n
    if warm and len(warm.get("w") or []) == d:
        w, b = [float(v) for v in warm["w"]], float(warm["b"])
    else:
        w, b = [0.0] * d, _logit(base)
    mul = operator.mul
    iterations = 0
    for iterations in range(1, MAX_ITER + 1):
        s = [b] * n
        for k in range(d):
            wk = w[k]
            if wk:
                s = [a + wk * x for a, x in zip(s, z[k])]
        p = [_sigmoid(v) for v in s]
        q = [pi * (1.0 - pi) for pi in p]
        e = [pi - yi for pi, yi in zip(p, y)]
        g = [sum(e)] + [sum(map(mul, e, z[k])) + lam * w[k] for k in range(d)]
        qz = [list(map(mul, q, z[k])) for k in range(d)]
        h = [[0.0] * (d + 1) for _ in range(d + 1)]
        h[0][0] = sum(q) + 1e-9
        for k in range(d):
            h[0][k + 1] = h[k + 1][0] = sum(qz[k])
            for m in range(k, d):
                v = sum(map(mul, qz[k], z[m]))
                h[k + 1][m + 1] = h[m + 1][k + 1] = v
            h[k + 1][k + 1] += lam
        step = solve(h, g)
        b -= step[0]
        w = [wk - st for wk, st in zip(w, step[1:])]
        if max(abs(v) for v in step) < STEP_TOL:
            break
    return {"kind": "logistic_l2", "w": w, "b": b, "mean": list(mu), "std": list(sd), "lambda": lam, "n": n,
            "base_rate": base, "iterations": iterations}


def _fit_columns(cols, y, lam=LAMBDA, warm=None):
    z, mu, sd = _standardize(cols)
    return _newton(z, mu, sd, y, lam, warm)


def _round_params(p):
    out = dict(p)
    for k in ("w", "mean", "std"):
        if k in out:
            out[k] = [round(v, 10) for v in out[k]]
    for k in ("b",):
        if k in out:
            out[k] = round(out[k], 10)
    if "base_rate" in out:
        out["base_rate"] = round(out["base_rate"], 4)
    return out


def fit(rows, names, horizon, lam=LAMBDA, warm=None):
    """Fit the horizon's model on the labeled rows given. warm is a previous
    fit with the same feature list (its weights are the starting point).
    Returns the params of contract 7 ("w", "b", "mean", "std", "lambda", "n",
    "base_rate", "fitted_on", "iterations") plus "kind"."""
    key = str(horizon)
    data = [r for r in rows if r["y"].get(key) is not None]
    if not data:
        raise ValueError("No labeled rows.")
    cols = [[r["x"][k] for r in data] for k in range(len(names))]
    params = _fit_columns(cols, [float(r["y"][key]) for r in data], lam, warm)
    params["fitted_on"] = rows[-1]["date"]
    return _round_params(params)


def _raw_predict(params, x):
    s = params["b"]
    for wk, xk, m, sd in zip(params["w"], x, params["mean"], params["std"]):
        if xk is not None:
            s += wk * (xk - m) / sd
    return _clip(_sigmoid(s))


def predict(params, x):
    """Probability that the close h bars ahead is higher, clipped to
    [0.02, 0.98]. A missing feature value counts as its training mean; a
    base rate model returns its base rate (0.5 when unknown)."""
    if not params or params.get("kind") == "base_rate" or not params.get("w"):
        b = num((params or {}).get("base_rate"))
        return _clip(PRIOR_MEAN if b is None else b)
    return _raw_predict(params, x)


def _base_rate_params(train, key, as_of):
    """The warming up model: the observed up rate shrunk toward one half.
    Labels h days ahead overlap, so the observations count as n / h
    independent ones against PRIOR_STRENGTH pseudo-observations."""
    ys = [r["y"][key] for r in train]
    h = max(1, int(key))
    n_eff = len(ys) / h
    rate = sum(ys) / len(ys) if ys else PRIOR_MEAN
    shrunk = (rate * n_eff + PRIOR_MEAN * PRIOR_STRENGTH) / (n_eff + PRIOR_STRENGTH)
    return {"kind": "base_rate", "base_rate": round(shrunk, 4), "n": len(ys),
            "observed_rate": round(rate, 4) if ys else None, "prior": PRIOR_MEAN,
            "prior_strength": PRIOR_STRENGTH, "fitted_on": as_of}


# ---------------------------------------------------------------- scoring

def _score(triples):
    """Scorecard from (p, base_rate, y) triples."""
    n = len(triples)
    cal = []
    for lo, hi in BINS:
        inside = [(p, y) for p, _, y in triples if lo <= p < hi or (hi == 1.0 and p == 1.0)]
        cal.append({"lo": lo, "hi": hi, "n": len(inside),
                    "mean_p": round(mean([p for p, _ in inside]), 4) if inside else None,
                    "freq": round(mean([float(y) for _, y in inside]), 4) if inside else None})
    if not n:
        return {"n": 0, "hit_rate": None, "brier": None, "base_brier": None, "skill": None, "up_rate": None,
                "calibration": cal}
    hits = sum(1 for p, _, y in triples if (p >= 0.5) == (y == 1))
    brier = math.fsum((p - y) ** 2 for p, _, y in triples) / n
    base = math.fsum((b - y) ** 2 for _, b, y in triples) / n
    skill = None if n < 10 or base == 0 else 1.0 - brier / base
    return {"n": n, "hit_rate": round(hits / n, 4), "brier": round(brier, 4), "base_brier": round(base, 4),
            "skill": None if skill is None else round(skill, 4),
            "up_rate": round(sum(y for _, _, y in triples) / n, 4), "calibration": cal}


def scorecard(entries, horizon):
    """The scorecard of the graded entries at horizon (contract 6.5)."""
    key = str(horizon)
    triples = []
    for e in entries or []:
        out = (e.get("outcome") or {}).get(key)
        p = num((e.get("p_up") or {}).get(key))
        b = num((e.get("base_rate") or {}).get(key))
        if isinstance(out, dict) and out.get("y") in (0, 1) and p is not None:
            triples.append((p, PRIOR_MEAN if b is None else b, out["y"]))
    return _score(triples)


def _assessment_score(entries):
    graded = [e["assessment"]["grade"] for e in entries or []
              if isinstance(e.get("assessment"), dict) and isinstance(e["assessment"].get("grade"), dict)
              and e["assessment"]["grade"].get("graded_on")]
    stance = [g["stance_correct"] for g in graded if g.get("stance_correct") is not None]
    return {"n": len(graded),
            "stance_hit_rate": round(sum(1 for s in stance if s) / len(stance), 4) if stance else None,
            "target": sum(1 for g in graded if g.get("levels") == "target"),
            "stop": sum(1 for g in graded if g.get("levels") == "stop"),
            "neither": sum(1 for g in graded if g.get("levels") == "neither")}


# ---------------------------------------------------------------- grading

def _abs_moves(closes, h):
    return [abs(closes[i + h] / closes[i] - 1.0) for i in range(len(closes) - h)]


def _grade_inplace(st, bars):
    dates = [b["date"] for b in bars]
    pos = {d: i for i, d in enumerate(dates)}
    closes = [b["close"] for b in bars]
    n = len(bars)
    med_abs = None
    for e in st.get("predictions") or []:
        i = pos.get(e.get("as_of"))
        if i is None:
            continue
        outcome = e.setdefault("outcome", {})
        for key in list((e.get("p_up") or {}).keys()):
            outcome.setdefault(key, None)
        for key, out in list(outcome.items()):
            if out is not None:
                continue
            h = int(key)
            if i + h < n:
                c0, c1 = closes[i], closes[i + h]
                outcome[key] = {"y": 1 if c1 > c0 else 0, "ret_pct": round_pct((c1 / c0 - 1.0) * 100.0),
                                "graded_on": dates[i + h]}
        a = e.get("assessment")
        if not isinstance(a, dict):
            continue
        g = a.get("grade") if isinstance(a.get("grade"), dict) else {}
        if g.get("graded_on") or i + ASSESS_HORIZON >= n:
            continue
        ret = closes[i + ASSESS_HORIZON] / closes[i] - 1.0
        stance = a.get("stance")
        if stance == "bullish":
            correct = ret > 0
        elif stance == "bearish":
            correct = ret < 0
        elif stance == "neutral":
            if med_abs is None:
                med_abs = median(_abs_moves(closes, ASSESS_HORIZON)) or 0.0
            correct = abs(ret) < med_abs
        else:
            correct = None
        scale = closes[i] / e["close"] if num(e.get("close")) else 1.0
        targets = sorted(num(t.get("price")) for t in a.get("targets") or []
                         if isinstance(t, dict) and num(t.get("price")))
        stop = num((a.get("stop") or {}).get("price")) if isinstance(a.get("stop"), dict) else None
        levels = None
        if targets and stop:
            tgt, stp = targets[0] * scale, stop * scale
            levels = "neither"
            for k in range(i + 1, i + ASSESS_HORIZON + 1):
                if bars[k]["low"] <= stp:
                    levels = "stop"
                    break
                if bars[k]["high"] >= tgt:
                    levels = "target"
                    break
        a["grade"] = {"stance_correct": correct, "levels": levels, "graded_on": dates[i + ASSESS_HORIZON],
                      "ret_pct": round_pct(ret * 100.0)}


def grade(state, bars):
    """A copy of state with every logged call whose horizon has passed in
    bars graded: the bar h rows after the entry's as_of gives y and ret_pct;
    assessments are graded after 20 bars (stance and levels)."""
    st = copy.deepcopy(state)
    _grade_inplace(st, clean_bars(bars))
    st["scorecard"] = {"live": {str(h): scorecard(st.get("predictions"), h)
                                for h in (st.get("model") or {}).get("horizons") or []},
                       "assessments": _assessment_score(st.get("predictions"))}
    return st


# ---------------------------------------------------------------- excursions

def _setup(close, s50, r):
    if s50 is None or r is None:
        return None
    band = "below 40" if r < 40 else "above 60" if r > 60 else "40 to 60"
    return (close > s50, band)


def _setup_label(setup):
    side, band = setup
    return f"{'Above' if side else 'Below'} the 50 day average, RSI {band}"


def excursions(bars, horizon=EXCURSION_HORIZON, min_n=EXCURSION_MIN_N, similar=True):
    """Quantiles (25, 50, 75) of the forward maximum drawdown
    (min(low[t+1..t+h]) / close[t] - 1) and run-up (max(high[t+1..t+h]) /
    close[t] - 1) over the ticker's own history; with `similar`, over the
    past days in the same setup as the last bar (same side of the 50 day
    average, same RSI band) when there are at least min_n of them. None
    with fewer than min_n samples."""
    bars = clean_bars(bars)
    n = len(bars)
    closes = [b["close"] for b in bars]
    total = n - horizon
    if total < min_n:
        return None
    s50, r14 = sma(closes, 50), rsi(closes, 14)
    today = _setup(closes[-1], s50[-1], r14[-1]) if similar else None
    dd_all, ru_all, dd_sim, ru_sim = [], [], [], []
    for t in range(total):
        lows = [b["low"] for b in bars[t + 1:t + horizon + 1]]
        highs = [b["high"] for b in bars[t + 1:t + horizon + 1]]
        dd, ru = min(lows) / closes[t] - 1.0, max(highs) / closes[t] - 1.0
        dd_all.append(dd)
        ru_all.append(ru)
        if today is not None and _setup(closes[t], s50[t], r14[t]) == today:
            dd_sim.append(dd)
            ru_sim.append(ru)
    use_sim = today is not None and len(dd_sim) >= min_n
    dd, ru = (dd_sim, ru_sim) if use_sim else (dd_all, ru_all)

    def qs(values):
        return {"25": round(quantile(values, 0.25), 4), "50": round(quantile(values, 0.5), 4),
                "75": round(quantile(values, 0.75), 4)}

    return {"horizon": horizon, "n": len(dd), "n_all": total, "scope": "similar" if use_sim else "all",
            "setup": _setup_label(today) if today is not None else None,
            "drawdown_q": qs(dd), "runup_q": qs(ru)}


# ---------------------------------------------------------------- walk forward

def _choose(errors, lambdas, n):
    """The lambda with the lowest summed squared error over n known
    outcomes; the larger lambda (the more cautious model) wins a tie, and
    stands until at least MIN_CHOICE_N outcomes are known."""
    if not errors or n < MIN_CHOICE_N:
        return max(lambdas)
    return min(lambdas, key=lambda lam: (round(errors[lam], 12), -lam))


def _walk_forward(rows, names, h, c, lambdas=LAMBDAS):
    """The ticker's own walk forward backtest for horizon h.

    At rows start, start + refit_every, ... every lambda in the grid is
    fitted on the rows whose label was known by that bar (the last
    bootstrap_years * 252 of them) and predicts the next refit_every rows.
    The backtest's prediction for a row comes from the lambda with the
    lowest squared error on the earlier predictions whose outcome was known
    by the bar of the refit (no look-ahead). Returns (results [(p,
    base_rate, y, date)], fits, {lambda: last model}, {lambda: (sum of
    squared errors, n)})."""
    key = str(h)
    n, d = len(rows), len(names)
    idx = [r["i"] for r in rows]
    allcols = [[r["x"][k] for r in rows] for k in range(d)]
    ally = [r["y"][key] for r in rows]
    window = c["bootstrap_years"] * 252
    known = []
    ptr = 0
    for i in range(n):
        while ptr < n and idx[ptr] + h <= idx[i]:
            ptr += 1
        known.append(ptr)
    start = next((i for i in range(n) if known[i] >= c["min_train_rows"]), None)
    out, fits = [], 0
    models = {lam: None for lam in lambdas}
    pending = []                     # (bar where the outcome is known, {lambda: squared error})
    seen = {lam: 0.0 for lam in lambdas}
    totals = {lam: 0.0 for lam in lambdas}
    count = 0
    i = start
    while i is not None and i < n:
        hi = known[i]
        lo = max(0, hi - window)
        while pending and pending[0][0] <= idx[i]:
            for lam, err in pending.pop(0)[1].items():
                seen[lam] += err
            count += 1
        chosen = _choose(seen, lambdas, count)
        z, mu, sd = _standardize([col[lo:hi] for col in allcols])
        y = [float(v) for v in ally[lo:hi]]
        for lam in lambdas:
            models[lam] = _newton(z, mu, sd, y, lam, models[lam])
            fits += 1
        for k in range(i, min(i + c["refit_every"], n)):
            if ally[k] is None:
                continue
            x = [col[k] for col in allcols]
            errs = {}
            for lam in lambdas:
                p = round(_raw_predict(models[lam], x), 4)
                errs[lam] = (p - ally[k]) ** 2
                totals[lam] += errs[lam]
                if lam == chosen:
                    out.append((p, round(models[lam]["base_rate"], 4), ally[k], rows[k]["date"]))
            pending.append((idx[k] + h, errs))
        i += c["refit_every"]
    return out, fits, models, {lam: (totals[lam], len(out)) for lam in lambdas}


# ---------------------------------------------------------------- the daily run

def _lam_key(lam):
    return str(int(lam)) if float(lam).is_integer() else str(lam)


def _needs_bootstrap(st, names, dates):
    """No model yet, still warming up, a new feature list, or a backtest
    more than REBOOT_BARS bars old (a fresh backtest and lambda choice
    about once a quarter)."""
    model = st.get("model") or {}
    bt = st.get("backtest")
    if (list(st.get("features") or []) != list(names) or model.get("kind") != "logistic_l2"
            or model.get("status") != "ok" or not isinstance(bt, dict)):
        return True
    made = bt.get("as_of")
    if not isinstance(made, str):
        return True
    return len(dates) - bisect.bisect_right(dates, made) >= REBOOT_BARS


def _run(state, bars, spy, fred, short_volume, cfg, now, force, ticker=None, others=()):
    if not isinstance(state, dict):
        raise ValueError("The state is not an object.")
    check_state(state.get("ticker") if ticker is None else ticker, state, others)
    c = _cfg(cfg)
    bars = clean_bars(bars)
    if len(bars) < 2:
        raise ValueError("Not enough price bars.")
    st = copy.deepcopy(state)
    _fill(st)
    horizons = c["horizons"]
    cols = feature_columns(bars, spy, fred, short_volume)
    names = select_features(cols, len(bars), horizons, c["min_train_rows"])
    rows = _rows(bars, cols, names, horizons)
    _grade_inplace(st, bars)
    as_of = bars[-1]["date"]
    boot = force or _needs_bootstrap(st, names, [b["date"] for b in bars])
    model = st["model"]
    prev_params = (model.get("params") or {}) if not boot else {}
    refits = 0 if boot else int((model.get("training") or {}).get("refits") or 0)
    window = c["bootstrap_years"] * 252
    params, backtest_sc, backtest_rows, bt_dates, lambda_scores = {}, {}, {}, [], {}
    train_first = None
    for h in horizons:
        key = str(h)
        labeled = [r for r in rows if r["y"][key] is not None]
        train = labeled[-window:]
        if train_first is None:
            train_first = train
        prev = prev_params.get(key) or {}
        if names and len(train) >= c["min_train_rows"]:
            warm, lam = None, num(prev.get("lambda")) or max(LAMBDAS)
            if boot:
                results, fits, models, totals = _walk_forward(rows, names, h, c)
                refits += fits
                if results:
                    backtest_sc[key] = _score([(p, b, y) for p, b, y, _ in results])
                    backtest_rows[key] = len(results)
                    bt_dates.extend(dt for _, _, _, dt in results[:1] + results[-1:])
                    lam = _choose({k: v[0] for k, v in totals.items()}, LAMBDAS, len(results))
                    lambda_scores[key] = {_lam_key(k): round(v[0] / v[1], 4) if v[1] else None
                                          for k, v in totals.items()}
                    warm = _round_params(models[lam])
                else:
                    lam = max(LAMBDAS)
            elif prev.get("kind") == "logistic_l2" and len(prev.get("w") or []) == len(names):
                warm = prev
            if (not boot and prev.get("kind") == "logistic_l2" and prev.get("fitted_on") == as_of
                    and prev.get("n") == len(train) and len(prev.get("w") or []) == len(names)):
                p = prev
            else:
                p = fit(train, names, h, lam, warm)
                p["fitted_on"] = as_of
                refits += 1
        else:
            p = _base_rate_params(train, key, as_of)
        params[key] = p
    kinds = [p["kind"] for p in params.values()]
    status = "ok" if all(k == "logistic_l2" for k in kinds) else "warming_up"
    if boot:
        st["backtest"] = None
        if backtest_sc:
            for h in horizons:
                backtest_sc.setdefault(str(h), _score([]))
            st["backtest"] = {"method": "walk_forward", "min_train": c["min_train_rows"], "step": c["refit_every"],
                              "rows": backtest_rows.get(str(horizons[0]), 0),
                              "rows_by_horizon": {str(h): backtest_rows.get(str(h), 0) for h in horizons},
                              "first_date": min(bt_dates) if bt_dates else None,
                              "last_date": max(bt_dates) if bt_dates else None,
                              "lambdas": [_lam_key(v) for v in LAMBDAS], "lambda_brier": lambda_scores,
                              "as_of": as_of, "created": now, "scorecard": backtest_sc}
    st["features"] = names
    st["model"] = {
        "kind": "logistic_l2" if "logistic_l2" in kinds else "base_rate", "status": status,
        "horizons": horizons, "params": params,
        "excursions": excursions(bars),
        "training": {"rows": len(train_first or []),
                     "first_date": train_first[0]["date"] if train_first else None,
                     "last_date": train_first[-1]["date"] if train_first else None,
                     "refits": refits, "bars": len(bars), "feature_rows": len(rows),
                     "first_bar": bars[0]["date"]},
    }
    last_i = len(bars) - 1
    x = [_value(cols, f, last_i) for f in names]
    p_up = {str(h): round(predict(params[str(h)], x), 4) for h in horizons}
    base = {str(h): round(params[str(h)]["base_rate"], 4) for h in horizons}
    preds = st["predictions"]
    entry = next((e for e in reversed(preds) if e.get("as_of") == as_of), None)
    last = st.get("last_as_of")
    if entry is not None:
        entry["p_up"] = p_up
    elif last is None or as_of > last:
        from stocks.data.trading_days import add_trading_days

        d0 = date.fromisoformat(as_of)
        entry = {"as_of": as_of, "close": round_price(bars[-1]["close"]), "p_up": p_up, "base_rate": base,
                 "due": {str(h): add_trading_days(d0, h).isoformat() for h in horizons},
                 "outcome": {str(h): None for h in horizons}, "assessment": None}
        missing = [f for f, v in zip(names, x) if v is None]
        if missing:
            entry["imputed"] = missing
        preds.append(entry)
    st["scorecard"] = {"live": {str(h): scorecard(preds, h) for h in horizons},
                       "assessments": _assessment_score(preds)}
    st["last_as_of"] = as_of if last is None or as_of > last else last
    st["updated"] = now
    st["last_run"] = {"at": now, "status": status, "model": None}
    return st


def bootstrap(state, bars, spy, fred, short_volume, cfg, now, *, ticker=None, others=()):
    """The first run of a ticker (or a fresh start): a walk forward backtest
    over its own history, the live model fitted on all labeled rows, and the
    day's call logged. With fewer than min_train_rows labeled rows the model
    predicts the base rate and the status is "warming_up". With ticker (and
    others), check_state runs against them first."""
    return _run(state, bars, spy, fred, short_volume, cfg, now, True, ticker, others)


def daily_update(state, bars, spy, fred, short_volume, cfg, now, *, ticker=None, others=()):
    """A trading day's run, idempotent per as_of (the last bar's date):
    grade, refit from the previous weights, log the call, recompute the
    scorecards. Runs the bootstrap instead when the state has no model yet,
    was warming up, its feature list changed, or its backtest is more than
    REBOOT_BARS bars old."""
    return _run(state, bars, spy, fred, short_volume, cfg, now, False, ticker, others)


# ---------------------------------------------------------------- assessments and lessons

def _compact_assessment(a, model):
    zones = [{"low": round_price(z.get("low")), "high": round_price(z.get("high"))}
             for z in a.get("entry_zones") or [] if isinstance(z, dict)]
    targets = [{"price": round_price(t.get("price")), "horizon_days": t.get("horizon_days")}
               for t in a.get("targets") or [] if isinstance(t, dict)]
    stop = a.get("stop") if isinstance(a.get("stop"), dict) else None
    return {"model": model, "source": a.get("source"), "levels_source": a.get("levels_source"),
            "stance": a.get("stance"), "confidence": a.get("confidence"), "entry_zones": zones,
            "targets": targets, "stop": None if stop is None else {"price": round_price(stop.get("price"))},
            "grade": {"stance_correct": None, "levels": None, "graded_on": None}}


def clean_lessons(texts, existing, ticker, others=(), other_names=()):
    """The new lesson texts that would be kept: plain, at most 200
    characters, at most two, no case-insensitive duplicate of an existing
    lesson, none naming another tracked ticker or company."""
    seen = {str((l or {}).get("text", "")).lower() for l in existing or [] if isinstance(l, dict)}
    rest = tuple(o for o in others if o and o != ticker)
    kept = []
    for text in list(texts or [])[:LESSONS_PER_RUN]:
        t = plain(text, LESSON_CHARS)
        if not t or t.lower() in seen:
            continue
        if names_ticker(t, rest) or names_company(t, other_names):
            continue
        seen.add(t.lower())
        kept.append(t)
    return kept


def attach_assessment(state, as_of, assessment, lessons, model, cap, others=(), other_names=()):
    """A copy of state with the day's assessment (section 8.10, as validated)
    attached to the call logged for as_of, and the model's new lessons added
    (see clean_lessons), keeping the newest `cap`."""
    st = copy.deepcopy(state)
    _fill(st)
    entry = next((e for e in reversed(st["predictions"]) if e.get("as_of") == as_of), None)
    if entry is not None and isinstance(assessment, dict):
        entry["assessment"] = _compact_assessment(assessment, model)
    new = clean_lessons(lessons, st["lessons"], st["ticker"], others, other_names)
    lessons = st["lessons"] + [{"date": as_of, "text": t, "source": "model"} for t in new]
    keep = max(0, int(cap))
    st["lessons"] = lessons[-keep:] if keep else []
    run = dict(st.get("last_run") or {})
    run["model"] = model
    st["last_run"] = run
    return st


# ---------------------------------------------------------------- views

def confidence_note(state):
    """"Low confidence, n days of history." for a young or warming up
    ticker, else None."""
    model = state.get("model") or {}
    bars = int((model.get("training") or {}).get("bars") or 0)
    if bars < LOW_CONFIDENCE_BARS or model.get("status") != "ok":
        note = f"Low confidence, {bars} days of history."
        if model.get("status") != "ok":
            note += " Too little history to fit a model, so the call is the base rate."
        return note
    return None


def learner_block(state):
    """The Learner block of the ticker document (8.9)."""
    model = state.get("model") or {}
    training = model.get("training") or {}
    preds = state.get("predictions") or []
    last = preds[-1] if preds else {}
    sc = state.get("scorecard") or {}
    bt = state.get("backtest") or {}
    note = confidence_note(state)
    return {"status": "ok" if model.get("status") == "ok" else "warming_up", "kind": model.get("kind"),
            "features": list(state.get("features") or []),
            "probabilities": dict(last.get("p_up") or {}), "base_rate": dict(last.get("base_rate") or {}),
            "scorecard": {"live": copy.deepcopy(sc.get("live") or {}),
                          "backtest": copy.deepcopy(bt.get("scorecard") or {}),
                          "assessments": copy.deepcopy(sc.get("assessments") or _empty_assessments())},
            "backtest": {k: bt.get(k) for k in ("method", "rows", "first_date", "last_date", "min_train", "step")}
            if bt else None,
            "excursions": copy.deepcopy(model.get("excursions")),
            "lessons": [{"date": l.get("date"), "text": l.get("text")} for l in state.get("lessons") or []
                        if isinstance(l, dict)],
            "training_rows": int(training.get("rows") or 0), "history_days": int(training.get("bars") or 0),
            "confidence": "low" if note else "normal", "note": note,
            "as_of": state.get("last_as_of")}


def claude_context(state, recent=10):
    """What the model is told about this ticker's own learning: its
    probabilities, backtest and live scorecards, its last graded calls and
    its lessons. Nothing of any other ticker."""
    block = learner_block(state)
    calls = []
    for e in state.get("predictions") or []:
        out = e.get("outcome") or {}
        if not any(isinstance(v, dict) for v in out.values()):
            continue
        a = e.get("assessment") if isinstance(e.get("assessment"), dict) else {}
        g = a.get("grade") if isinstance(a.get("grade"), dict) else {}
        calls.append({"as_of": e.get("as_of"), "p_up": dict(e.get("p_up") or {}),
                      "outcome": {k: (None if not isinstance(v, dict) else {"up": v.get("y") == 1,
                                                                             "ret_pct": v.get("ret_pct")})
                                  for k, v in out.items()},
                      "source": a.get("source"), "stance": a.get("stance"),
                      "stance_correct": g.get("stance_correct"), "levels": g.get("levels")})
    return {"status": block["status"], "note": block["note"], "history_days": block["history_days"],
            "features": block["features"], "probabilities": block["probabilities"],
            "base_rate": block["base_rate"], "backtest": block["scorecard"]["backtest"],
            "live": block["scorecard"]["live"], "assessments": block["scorecard"]["assessments"],
            "recent_calls": calls[-recent:] if recent > 0 else [],
            "lessons": [l["text"] for l in block["lessons"]]}


# ---------------------------------------------------------------- size

def _ref_date(st):
    return st.get("last_as_of") or (st.get("updated") or "")[:10] or None


def _days_before(day, n):
    return date.fromordinal(date.fromisoformat(day).toordinal() - n).isoformat()


def _fully_graded(e):
    out = e.get("outcome") or {}
    a = e.get("assessment")
    a_done = not isinstance(a, dict) or bool((a.get("grade") or {}).get("graded_on"))
    return bool(out) and all(isinstance(v, dict) for v in out.values()) and a_done


def trim_state(state, max_bytes, *, insider_days=365, congress_days=365, bootstrap_years=5):
    """A copy of state under max_bytes (compact JSON). Removes, in order and
    only while too large: graded calls older than the newest 400, Form 4
    transactions older than insider_days, congress trades older than
    congress_days, short volume rows beyond 300, price rows beyond
    bootstrap_years * 252 + 300; then the oldest calls. Raises ValueError
    when even that is not enough."""
    st = copy.deepcopy(state)
    if size_of(st) <= max_bytes:
        return st
    ref = _ref_date(st)
    caches = st.get("caches") if isinstance(st.get("caches"), dict) else {}

    def old_graded():
        preds = st.get("predictions") or []
        if len(preds) > KEEP_OLD_GRADED:
            head, tail = preds[:-KEEP_OLD_GRADED], preds[-KEEP_OLD_GRADED:]
            st["predictions"] = [e for e in head if not _fully_graded(e)] + tail

    def form4():
        f4 = caches.get("form4")
        if ref and isinstance(f4, dict) and isinstance(f4.get("transactions"), list):
            cut = _days_before(ref, insider_days)
            f4["transactions"] = [t for t in f4["transactions"]
                                  if str(t.get("date") or t.get("filed") or "") >= cut]

    def congress():
        cg = caches.get("congress")
        if ref and isinstance(cg, dict) and isinstance(cg.get("trades"), list):
            cut = _days_before(ref, congress_days)
            cg["trades"] = [t for t in cg["trades"] if str(t.get("date") or t.get("filed") or "") >= cut]

    def shorts():
        sv = caches.get("short_volume")
        if isinstance(sv, dict) and isinstance(sv.get("rows"), list):
            sv["rows"] = sv["rows"][-SHORT_KEEP:]

    def prices():
        pr = caches.get("prices")
        if isinstance(pr, dict) and isinstance(pr.get("rows"), list):
            pr["rows"] = pr["rows"][-(bootstrap_years * 252 + 300):]
            if pr["rows"] and isinstance(pr["rows"][0], list) and pr["rows"][0]:
                pr["first_date"] = pr["rows"][0][0]

    for step in (old_graded, form4, congress, shorts, prices):
        step()
        if size_of(st) <= max_bytes:
            return st
    preds = st.get("predictions") or []
    while preds and size_of(st) > max_bytes:
        cut = max(1, len(preds) // 10)
        preds = preds[cut:]
        st["predictions"] = preds
    if size_of(st) > max_bytes:
        raise ValueError("The state is too large.")
    return st
