"""Macro series from FRED's graph CSV, no key needed (contract section 5.5).

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>&cosd=YYYY-MM-DD

One series per request (with several ids FRED ignores cosd). The header is
observation_date,<SERIES>; a missing value is an empty field (older files
used "."), and both are dropped. FRED's CDN refuses some user agents; the
private job sends its contact string, which FRED accepts.
"""
import re
import statistics
from datetime import date, datetime

from stocks.data import FetchError

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
SERIES_RE = re.compile(r"^[A-Z0-9]{2,30}$")
TRANSFORMS = ("level", "yoy", "diff", "range", "thousands")


def _day(value):
    s = str(value or "").strip()[:10]
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


def parse_csv(text, series_id):
    """[(date, value)] oldest first, missing values dropped."""
    lines = [ln.strip() for ln in str(text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        raise ValueError("the FRED answer is empty")
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    if len(header) < 2 or header[0].lower() not in ("observation_date", "date") \
            or header[1].upper() != str(series_id).upper():
        raise ValueError("the FRED answer has an unexpected header")
    out = {}
    for ln in lines[1:]:
        parts = [p.strip().strip('"') for p in ln.split(",")]
        if len(parts) < 2:
            continue
        day = _day(parts[0])
        if day is None or parts[1] in ("", "."):
            continue
        try:
            v = float(parts[1])
        except ValueError:
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        out[day] = v
    return [(d, out[d]) for d in sorted(out)]


def series(http, series_id, start):
    sid = str(series_id or "").strip().upper()
    if not SERIES_RE.match(sid):
        raise ValueError("not a FRED series id")
    resp = http.get(FRED_CSV.format(series=sid, start=_as_date(start).isoformat()))
    return parse_csv(resp.text(), sid)


def fetch_many(http, ids, start):
    """({id: points}, {id: reason}). start is a date, or {id: date}."""
    data, errors = {}, {}
    out_of_time = False
    for sid in dict.fromkeys(str(i or "").strip().upper() for i in ids or ()):
        if not sid:
            continue
        if out_of_time:
            errors[sid] = "time budget used up"
            continue
        s = start.get(sid) if isinstance(start, dict) else start
        try:
            points = series(http, sid, s)
        except FetchError as e:
            errors[sid] = e.reason
            out_of_time = e.reason == "time budget used up"
            continue
        except ValueError:
            errors[sid] = "bad data"
            continue
        if not points:
            errors[sid] = "no data"
            continue
        data[sid] = points
    return data, errors


def yoy(points):
    """Percent change against the observation with the same month and day a year
    before (monthly and quarterly series)."""
    by = dict(points or ())
    out = []
    for d, v in points or ():
        y, rest = d[:4], d[4:]
        prior = by.get(f"{int(y) - 1:04d}{rest}")
        if prior:
            out.append((d, (v / prior - 1.0) * 100.0))
    return out


def diff(points):
    pts = list(points or ())
    return [(pts[i][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts))]


def frequency(points):
    """daily, weekly, monthly, quarterly or annual, from the typical gap."""
    pts = list(points or ())[-31:]
    if len(pts) < 2:
        return None
    gaps = [(date.fromisoformat(pts[i][0]) - date.fromisoformat(pts[i - 1][0])).days for i in range(1, len(pts))]
    g = statistics.median(gaps)
    if g <= 4:
        return "daily"
    if g <= 10:
        return "weekly"
    if g <= 40:
        return "monthly"
    if g <= 120:
        return "quarterly"
    return "annual"


def _r(x):
    return None if x is None else round(x, 2)


def _row(cfg, status="ok", **values):
    sids = [str(s).upper() for s in cfg.get("series") or ()]
    row = {"id": cfg.get("id"), "label": cfg.get("label"), "unit": cfg.get("unit"), "frequency": None,
           "value": None, "prior": None, "change": None, "as_of": None, "prior_as_of": None, "display": None,
           "series": sids, "status": status}
    row.update(values)
    return row


def _range_row(cfg, data):
    sids = [str(s).upper() for s in cfg.get("series") or ()]
    if len(sids) != 2 or not data.get(sids[0]) or not data.get(sids[1]):
        return _row(cfg, "not_available")
    lower, upper = data[sids[0]], data[sids[1]]
    as_of, value = upper[-1]
    low_by = dict(lower)
    low = low_by.get(as_of, lower[-1][1])
    prior = prior_as_of = None
    for d, v in reversed(upper):
        if v != value:
            prior, prior_as_of = v, d
            break
    return _row(cfg, frequency=frequency(upper), value=_r(value), prior=_r(prior),
                change=_r(value - prior) if prior is not None else None, as_of=as_of, prior_as_of=prior_as_of,
                display=f"{low:.2f} to {value:.2f}")


def macro_table(data, rows):
    """MacroRows in config order. A row whose series failed is not_available."""
    out = []
    for cfg in rows or ():
        if not isinstance(cfg, dict):
            continue
        transform = cfg.get("transform")
        sids = [str(s).upper() for s in cfg.get("series") or ()]
        if transform == "range":
            out.append(_range_row(cfg, data or {}))
            continue
        points = (data or {}).get(sids[0]) if sids else None
        if not points or transform not in TRANSFORMS:
            out.append(_row(cfg, "not_available"))
            continue
        if transform == "yoy":
            pts = yoy(points)
        elif transform == "diff":
            pts = diff(points)
        elif transform == "thousands":
            pts = [(d, v / 1000.0) for d, v in points]
        else:
            pts = list(points)
        if not pts:
            out.append(_row(cfg, "not_available"))
            continue
        as_of, value = pts[-1]
        prior_as_of, prior = pts[-2] if len(pts) > 1 else (None, None)
        out.append(_row(cfg, frequency=frequency(points), value=_r(value), prior=_r(prior),
                        change=_r(value - prior) if prior is not None else None, as_of=as_of,
                        prior_as_of=prior_as_of))
    return out


def fx_usd_per_unit(data, currency, fx_cfg):
    """(US dollars per unit of currency, series id, date) from the latest
    observation of the currency's series in fx_cfg, or None. US dollars give
    (1.0, "", "")."""
    cur = str(currency or "").strip().upper()
    if cur == "USD":
        return 1.0, "", ""
    cfg = (fx_cfg or {}).get(cur)
    if not isinstance(cfg, dict):
        return None
    sid = str(cfg.get("series") or "").upper()
    points = (data or {}).get(sid)
    if not points:
        return None
    d, v = points[-1]
    if not v or v <= 0:
        return None
    quote = cfg.get("quote")
    if quote == "usd_per_unit":
        rate = v
    elif quote == "units_per_usd":
        rate = 1.0 / v
    else:
        return None
    return round(rate, 6), sid, d


# Daily series the macro sensitivities regress on: six years of history; every
# other series three.
FACTOR_SERIES = ("DGS10", "DGS2", "DTWEXBGS", "DCOILWTICO", "VIXCLS")


def run_starts(ids, today, factor_years=6, other_years=3):
    """{id: first date} for fetch_many: factor_years back for the factor series,
    other_years back for the rest."""
    t = _as_date(today)

    def back(years):
        try:
            return t.replace(year=t.year - years)
        except ValueError:  # February 29
            return t.replace(year=t.year - years, day=28)
    return {str(i).upper(): back(factor_years if str(i).upper() in FACTOR_SERIES else other_years) for i in ids}
