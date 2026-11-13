"""The analysis layer of the private stock section (contract section 6).

  indicators.py    moving averages, RSI, MACD, Bollinger bands, ATR, the
                   technicals block and the chart arrays
  levels.py        support and resistance from swing pivots, the candidate
                   price list a plan may use, the code's fallback plan
  shorts.py        FINRA short sale volume against the stock's own history
  macro_impact.py  the stock's measured sensitivity to rates, the dollar, oil
                   and VIX, and its moves on FOMC and CPI days
  learner.py       the per-ticker learner: features, logistic model, walk
                   forward backtest, daily grading, scorecards, lessons

Every function is pure and deterministic: inputs are passed in (dates too,
never read from the clock), nothing is cached at module level, nothing is
printed, and the standard library is the only dependency. A function that
works on a learning state takes exactly one ticker's state.

This file holds the small numeric helpers the modules share.
"""
import math

__all__ = ["num", "mean", "pstdev", "sstdev", "median", "quantile", "solve", "inverse"]


def num(x):
    """x as a finite float, or None."""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mean(values):
    return math.fsum(values) / len(values) if values else None


def pstdev(values, mu=None):
    """Population standard deviation (divide by n)."""
    if not values:
        return None
    mu = mean(values) if mu is None else mu
    return math.sqrt(math.fsum((v - mu) * (v - mu) for v in values) / len(values))


def sstdev(values, mu=None):
    """Sample standard deviation (divide by n - 1)."""
    if len(values) < 2:
        return None
    mu = mean(values) if mu is None else mu
    return math.sqrt(math.fsum((v - mu) * (v - mu) for v in values) / (len(values) - 1))


def quantile(values, q):
    """Linear interpolation between order statistics (numpy's default), q in [0, 1]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def median(values):
    return quantile(values, 0.5)


def solve(a, b):
    """x with a x = b, by Gaussian elimination with partial pivoting.
    a is a square list of lists, b a list. Raises ValueError when singular."""
    m = len(b)
    rows = [list(map(float, a[i])) + [float(b[i])] for i in range(m)]
    for c in range(m):
        p = max(range(c, m), key=lambda r: abs(rows[r][c]))
        if abs(rows[p][c]) < 1e-14:
            raise ValueError("singular matrix")
        rows[c], rows[p] = rows[p], rows[c]
        pivot_row = rows[c]
        piv = pivot_row[c]
        for r in range(c + 1, m):
            row = rows[r]
            f = row[c] / piv
            if f:
                for k in range(c, m + 1):
                    row[k] -= f * pivot_row[k]
    x = [0.0] * m
    for c in range(m - 1, -1, -1):
        row = rows[c]
        x[c] = (row[m] - math.fsum(row[k] * x[k] for k in range(c + 1, m))) / row[c]
    return x


def inverse(a):
    """The inverse of a square matrix (list of lists), column by column."""
    m = len(a)
    cols = []
    for j in range(m):
        e = [0.0] * m
        e[j] = 1.0
        cols.append(solve(a, e))
    return [[cols[j][i] for j in range(m)] for i in range(m)]
