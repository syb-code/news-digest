"""FINRA short sale volume judged against the stock's own history
(contract 6.3, 8.6)."""
import math

from stocks.analysis import mean, num, sstdev

NOTE_FINRA = ("FINRA short sale volume counts trades reported to FINRA facilities, much of it market making. "
              "Compare it with this stock's own history, not with other stocks.")
NOTE_INTEREST = ("Short interest is the position open at settlement, reported twice a month. "
                 "Days to cover is short interest divided by average daily volume.")
NOT_ENOUGH = "not enough history"


def _rows(rows):
    """ShortVolumeRow dicts sorted by date, one per date. Also accepts the
    state cache form {"fields": [...], "rows": [[...]]}."""
    if isinstance(rows, dict) and isinstance(rows.get("rows"), list):
        fields = rows.get("fields") or ["date", "short_volume", "exempt_volume", "total_volume"]
        rows = [dict(zip(fields, r)) for r in rows["rows"] if isinstance(r, (list, tuple))]
    by_date = {}
    for r in rows or []:
        if isinstance(r, dict) and isinstance(r.get("date"), str):
            by_date[r["date"][:10]] = r
    return [by_date[d] for d in sorted(by_date)]


def ratio_series(rows):
    """[(date, short_volume / total_volume)] for days with a positive total."""
    out = []
    for r in _rows(rows):
        sv, tv = num(r.get("short_volume")), num(r.get("total_volume"))
        if sv is not None and tv is not None and tv > 0 and sv >= 0:
            out.append((r["date"], sv / tv))
    return out


def zscore(values, window=252, min_n=40):
    """z of the last value against the `window` values before it (the latest
    day excluded; sample standard deviation): {"z", "mean", "std", "n", "percentile"}."""
    if not values:
        return {"z": None, "mean": None, "std": None, "n": 0, "percentile": None}
    last = values[-1]
    base = list(values[-1 - window:-1]) if len(values) > 1 else []
    n = len(base)
    if n < max(2, min_n):
        return {"z": None, "mean": None if not base else round(mean(base), 4), "std": None, "n": n,
                "percentile": None}
    mu = mean(base)
    sd = sstdev(base, mu)
    z = None if not sd else (last - mu) / sd
    pct = sum(1 for v in base if v < last) / n * 100.0
    return {"z": None if z is None else round(z, 2), "mean": round(mu, 4), "std": round(sd, 4), "n": n,
            "percentile": round(pct, 1)}


def z_label(z):
    if z is None:
        return NOT_ENOUGH
    if z >= 2:
        return "unusually high"
    if z >= 1:
        return "high"
    if z > -1:
        return "normal"
    if z > -2:
        return "low"
    return "unusually low"


def rolling_mean(values, n):
    return [math.fsum(values[i - n + 1:i + 1]) / n for i in range(n - 1, len(values))]


def summarize(volume_rows, interest_rows, days=252):
    """The Shorts block (8.6)."""
    rows = _rows(volume_rows)
    series = ratio_series(rows)
    notes = [NOTE_FINRA]
    if not series:
        block = {"status": "not_available", "daily": {"dates": [], "ratio": [], "short_volume": [], "total_volume": []},
                 "latest": None, "avg5": None}
        notes.append("No short sale volume could be read for this stock.")
    else:
        valid = {d for d, _ in series}
        kept = [r for r in rows if r["date"] in valid][-days:]
        ratios = [v for _, v in series]
        z = zscore(ratios)
        last_row = kept[-1]
        latest = {"date": last_row["date"], "ratio": round(ratios[-1], 4),
                  "short_volume": round(num(last_row.get("short_volume")) or 0.0),
                  "total_volume": round(num(last_row.get("total_volume")) or 0.0)}
        latest.update(z)
        latest["label"] = z_label(z["z"])
        avg5 = None
        if len(ratios) >= 5:
            avgs = rolling_mean(ratios, 5)
            z5 = zscore(avgs)
            avg5 = {"ratio": round(avgs[-1], 4), "z": z5["z"], "label": z_label(z5["z"])}
        block = {"status": "ok",
                 "daily": {"dates": [r["date"] for r in kept],
                           "ratio": [round(num(r["short_volume"]) / num(r["total_volume"]), 4) for r in kept],
                           "short_volume": [float(round(num(r["short_volume"]))) for r in kept],
                           "total_volume": [float(round(num(r["total_volume"]))) for r in kept]},
                 "latest": latest, "avg5": avg5}
        latest["short_volume"] = float(latest["short_volume"])
        latest["total_volume"] = float(latest["total_volume"])
        if z["z"] is None:
            notes.append(f"Only {len(ratios)} days of short sale volume so far, so there is no comparison yet.")
    irows = sorted((r for r in (interest_rows or []) if isinstance(r, dict) and r.get("settlement_date")),
                   key=lambda r: r["settlement_date"])
    if irows:
        block["interest"] = {"status": "ok", "rows": irows[-24:], "latest": irows[-1]}
        notes.append(NOTE_INTEREST)
    else:
        block["interest"] = {"status": "not_available", "rows": [], "latest": None}
    block["notes"] = notes
    return block
