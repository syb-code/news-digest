"""Offline helpers for the stock section tests (contract section 16).

Fixture access, synthetic market data and two example documents. Random
numbers come from random.Random(seed) only, so every helper is deterministic.
No helper names a ticker the owner tracks: the examples use AAPL and made-up
values.
"""
import json
import math
import os
import random
import sys
from datetime import date, timedelta

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
FIXTURES = os.path.join(TESTS, "fixtures", "stocks")
_SCRIPTS = os.path.join(ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

EXAMPLE_NOW = "2026-11-13T13:58:02+00:00"
EXAMPLE_AS_OF = "2026-11-12"


# ---------------------------------------------------------------- fixtures

def fixture_path(name):
    return os.path.join(FIXTURES, name)


def fixture_bytes(name):
    with open(fixture_path(name), "rb") as f:
        return f.read()


def fixture_text(name):
    """The fixture as text, a byte order mark dropped (as Response.text() does)."""
    return fixture_bytes(name).decode("utf-8-sig")


def fixture_json(name):
    return json.loads(fixture_text(name))


# ---------------------------------------------------------------- synthetic data

def weekdays(start, n):
    """n weekday dates (YYYY-MM-DD) from start, start included when a weekday."""
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def start_for(end, n):
    """The start date that gives n weekdays ending on end (end included)."""
    d = date.fromisoformat(end)
    count = 0
    while True:
        if d.weekday() < 5:
            count += 1
            if count == n:
                return d.isoformat()
        d -= timedelta(days=1)


def synthetic_bars(n=1300, seed=7, start="2021-06-01", price=100.0, drift=0.0004, vol=0.018):
    """n daily bars in the contract's Bar shape, weekdays only, oldest first.

    A random walk of log returns with the given drift and volatility; open,
    high and low around it; volume log-normal. No dividends and no splits, so
    the adjusted and raw values are equal."""
    rng = random.Random(seed)
    dates = weekdays(start, n)
    bars = []
    close = float(price)
    for d in dates:
        prev = close
        close = prev * math.exp(drift + vol * rng.gauss(0.0, 1.0))
        open_ = prev * math.exp(0.25 * vol * rng.gauss(0.0, 1.0))
        high = max(open_, close) * (1.0 + abs(rng.gauss(0.0, 1.0)) * vol * 0.5)
        low = min(open_, close) * (1.0 - abs(rng.gauss(0.0, 1.0)) * vol * 0.5)
        volume = float(round(2_000_000.0 * math.exp(0.35 * rng.gauss(0.0, 1.0))))
        bars.append({
            "date": d,
            "open": round(open_, 4), "high": round(high, 4), "low": round(low, 4), "close": round(close, 4),
            "volume": volume, "raw_close": round(close, 4), "raw_volume": int(volume),
            "div": 0.0, "split": 1.0,
        })
    return bars


def synthetic_spy(n=1300, seed=11, start="2021-06-01"):
    """A calmer index-like series in the Bar shape."""
    return synthetic_bars(n=n, seed=seed, start=start, price=420.0, drift=0.0003, vol=0.011)


def synthetic_fred(dates, seed=3):
    """The five factor series of macro_impact.FACTORS on the given dates:
    {"DGS10": [(date, value)], "DGS2", "DTWEXBGS", "DCOILWTICO", "VIXCLS"}."""
    rng = random.Random(seed)
    dgs10, dgs2, dollar, oil, vix = 4.0, 3.8, 120.0, 75.0, 18.0
    out = {"DGS10": [], "DGS2": [], "DTWEXBGS": [], "DCOILWTICO": [], "VIXCLS": []}
    for d in dates:
        common = rng.gauss(0.0, 0.04)
        dgs10 = max(0.1, dgs10 + common + rng.gauss(0.0, 0.02))
        dgs2 = max(0.1, dgs2 + common * 1.2 + rng.gauss(0.0, 0.025))
        dollar *= math.exp(rng.gauss(0.0, 0.003))
        oil *= math.exp(rng.gauss(0.0, 0.02))
        vix = max(9.0, vix + 0.08 * (18.0 - vix) + rng.gauss(0.0, 1.1))
        out["DGS10"].append((d, round(dgs10, 2)))
        out["DGS2"].append((d, round(dgs2, 2)))
        out["DTWEXBGS"].append((d, round(dollar, 4)))
        out["DCOILWTICO"].append((d, round(oil, 2)))
        out["VIXCLS"].append((d, round(vix, 2)))
    return out


def synthetic_short_volume(dates, seed=5):
    """ShortVolumeRow per date: a short ratio near 0.45 that wanders a little."""
    rng = random.Random(seed)
    rows = []
    level = 0.45
    for d in dates:
        level = min(0.75, max(0.2, level + 0.1 * (0.45 - level) + rng.gauss(0.0, 0.01)))
        ratio = min(0.95, max(0.05, level + rng.gauss(0.0, 0.03)))
        total = round(1_500_000.0 * math.exp(0.3 * rng.gauss(0.0, 1.0)), 6)
        rows.append({"date": d, "short_volume": round(total * ratio, 6),
                     "exempt_volume": float(round(total * 0.004)), "total_volume": total})
    return rows


def example_inputs(n=800, end=EXAMPLE_AS_OF):
    """bars, spy, fred and short volume ending on end, for examples and tests."""
    start = start_for(end, n)
    bars = synthetic_bars(n=n, seed=7, start=start, price=180.0, drift=0.0005, vol=0.016)
    spy = synthetic_spy(n=n, seed=11, start=start)
    dates = [b["date"] for b in bars]
    fred = synthetic_fred(dates)
    shorts = synthetic_short_volume(dates[-252:])
    return bars, spy, fred, shorts


# ---------------------------------------------------------------- example documents

EXAMPLE_CFG = {"horizons": [5, 20], "bootstrap_years": 5, "min_train_rows": 150, "refit_every": 21,
               "lessons_cap": 12, "chart_days": 400}

_CACHE = {}


def _build_example_document(ticker):
    from stocks import DISCLAIMER, round_pct, round_price
    from stocks.analysis import indicators, learner, levels, macro_impact, shorts

    bars, spy, fred, short_rows = example_inputs()
    state = learner.new_state(ticker, EXAMPLE_NOW)
    state["identity"] = {"cik": 320193, "name": "Apple Inc.", "filer_type": "domestic", "cusips": ["037833100"],
                         "finra_symbol": ticker, "issuer_key": "APPLE"}
    state = learner.bootstrap(state, bars, spy, fred, short_rows, EXAMPLE_CFG, EXAMPLE_NOW)
    tech = indicators.technicals(bars, spy)
    excursions = state["model"]["excursions"]
    lv = levels.levels_block(bars, tech, excursions)
    plan = levels.fallback_plan(lv["candidates"], tech["close"], tech["atr14"])
    history = {"fomc": ["2024-12-18", "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
                        "2025-09-17", "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29",
                        "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28"],
               "cpi": ["2025-%02d-12" % m for m in range(1, 13)] + ["2026-%02d-13" % m for m in range(1, 11)]}
    mi = macro_impact.macro_impact(bars, spy, fred, history)
    interest = [{"settlement_date": "2026-10-15", "short_interest": 128753092.0, "previous": 125000000.0,
                 "change_pct": 3.0, "avg_daily_volume": 41000000.0, "days_to_cover": 3.14},
                {"settlement_date": "2026-10-30", "short_interest": 120763539.0, "previous": 128753092.0,
                 "change_pct": -6.21, "avg_daily_volume": 39858260.0, "days_to_cover": 3.03}]
    sh = shorts.summarize(short_rows, interest)
    last, prev = bars[-1], bars[-2]
    change = last["raw_close"] - prev["raw_close"]
    lb = learner.learner_block(state)
    p20 = (lb.get("probabilities") or {}).get("20")
    stance = "neutral" if p20 is None else ("bullish" if p20 >= 0.55 else "bearish" if p20 <= 0.45 else "neutral")
    assessment = {
        "source": "model", "levels_source": "code", "stance": stance, "confidence": "low",
        "summary": "The trend is up and the stock sits a little above its 20 day average. "
                   "The model's own record on this stock is close to the base rate, so the call is held lightly.",
        "technical": "Price is above the 50 and 200 day averages; RSI is in the middle of its range.",
        "valuation": "P/E is near 30 on the last four quarters of diluted earnings.",
        "shorts": "Short sale volume is in line with this stock's own history.",
        "holders": "Insiders sold under trading plans; no open market buys this year.",
        "macro": "The stock has moved more than usual on CPI days.",
        "risks": ["A hot CPI print could lift yields and weigh on the stock."],
        "entry_zones": plan["entry_zones"], "targets": plan["targets"], "stop": plan["stop"],
        "change_my_mind": "A close below the 200 day average would turn the view neutral.",
        "lessons_added": [],
        "problems": ["The model's price levels did not pass the checks, so these levels come from the code."],
    }
    state = learner.attach_assessment(state, lb["as_of"], assessment, [], "claude-sonnet-5-5", 12)
    lb = learner.learner_block(state)
    doc = {
        "kind": "stocks.ticker", "version": 1, "ticker": ticker, "status": "ok", "notes": [],
        "header": {"ticker": ticker, "name": "Apple Inc.", "cik": 320193, "exchanges": ["Nasdaq"],
                   "filer_type": "domestic", "currency": "USD", "first_date": bars[0]["date"],
                   "last_close": round_price(last["raw_close"]), "prev_close": round_price(prev["raw_close"]),
                   "change": round_price(change), "change_pct": round_pct(change / prev["raw_close"] * 100.0),
                   "bars": len(bars)},
        "chart": indicators.chart(bars, 400),
        "technicals": tech,
        "levels": lv,
        "valuation": {"status": "ok", "pe_ttm": round(last["raw_close"] / 8.97, 2), "eps_ttm": 8.97,
                      "eps_basis": "four_quarters", "eps_currency": "USD", "eps_usd": 8.97, "fx": None,
                      "concept": "us-gaap:EarningsPerShareDiluted",
                      "quarters": [{"start": "2025-09-28", "end": "2025-12-27", "eps": 2.84, "form": "10-Q",
                                    "filed": "2026-01-30", "derived": False},
                                   {"start": "2025-12-28", "end": "2026-03-28", "eps": 2.01, "form": "10-Q",
                                    "filed": "2026-05-01", "derived": False},
                                   {"start": "2026-03-29", "end": "2026-06-27", "eps": 2.02, "form": "10-Q",
                                    "filed": "2026-08-01", "derived": False},
                                   {"start": "2026-06-28", "end": "2026-09-26", "eps": 2.10, "form": "10-K",
                                    "filed": "2026-10-30", "derived": True}],
                      "latest_end": "2026-09-26", "latest_filed": "2026-10-30", "stale": False,
                      "price_used": round_price(last["raw_close"]), "split_adjusted": False, "note": None},
        "shorts": sh,
        "holders": {
            "insiders": {"status": "ok", "window_days": 365, "transactions": [
                {"accession": "0001140361-26-040127", "form": "4", "filed": "2026-10-19", "date": "2026-10-16",
                 "owner": "Example Owner A", "owner_cik": "0009999999",
                 "role": "Officer (Principal Accounting Officer)", "code": "S",
                 "code_label": "Open market or private sale", "acquired_disposed": "D", "shares": 151.0,
                 "price": 243.08, "value": 36705.08, "shares_after": 39417.0, "ownership": "D", "nature": None,
                 "plan_10b5_1": True, "derivative": False, "security": "Common Stock",
                 "url": "https://www.sec.gov/Archives/edgar/data/320193/000114036126040127/form4.xml"}],
                "summary": {"open_market_buys": 0, "open_market_sells": 1, "buy_shares": 0.0, "sell_shares": 151.0,
                            "buy_value": 0.0, "sell_value": 36705.08, "net_shares": -151.0, "insiders": 1,
                            "since": "2025-11-13"},
                "note": "Open market trades are codes P and S. Grants, exercises, tax withholding and gifts "
                        "are listed but not counted."},
            "funds": {"status": "ok", "funds_checked": 16, "rows": [
                {"fund": "Berkshire Hathaway", "cik": 1067983, "period": "2026-06-30",
                 "previous_period": "2026-03-31", "shares": 238212764.0, "value_usd": 57000000000.0,
                 "prev_shares": 280013886.0, "prev_value_usd": 61000000000.0, "change": "trimmed",
                 "change_shares": -41801122.0, "change_pct": -14.93, "form": "13F-HR", "filed": "2026-08-14",
                 "url": "https://www.sec.gov/Archives/edgar/data/1067983/000119312526352200/"}],
                "note": "13F filings show long US positions at quarter end, filed up to 45 days later. "
                        "Options and short positions are not included."},
            "congress": {"status": "ok", "label": "Members of Congress, best effort",
                         "coverage": {"house": "ok", "senate": "off"}, "trades": [
                             {"member": "Hon. Example Member", "chamber": "House", "district": "XX01",
                              "ticker": ticker, "asset": "Apple Inc. - Common Stock", "asset_type": "ST",
                              "type": "partial sale", "owner": "spouse", "date": "2026-09-08",
                              "notified": "2026-09-15", "filed": "2026-09-25", "amount_range": "$1,001 - $15,000",
                              "source": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf"}],
                         "note": "From House periodic transaction reports read from their PDFs. Paper filings "
                                 "cannot be read and are skipped. Senate trades only with an FMP key. Amounts "
                                 "are ranges, filed up to 45 days after the trade."},
        },
        "macro_impact": mi,
        "learner": lb,
        "assessment": assessment,
        "model": "claude-sonnet-5-5", "llm": True,
        "cost": {"input_tokens": 14210, "output_tokens": 3120, "cache_read_tokens": 0, "cost_usd": 0.0596,
                 "cost_estimated": False},
        "generated_at": EXAMPLE_NOW, "data_as_of": bars[-1]["date"],
        "disclaimer": DISCLAIMER,
    }
    return json.dumps(doc, sort_keys=False, allow_nan=False)


def example_document(ticker="AAPL"):
    """A valid section 8 document for ticker, built by the analysis layer from
    synthetic data (a fresh copy on every call)."""
    key = ("doc", ticker)
    if key not in _CACHE:
        _CACHE[key] = _build_example_document(ticker)
    return json.loads(_CACHE[key])


def example_overview():
    """A valid section 9 document (market wide, no tracked ticker)."""
    from stocks import DISCLAIMER

    src_fomc = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    calendar = [
        {"date": "2026-11-13", "time": "08:30", "tz": "America/New_York", "kind": "ppi", "title": "PPI release",
         "flags": {}, "source": "https://www.bls.gov/schedule/news_release/bls.ics"},
        {"date": "2026-11-25", "time": "08:30", "tz": "America/New_York", "kind": "gdp",
         "title": "GDP (Second Estimate), 3rd Quarter 2026", "flags": {},
         "source": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"},
        {"date": "2026-11-25", "time": "10:00", "tz": "America/New_York", "kind": "pce",
         "title": "Personal Income and Outlays, October 2026", "flags": {},
         "source": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"},
        {"date": "2026-12-01", "time": "10:00", "tz": "America/New_York", "kind": "jolts", "title": "JOLTS release",
         "flags": {}, "source": "https://www.bls.gov/schedule/news_release/bls.ics"},
        {"date": "2026-12-04", "time": "08:30", "tz": "America/New_York", "kind": "jobs", "title": "Jobs report",
         "flags": {}, "source": "https://www.bls.gov/schedule/news_release/bls.ics"},
        {"date": "2026-12-09", "time": "14:00", "tz": "America/New_York", "kind": "fomc",
         "title": "FOMC meeting, statement and press conference",
         "flags": {"start_date": "2026-12-08", "press_conference": True, "press_conference_confirmed": False,
                   "projections": True, "tentative": False}, "source": src_fomc},
        {"date": "2026-12-10", "time": "08:30", "tz": "America/New_York", "kind": "cpi", "title": "CPI release",
         "flags": {}, "source": "https://www.bls.gov/schedule/news_release/bls.ics"},
    ]

    def row(id_, label, unit, freq, value, prior, as_of, prior_as_of, series, display=None):
        change = None if value is None or prior is None else round(value - prior, 4)
        return {"id": id_, "label": label, "unit": unit, "frequency": freq, "value": value, "prior": prior,
                "change": change, "as_of": as_of, "prior_as_of": prior_as_of, "display": display,
                "series": series, "status": "ok"}

    macro = [
        row("fed_funds", "Fed funds target range", "percent", "daily", 4.0, 4.25, "2026-11-12", "2026-10-28",
            ["DFEDTARL", "DFEDTARU"], "3.75 to 4.00"),
        row("dgs2", "2 year Treasury yield", "percent", "daily", 4.73, 4.70, "2026-11-10", "2026-11-09", ["DGS2"]),
        row("dgs10", "10 year Treasury yield", "percent", "daily", 4.98, 4.95, "2026-11-10", "2026-11-09", ["DGS10"]),
        row("t10y2y", "10 year minus 2 year", "points", "daily", 0.24, 0.25, "2026-11-10", "2026-11-09", ["T10Y2Y"]),
        row("cpi_yoy", "CPI, year over year", "percent", "monthly", 3.1, 2.9, "2026-10-01", "2026-09-01",
            ["CPIAUCSL"]),
        row("unrate", "Unemployment rate", "percent", "monthly", 4.1, 4.1, "2026-10-01", "2026-09-01", ["UNRATE"]),
        row("vix", "VIX", "index", "daily", 17.49, 18.2, "2026-11-10", "2026-11-09", ["VIXCLS"]),
        {"id": "hy_spread", "label": "High yield spread", "unit": "percent", "frequency": "daily", "value": None,
         "prior": None, "change": None, "as_of": None, "prior_as_of": None, "display": None,
         "series": ["BAMLH0A0HYM2"], "status": "not_available"},
    ]
    news = [
        {"title": "Federal Reserve issues FOMC statement", "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm",
         "source": "Federal Reserve", "kind": "fed", "published": "2026-10-28T18:00:00+00:00",
         "summary": "The Committee decided to lower the target range for the federal funds rate."},
        {"title": "Consumer Price Index, October 2026", "url": "https://www.bls.gov/news.release/cpi.nr0.htm",
         "source": "BLS", "kind": "bls", "published": "2026-11-10T13:30:00+00:00",
         "summary": "The all items index rose 0.3 percent in October."},
    ]
    return {
        "kind": "stocks.overview", "version": 1, "generated_at": EXAMPLE_NOW, "data_as_of": EXAMPLE_AS_OF,
        "calendar": calendar, "macro": macro, "news": news,
        "summary": {"source": "model", "headline": "Inflation data and a Fed meeting set the tone",
                    "summary": "Yields are near their highs of the year. PPI today and CPI next month matter most.",
                    "this_week": [{"date": "2026-11-13", "event": "PPI release",
                                   "why_it_matters": "A firm reading would support higher yields."}],
                    "watch": ["The 10 year yield near 5 percent."],
                    "risks": ["A hot inflation print before the December meeting."],
                    "news": [{"url": news[0]["url"], "why": "The last rate decision."}]},
        "model": "claude-sonnet-5-5",
        "cost": {"input_tokens": 6100, "output_tokens": 2400, "cache_read_tokens": 0, "cost_usd": 0.0362,
                 "cost_estimated": False},
        "run": {"reason": "scheduled", "started_at": "2026-11-13T13:45:10+00:00",
                "finished_at": EXAMPLE_NOW, "tickers": 1, "ok": 1, "partial": 0, "failed": 0,
                "cost_usd": 0.0958, "model": "claude-sonnet-5-5"},
        "disclaimer": DISCLAIMER,
    }
