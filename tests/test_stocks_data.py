"""Offline tests of the stock section's data layer (scripts/stocks/data/).

Every parser runs on the captured fixtures in tests/fixtures/stocks/ and every
fetch runs against a fake transport, so nothing here touches the network. The
tickers are public examples (AAPL, MSFT, SPOT, SPY, Berkshire's holdings) or
made-up symbols.
"""
import contextlib
import gzip
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime, timedelta, timezone

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
FIXTURES = os.path.join(TESTS, "fixtures", "stocks")
DATA_DIR = os.path.join(ROOT, "scripts", "stocks", "data")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from stocks.data import FetchError, HttpClient, Response, safe_reason  # noqa: E402
from stocks.data import calendar as cal  # noqa: E402
from stocks.data import congress, finra, fred, news, prices, sec, trading_days  # noqa: E402

UA = "news-digest tests contact@example.com"

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False


def fb(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def ft(name):
    return fb(name).decode("utf-8-sig")


def fj(name):
    return json.loads(ft(name))


class Clock:
    """A fake monotonic clock; sleep advances it."""

    def __init__(self, t=1000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeTransport:
    """Answers from a route table: url (or (method, url)) -> (status, headers,
    body), an exception, a callable(method, url, headers, body) or a list of
    those consumed in order. Unknown URLs raise a connection error."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body,
                           "timeout": timeout})
        key = (method, url) if (method, url) in self.routes else url
        if key not in self.routes:
            base = url.split("?", 1)[0]
            key = base if base in self.routes else None
        if key is None:
            raise ConnectionRefusedError("no route")
        r = self.routes[key]
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            r = r(method, url, headers, body)
        return r


def ok(body, headers=None, status=200):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    return status, dict(headers or {}), body


def client(routes=None, **kw):
    t = FakeTransport(routes)
    c = Clock()
    http = HttpClient(UA, transport=t, sleep=c.sleep, clock=c, **kw)
    return http, t, c


def ny(y, m, d, hh=0, mm=0):
    from zoneinfo import ZoneInfo

    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))


# ================================================================ HTTP client

class HttpClientTest(unittest.TestCase):
    URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"

    def test_headers_and_gzip(self):
        http, t, _ = client({self.URL: ok(gzip.compress(b"hello"), {"Content-Encoding": "gzip"})})
        r = http.get(self.URL, headers={"Accept": "text/csv", "User-Agent": "other"})
        self.assertEqual(r.body, b"hello")
        sent = t.calls[0]["headers"]
        self.assertEqual(sent["User-Agent"], UA)
        self.assertEqual(sent["Accept-Encoding"], "gzip")
        self.assertEqual(sent["Accept"], "text/csv")
        self.assertNotIn("other", json.dumps(sent))

    def test_text_strips_bom_and_json(self):
        r = Response(200, {"X-A": "1"}, b"\xef\xbb\xbf{\"a\": 1}")
        self.assertEqual(r.headers, {"x-a": "1"})
        self.assertEqual(r.json(), {"a": 1})
        with self.assertRaises(ValueError):
            Response(200, {}, b"not json").json()

    def test_5xx_retries_once(self):
        http, t, c = client({self.URL: [ok(b"", status=503), ok(b"fine")]})
        self.assertEqual(http.get(self.URL).body, b"fine")
        self.assertEqual(c.sleeps, [2.0])
        http, t, c = client({self.URL: [ok(b"", status=503), ok(b"", status=502)]})
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual((e.exception.reason, e.exception.status), ("HTTP 502", 502))
        self.assertEqual(len(t.calls), 2)

    def test_connection_error_retries_once(self):
        http, t, c = client({self.URL: [ConnectionResetError("x"), ok(b"fine")]})
        self.assertEqual(http.get(self.URL).body, b"fine")
        http, t, c = client({self.URL: [TimeoutError("slow"), TimeoutError("slow")]})
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual(e.exception.reason, "connection error")
        self.assertIsNone(e.exception.status)
        self.assertEqual(len(t.calls), 2)

    def test_429_waits_retry_after_at_most_10_seconds_once(self):
        http, t, c = client({self.URL: [ok(b"", {"Retry-After": "30"}, 429), ok(b"fine")]})
        self.assertEqual(http.get(self.URL).body, b"fine")
        self.assertEqual(c.sleeps, [10.0])
        http, t, c = client({self.URL: [ok(b"", {"Retry-After": "3"}, 429), ok(b"", {}, 429)]})
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual((e.exception.reason, e.exception.status), ("rate limited", 429))
        self.assertEqual(c.sleeps, [3.0])

    def test_other_status_and_allow(self):
        http, t, _ = client({self.URL: ok(b"gone", status=404)})
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual((e.exception.reason, e.exception.status), ("HTTP 404", 404))
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(http.get(self.URL, allow=(404,)).status, 404)

    def test_size_cap_plain_and_gzip(self):
        http, _, _ = client({self.URL: ok(b"x" * 101)}, max_bytes=100)
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual(e.exception.reason, "response too large")
        bomb = gzip.compress(b"0" * 5000)
        http, _, _ = client({self.URL: ok(bomb, {"Content-Encoding": "gzip"})}, max_bytes=1000)
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual(e.exception.reason, "response too large")

    def test_sec_requests_are_spaced(self):
        a = "https://data.sec.gov/submissions/CIK0000320193.json"
        b = "https://www.sec.gov/files/company_tickers.json"
        http, t, c = client({a: ok(b"{}"), b: ok(b"{}"), self.URL: ok(b"x")})
        http.get(a)
        http.get(b)
        http.get(self.URL)
        http.get(self.URL)
        self.assertEqual(len(c.sleeps), 1)
        self.assertAlmostEqual(c.sleeps[0], 0.2)
        c.t += 5
        http.get(a)
        self.assertEqual(len(c.sleeps), 1)

    def test_post_json(self):
        url = "https://api.finra.org/data/group/otcMarket/name/regShoDaily"
        http, t, _ = client({("POST", url): ok([])})
        http.post_json(url, {"limit": 1})
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(json.loads(call["body"]), {"limit": 1})
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["headers"]["Accept"], "application/json")

    def test_reasons_never_hold_a_url_ticker_or_user_agent(self):
        url = "https://data.sec.gov/submissions/CIK0000320193.json?ticker=AAPL"
        cases = [ok(b"", status=500), ok(b"", status=404), ok(b"", status=429), ConnectionResetError(url),
                 ok(b"x" * 50)]
        for case in cases:
            http, _, _ = client({url: [case, case]}, max_bytes=10)
            with self.assertRaises(FetchError) as e:
                http.get(url)
            text = str(e.exception) + e.exception.reason + repr(e.exception.args)
            for bad in ("http", "sec.gov", "AAPL", "320193", "contact@example.com"):
                self.assertNotIn(bad, text)
        self.assertNotIn("contact", repr(http))

    def test_budget_and_https_only(self):
        http, t, c = client({self.URL: ok(b"x")}, budget=10)
        http.get(self.URL)
        c.t += 11
        with self.assertRaises(FetchError) as e:
            http.get(self.URL)
        self.assertEqual(e.exception.reason, "time budget used up")
        self.assertEqual(len(t.calls), 1)
        http, _, _ = client()
        with self.assertRaises(FetchError):
            http.get("http://example.com/")
        with self.assertRaises(ValueError):
            HttpClient("")

    def test_cache_within_a_run(self):
        http, t, _ = client({self.URL: ok(b"x")})
        http.get(self.URL, cache=True)
        http.get(self.URL, cache=True)
        self.assertEqual(len(t.calls), 1)
        http.get(self.URL)
        self.assertEqual(len(t.calls), 2)

    def test_attempt(self):
        from stocks.data import attempt

        self.assertEqual(attempt(int, "5"), (5, None))
        self.assertEqual(attempt(int, "x"), (None, "bad data"))
        http, _, _ = client({self.URL: ok(b"", status=404)})
        self.assertEqual(attempt(http.get, self.URL), (None, "HTTP 404"))
        with self.assertRaises(KeyError):
            attempt({}.__getitem__, "x")

    def test_safe_reason(self):
        self.assertEqual(safe_reason(FetchError("HTTP 500", 500)), "HTTP 500")
        self.assertEqual(safe_reason(ValueError("secret AAPL")), "bad data")
        self.assertEqual(safe_reason(KeyError("AAPL")), "KeyError")


# ================================================================ Tiingo

def tiingo_rows():
    return fj("tiingo_prices_synthetic.json")


def tiingo_responder(rows, seen=None):
    def respond(method, url, headers, body):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = q["startDate"][0]
        if seen is not None:
            seen.append(start)
        return ok([r for r in rows if r["date"][:10] >= start])
    return respond


TIINGO_URL = "https://api.tiingo.com/tiingo/daily/EXMPL/prices"


class PricesTest(unittest.TestCase):
    NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)  # 10:00 in New York

    def test_parse_tiingo(self):
        rows = tiingo_rows()
        bad = [dict(rows[0], date="2026-08-31T00:00:00.000Z", adjClose=None),
               dict(rows[0], date="2026-08-28T00:00:00.000Z", adjClose=0), "junk"]
        bars = prices.parse_tiingo(list(reversed(rows)) + bad)
        self.assertEqual(len(bars), 50)
        self.assertEqual(bars[0]["date"], "2026-09-01")
        self.assertEqual(bars[-1]["date"], "2026-11-10")
        b = bars[0]
        self.assertEqual((b["open"], b["high"], b["low"], b["close"]), (49.9256, 50.2656, 49.2078, 49.7494))
        self.assertEqual((b["volume"], b["raw_close"], b["raw_volume"]), (9840094.0, 99.71, 4909530))
        self.assertEqual((b["div"], b["split"]), (0.0, 1.0))
        by = {x["date"]: x for x in bars}
        self.assertEqual(by["2026-09-30"]["div"], 0.24)
        self.assertEqual(by["2026-10-14"]["split"], 2.0)
        self.assertEqual(list(b), prices.FIELDS)
        with self.assertRaises(ValueError):
            prices.parse_tiingo({"detail": "x"})

    def test_fetch_bars_request(self):
        http, t, _ = client({TIINGO_URL: tiingo_responder(tiingo_rows())})
        bars = prices.fetch_bars(http, "EXMPL", "KEY123", date(2026, 10, 1), date(2026, 10, 31))
        self.assertTrue(bars)
        call = t.calls[0]
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        self.assertEqual(q, {"startDate": ["2026-10-01"], "endDate": ["2026-10-31"], "format": ["json"]})
        self.assertEqual(call["headers"]["Authorization"], "Token KEY123")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertNotIn("KEY123", call["url"])
        with self.assertRaises(ValueError):
            prices.fetch_bars(http, "not a ticker", "KEY123", date(2026, 10, 1))

    def test_fetch_bars_error_kinds(self):
        for status, kind in ((401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate"), (500, "network")):
            http, _, _ = client({TIINGO_URL: ok(b'{"detail":"x"}', status=status)})
            with self.assertRaises(prices.PriceError) as e:
                prices.fetch_bars(http, "EXMPL", "KEY123", date(2026, 10, 1))
            self.assertEqual(e.exception.kind, kind)
            self.assertNotIn("KEY123", e.exception.reason)
            self.assertNotIn("EXMPL", e.exception.reason)
        for doc, kind in (({"detail": "Invalid token."}, "auth"), ({"detail": "Error: Ticker 'EXMPL' not found"},
                                                                      "not_found"), ({"x": 1}, "bad_data")):
            http, _, _ = client({TIINGO_URL: ok(doc)})
            with self.assertRaises(prices.PriceError) as e:
                prices.fetch_bars(http, "EXMPL", "KEY123", date(2026, 10, 1))
            self.assertEqual(e.exception.kind, kind)
            self.assertNotIn("EXMPL", e.exception.reason)
        http, _, _ = client({TIINGO_URL: ok(b"<html>")})
        with self.assertRaises(prices.PriceError) as e:
            prices.fetch_bars(http, "EXMPL", "KEY123", date(2026, 10, 1))
        self.assertEqual(e.exception.kind, "bad_data")
        with self.assertRaises(prices.PriceError) as e:
            prices.fetch_bars(http, "EXMPL", "", date(2026, 10, 1))
        self.assertEqual(e.exception.kind, "auth")

    def test_full_fetch_without_cache(self):
        starts = []
        http, t, _ = client({TIINGO_URL: tiingo_responder(tiingo_rows(), starts)})
        cache, bars, info = prices.update_cache(None, http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual(info, {"mode": "full", "rows": 50, "added": 50, "reason": "no cache"})
        self.assertEqual(starts, [(date(2026, 11, 11) - timedelta(days=5 * 366 + 300)).isoformat()])
        self.assertEqual(cache["last_date"], "2026-11-10")
        self.assertEqual(cache["first_date"], "2026-09-01")
        self.assertEqual(prices.bars_from_cache(cache), bars)
        cache2, _, info2 = prices.update_cache({"fields": ["x"]}, http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual(info2["reason"], "cache unreadable")

    def test_cached_day_makes_no_request(self):
        bars = prices.parse_tiingo(tiingo_rows())
        cache = prices.cache_from_bars(bars, "2026-11-10T23:00:00+00:00")
        http, t, _ = client()
        new, out, info = prices.update_cache(cache, http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual(t.calls, [])
        self.assertEqual(info, {"mode": "cached", "rows": 50, "added": 0, "reason": None})
        self.assertEqual(out, bars)
        # An evening run on 2026-11-11 expects that day's close, so it asks.
        evening = datetime(2026, 11, 11, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(trading_days.last_closed_day(evening).isoformat(), "2026-11-11")
        http, t, _ = client({TIINGO_URL: tiingo_responder(tiingo_rows())})
        _, _, info = prices.update_cache(cache, http, "EXMPL", "K", evening, 5)
        self.assertEqual(info["mode"], "delta")
        self.assertEqual(len(t.calls), 1)

    def test_delta_merge(self):
        rows = tiingo_rows()
        bars = prices.parse_tiingo(rows)
        cache = prices.cache_from_bars(bars[:45], "2026-11-03T23:00:00+00:00")
        starts = []
        http, t, _ = client({TIINGO_URL: tiingo_responder(rows, starts)})
        new, out, info = prices.update_cache(cache, http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual(info, {"mode": "delta", "rows": 50, "added": 5, "reason": None})
        last = date.fromisoformat(bars[44]["date"])
        self.assertEqual(starts, [(last - timedelta(days=7)).isoformat()])
        self.assertEqual(out, bars)
        self.assertEqual(new["fetched_at"], "2026-11-11T15:00:00+00:00")

    def test_split_and_dividend_force_a_full_fetch(self):
        rows = tiingo_rows()
        bars = prices.parse_tiingo(rows)
        for cut, label in ((28, "split"), (18, "dividend")):
            cache = prices.cache_from_bars(bars[:cut], "x")
            starts = []
            http, t, _ = client({TIINGO_URL: tiingo_responder(rows, starts)})
            new, out, info = prices.update_cache(cache, http, "EXMPL", "K", self.NOW, 5)
            self.assertEqual(info["mode"], "full", label)
            self.assertEqual(info["reason"], "dividend or split", label)
            self.assertEqual(len(starts), 2, label)
            self.assertEqual(out, bars)

    def test_changed_adjusted_close_forces_a_full_fetch(self):
        rows = tiingo_rows()
        bars = prices.parse_tiingo(rows)
        old = [dict(b) for b in bars[:45]]
        old[-1]["close"] = round(old[-1]["close"] * 1.01, 4)
        cache = prices.cache_from_bars(old, "x")
        http, t, _ = client({TIINGO_URL: tiingo_responder(rows)})
        _, out, info = prices.update_cache(cache, http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual((info["mode"], info["reason"]), ("full", "adjusted prices changed"))
        self.assertEqual(out, bars)
        # A change within 0.05 percent merges.
        old[-1]["close"] = round(bars[44]["close"] * 1.0004, 4)
        http, t, _ = client({TIINGO_URL: tiingo_responder(rows)})
        _, _, info = prices.update_cache(prices.cache_from_bars(old, "x"), http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual(info["mode"], "delta")

    def test_keeps_at_most_years_times_252_plus_300_bars(self):
        d0 = date(2025, 1, 2)
        rows, d = [], d0
        while len(rows) < 420:
            if d.weekday() < 5:
                p = 100 + len(rows) * 0.1
                rows.append({"date": d.isoformat() + "T00:00:00.000Z", "close": p, "high": p + 1, "low": p - 1,
                             "open": p, "volume": 1000, "adjClose": p, "adjHigh": p + 1, "adjLow": p - 1,
                             "adjOpen": p, "adjVolume": 1000, "divCash": 0.0, "splitFactor": 1.0})
            d += timedelta(days=1)
        now = datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(hours=15)
        http, _, _ = client({TIINGO_URL: ok(rows)})  # more rows than the window asks for
        cache, bars, info = prices.update_cache(None, http, "EXMPL", "K", now, 0)
        self.assertEqual(prices.max_bars(0), 300)
        self.assertEqual(len(bars), 300)
        self.assertEqual(bars[-1]["date"], rows[-1]["date"][:10])

    def test_spy_bars(self):
        starts = []
        url = "https://api.tiingo.com/tiingo/daily/SPY/prices"
        http, t, _ = client({url: tiingo_responder(tiingo_rows(), starts)})
        bars = prices.spy_bars(http, "K", self.NOW, 5)
        self.assertEqual(len(bars), 50)
        self.assertEqual(starts, [prices.full_start(self.NOW, 5).isoformat()])

    def test_errors_propagate_for_a_cache_fallback(self):
        bars = prices.parse_tiingo(tiingo_rows())
        cache = prices.cache_from_bars(bars[:45], "x")
        http, _, _ = client({TIINGO_URL: ok(b"", status=429)})
        with self.assertRaises(prices.PriceError) as e:
            prices.update_cache(cache, http, "EXMPL", "K", self.NOW, 5)
        self.assertEqual(e.exception.kind, "rate")
        self.assertEqual(prices.bars_from_cache(cache), bars[:45])

    def test_bars_from_cache_rejects_malformed(self):
        good = prices.cache_from_bars(prices.parse_tiingo(tiingo_rows()), "x")
        for broken in (None, {}, dict(good, fields=["date"]), dict(good, rows=[["2026-01-01"]]),
                       dict(good, rows=[good["rows"][1], good["rows"][0]]),
                       dict(good, rows=[["2026-13-01"] + good["rows"][0][1:]]),
                       dict(good, rows=[good["rows"][0][:4] + ["x"] + good["rows"][0][5:]]),
                       dict(good, rows=[])):
            with self.assertRaises(ValueError):
                prices.bars_from_cache(broken)


# ================================================================ SEC

def ticker_map():
    return sec.parse_company_tickers(fj("sec_company_tickers.json"))


class SecIdentityTest(unittest.TestCase):
    def test_company_tickers(self):
        m = ticker_map()
        self.assertEqual(len(m), 24)
        self.assertEqual(m["AAPL"], {"cik": 320193, "name": "Apple Inc.", "classes": 1})
        self.assertEqual(m["GOOGL"]["classes"], 2)
        self.assertIn("BRK-B", m)
        with self.assertRaises(ValueError):
            sec.parse_company_tickers({})

    def test_company_tickers_request_is_cached(self):
        http, t, _ = client({sec.TICKERS: ok(fb("sec_company_tickers.json"))})
        sec.company_tickers(http)
        sec.company_tickers(http)
        self.assertEqual(len(t.calls), 1)

    def test_identity(self):
        m = ticker_map()
        ident = sec.identity("aapl", m, fj("sec_submissions_aapl.json"), cusips=["037833100", "bad", "037833100"])
        self.assertEqual(ident, {"ticker": "AAPL", "cik": 320193, "name": "Apple Inc.", "filer_type": "domestic",
                                 "exchanges": ["Nasdaq"], "cusips": ["037833100"], "finra_symbol": "AAPL",
                                 "issuer_key": "APPLE", "classes": 1})
        brk = sec.identity("BRK.B", m, None)
        self.assertEqual((brk["ticker"], brk["finra_symbol"], brk["filer_type"], brk["classes"]),
                         ("BRK-B", "BRK/B", "unknown", 2))
        unknown = sec.identity("EXMPL", m, None)
        self.assertIsNone(unknown["cik"])
        self.assertIsNone(unknown["issuer_key"])
        with self.assertRaises(ValueError):
            sec.identity("not valid", m, None)

    def test_filer_type(self):
        self.assertEqual(sec.filer_type(fj("sec_submissions_aapl.json")), "domestic")
        foreign = {"cik": "1", "filings": {"recent": {"form": ["6-K", "20-F", "4"], "filingDate": ["2026-01-01"] * 3}}}
        self.assertEqual(sec.filer_type(foreign), "foreign")
        self.assertEqual(sec.filer_type({"filings": {"recent": {"form": ["S-1"]}}}), "unknown")
        self.assertEqual(sec.filer_type({}), "unknown")

    def test_submissions_reads_one_older_page_when_needed(self):
        url = sec.SUBMISSIONS.format(cik=320193)
        subs = fj("sec_submissions_aapl.json")
        page = {"form": ["4"], "filingDate": ["2025-06-02"], "accessionNumber": ["0000000001-25-000001"],
                "reportDate": ["2025-05-30"], "primaryDocument": ["xslF345X05/doc.xml"]}
        page_url = sec.SUBMISSIONS_PAGE.format(name="CIK0000320193-submissions-001.json")
        http, t, _ = client({url: ok(subs), page_url: ok(page)})
        plain = sec.submissions(http, 320193)
        self.assertNotIn("older", plain["filings"])
        recent_only = sec.form4_refs(plain, date(2025, 1, 1))
        full = sec.submissions(http, 320193, since=date(2025, 1, 1))
        self.assertIn("older", full["filings"])
        refs = sec.form4_refs(full, date(2025, 1, 1))
        self.assertEqual(len(refs), len(recent_only) + 1)
        self.assertEqual(refs[-1]["document"], "doc.xml")
        # A year that the recent block covers needs no extra request.
        n = len(t.calls)
        sec.submissions(http, 320193, since=date(2026, 1, 1))
        self.assertEqual(len(t.calls), n)


class SecEpsTest(unittest.TestCase):
    def test_aapl_four_quarters_with_derived_q4(self):
        e = sec.eps_summary(fj("sec_companyfacts_aapl.json"))
        self.assertEqual(e["status"], "ok")
        self.assertEqual(e["concept"], "us-gaap:EarningsPerShareDiluted")
        self.assertEqual((e["unit"], e["currency"], e["basis"]), ("USD/shares", "USD", "four_quarters"))
        self.assertEqual(e["eps_ttm"], 8.97)
        self.assertEqual([(q["end"], q["eps"], q["derived"]) for q in e["quarters"]],
                         [("2025-12-27", 2.84, False), ("2026-03-28", 2.01, False), ("2026-06-27", 2.02, False),
                          ("2026-09-26", 2.1, True)])
        self.assertEqual(e["quarters"][-1]["form"], "10-K")
        self.assertEqual(e["quarters"][-1]["start"], "2026-06-28")
        self.assertEqual(e["annual"]["eps"], 8.98)
        self.assertEqual((e["latest_end"], e["latest_filed"], e["note"]), ("2026-09-26", "2026-11-03", None))

    def test_ifrs_annual_and_fx(self):
        e = sec.eps_summary(fj("sec_companyfacts_spot_ifrs.json"))
        self.assertEqual((e["status"], e["basis"], e["eps_ttm"], e["currency"]), ("ok", "annual", 10.51, "EUR"))
        self.assertEqual(e["concept"], "ifrs-full:DilutedEarningsLossPerShare")
        self.assertEqual((e["annual"]["start"], e["annual"]["end"]), ("2025-01-01", "2025-12-31"))
        data = {"DEXUSEU": fred.parse_csv(ft("fred_dexuseu.csv"), "DEXUSEU")}
        fx = fred.fx_usd_per_unit(data, e["currency"], {"EUR": {"series": "DEXUSEU", "quote": "usd_per_unit"}})
        self.assertEqual(fx, (1.1154, "DEXUSEU", "2026-11-06"))
        self.assertAlmostEqual(e["eps_ttm"] * fx[0], 11.7229, places=3)

    def test_no_eps(self):
        e = sec.eps_summary(fj("sec_companyfacts_no_eps.json"))
        self.assertEqual(e["status"], "not_available")
        self.assertEqual(e["note"], "No earnings per share in the SEC data yet.")
        self.assertIsNone(e["eps_ttm"])
        self.assertEqual(sec.eps_summary({})["status"], "not_available")
        self.assertEqual(sec.eps_summary(None)["status"], "not_available")

    def test_basic_concept_fallback_and_negative_quarters(self):
        rows = []
        for start, end, val in (("2025-10-01", "2025-12-31", -0.5), ("2026-01-01", "2026-03-31", 0.25),
                                ("2026-04-01", "2026-06-30", -1.0), ("2026-07-01", "2026-09-30", 0.1)):
            rows.append({"start": start, "end": end, "val": val, "form": "10-Q", "filed": "2026-11-01"})
        facts = {"facts": {"us-gaap": {"EarningsPerShareBasic": {"units": {"USD/shares": rows}}}}}
        e = sec.eps_summary(facts)
        self.assertEqual((e["concept"], e["basis"], e["eps_ttm"]), ("us-gaap:EarningsPerShareBasic",
                                                                  "four_quarters", -1.15))

    def test_restated_value_wins(self):
        rows = [{"start": "2026-01-01", "end": "2026-03-31", "val": 4.0, "form": "10-Q", "filed": "2026-05-01"},
                {"start": "2026-01-01", "end": "2026-03-31", "val": 1.0, "form": "10-Q", "filed": "2027-05-01"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": 3.0, "form": "10-K", "filed": "2026-02-01"}]
        facts = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": rows}}}}}
        e = sec.eps_summary(facts)
        self.assertEqual((e["basis"], e["eps_ttm"]), ("annual", 3.0))
        self.assertEqual(e["quarters"][-1]["eps"], 1.0)

    def test_stale_quarters_lose_to_a_newer_year(self):
        rows = [{"start": s, "end": e, "val": 1.0, "form": "10-Q", "filed": "2020-01-01"}
                for s, e in (("2018-01-01", "2018-03-31"), ("2018-04-01", "2018-06-30"),
                             ("2018-07-01", "2018-09-30"), ("2018-10-01", "2018-12-31"))]
        rows.append({"start": "2025-01-01", "end": "2025-12-31", "val": 7.0, "form": "20-F", "filed": "2026-03-01"})
        facts = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": rows}}}}}
        e = sec.eps_summary(facts)
        self.assertEqual((e["basis"], e["eps_ttm"], e["latest_end"]), ("annual", 7.0, "2025-12-31"))

    def test_company_facts_404_is_empty(self):
        url = sec.FACTS.format(cik=2)
        http, _, _ = client({url: ok(b"", status=404)})
        self.assertEqual(sec.company_facts(http, 2), {})
        self.assertEqual(sec.eps_for(http, 2)["note"], "No earnings per share in the SEC data yet.")
        http, _, _ = client({url: ok(b"", status=503)})
        e = sec.eps_for(http, 2)
        self.assertEqual((e["status"], e["note"]), ("not_available",
                                                    "Earnings per share could not be read from the SEC today."))
        http, _, _ = client({url: ok(fb("sec_companyfacts_aapl.json"))})
        self.assertEqual(sec.eps_for(http, 2)["eps_ttm"], 8.97)

    def test_eps_due(self):
        subs = fj("sec_submissions_aapl.json")
        now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        fresh = {"summary": {"latest_filed": "2026-11-03"}, "fetched_at": "2026-11-05T00:00:00+00:00",
                 "latest_filed": "2026-11-03"}
        self.assertFalse(sec.eps_due(subs, fresh, now))
        self.assertTrue(sec.eps_due(subs, dict(fresh, latest_filed="2026-08-01",
                                               summary={"latest_filed": "2026-08-01"}), now))
        self.assertTrue(sec.eps_due(subs, dict(fresh, fetched_at="2026-09-01T00:00:00+00:00"), now))
        self.assertTrue(sec.eps_due(subs, None, now))


class SecForm4Test(unittest.TestCase):
    def setUp(self):
        self.subs = fj("sec_submissions_aapl.json")

    def test_refs(self):
        refs = sec.form4_refs(self.subs, date(2026, 9, 1))
        self.assertEqual(len(refs), 23)
        self.assertEqual(refs[0], {"cik": 320193, "accession": "0001140361-26-042149", "form": "4",
                                   "filed": "2026-11-05", "report_date": "2026-11-03", "document": "form4.xml"})
        self.assertTrue(all(r["filed"] >= "2026-09-01" for r in refs))
        self.assertEqual(refs, sorted(refs, key=lambda r: (r["filed"], r["accession"]), reverse=True))
        older = sec.form4_refs(self.subs, "2026-01-01")
        self.assertTrue(any(r["document"].startswith("wk-form4_") for r in older))
        self.assertEqual(sec.form4_url(refs[0]),
                         "https://www.sec.gov/Archives/edgar/data/320193/000114036126042149/form4.xml")

    def test_exercise_and_sale(self):
        ref = {"cik": 320193, "accession": "0001140361-26-040127", "form": "4", "filed": "2026-10-19",
               "document": "form4.xml"}
        tx = sec.parse_form4(fb("sec_form4_exercise_and_sale.xml"), ref)
        self.assertEqual([t["code"] for t in tx], ["M", "F", "S", "S", "S", "S", "S", "M", "M", "M", "M"])
        self.assertEqual([t["derivative"] for t in tx], [False] * 7 + [True] * 4)
        m, f, s = tx[0], tx[1], tx[2]
        self.assertEqual((m["shares"], m["price"], m["value"], m["acquired_disposed"]), (1714.0, None, None, "A"))
        self.assertEqual(m["code_label"], "Exercise or conversion of a derivative")
        self.assertEqual((f["price"], f["value"], f["code_label"]),
                         (342.0, 301986.0, "Shares withheld for tax or exercise price"))
        self.assertEqual(s, {
            "accession": "0001140361-26-040127", "form": "4", "filed": "2026-10-19", "date": "2026-10-16",
            "owner": "Example Owner A", "owner_cik": "0009999999", "role": "Officer (Principal Accounting Officer)",
            "code": "S", "code_label": "Open market or private sale", "acquired_disposed": "D", "shares": 151.0,
            "price": 343.08, "value": 51805.08, "shares_after": 39417.0, "ownership": "D", "nature": None,
            "plan_10b5_1": True, "derivative": False, "security": "Common Stock",
            "url": "https://www.sec.gov/Archives/edgar/data/320193/000114036126040127/form4.xml"})
        self.assertEqual(tx[7]["security"], "Restricted Stock Unit")
        self.assertIsNone(tx[7]["price"])

    def test_planned_sale(self):
        ref = {"cik": 320193, "accession": "0001140361-26-042149", "form": "4", "filed": "2026-11-05",
               "document": "form4.xml"}
        tx = sec.parse_form4(fb("sec_form4_planned_sale.xml"), ref)
        self.assertEqual(len(tx), 1)
        t = tx[0]
        self.assertEqual((t["code"], t["shares"], t["price"], t["value"], t["shares_after"]),
                         ("S", 2399.0, 325.75, 781474.25, 29997.0))
        self.assertEqual(t["role"], "Officer (SVP, GC and Government Affairs)")
        self.assertTrue(t["plan_10b5_1"])

    def test_several_owners_roles_and_numeric_booleans(self):
        xml = b"""<?xml version="1.0"?><ownershipDocument><documentType>4</documentType>
        <periodOfReport>2026-10-01</periodOfReport><aff10b5One>0</aff10b5One>
        <reportingOwner><reportingOwnerId><rptOwnerCik>0000000001</rptOwnerCik><rptOwnerName>Fund One</rptOwnerName>
        </reportingOwnerId><reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>0</isOfficer>
        <isTenPercentOwner>1</isTenPercentOwner><isOther>1</isOther><otherText>Member of a group</otherText>
        </reportingOwnerRelationship></reportingOwner>
        <reportingOwner><reportingOwnerId><rptOwnerName>Fund Two</rptOwnerName></reportingOwnerId></reportingOwner>
        <nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle>
        <transactionCoding><transactionCode>Q</transactionCode></transactionCoding>
        <transactionAmounts><transactionShares><value>10</value></transactionShares>
        <transactionPricePerShare><value>2.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
        <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
        <natureOfOwnership><value>By LLC</value></natureOfOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeHolding><securityTitle><value>Common</value></securityTitle></nonDerivativeHolding>
        </nonDerivativeTable></ownershipDocument>"""
        ref = {"cik": 1, "accession": "0000000001-26-000001", "form": "4/A", "filed": "2026-10-03",
               "document": "a.xml"}
        tx = sec.parse_form4(xml, ref)
        self.assertEqual(len(tx), 1)
        t = tx[0]
        self.assertEqual(t["owner"], "Fund One and Fund Two")
        self.assertEqual(t["role"], "Director, 10 percent owner, Other (Member of a group)")
        self.assertEqual((t["date"], t["form"], t["code_label"], t["plan_10b5_1"]), ("2026-10-01", "4/A", "Other",
                                                                                     False))
        self.assertEqual((t["ownership"], t["nature"], t["value"]), ("I", "By LLC", 25.0))
        with self.assertRaises(ValueError):
            sec.parse_form4(b"<other/>", ref)
        with self.assertRaises(ValueError):
            sec.parse_form4(b'<!DOCTYPE x [<!ENTITY a "b">]><ownershipDocument/>', ref)
        with self.assertRaises(ValueError):
            sec.parse_form4(b"<ownershipDocument>", ref)

    def test_insider_transactions_reads_only_new_documents(self):
        since = date(2026, 10, 1)
        refs = sec.form4_refs(self.subs, since)
        routes = {sec.form4_url(r): ok(fb("sec_form4_planned_sale.xml")) for r in refs}
        routes[sec.form4_url(refs[1])] = ok(b"", status=404)
        http, t, _ = client(routes)
        first = sec.insider_transactions(http, self.subs, since, None, limit=3)
        self.assertEqual((first["read"], first["failed"], first["status"]), (2, 1, "partial"))
        self.assertEqual(first["skipped"], len(refs) - 3)
        self.assertEqual(len(first["seen"]), 2)
        second = sec.insider_transactions(http, self.subs, since,
                                          {"seen": first["seen"], "transactions": first["transactions"]},
                                          limit=100)
        self.assertEqual(second["read"], len(refs) - 3)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(len(second["seen"]), len(refs) - 1)
        fetched = [c["url"] for c in t.calls]
        self.assertEqual(len(fetched), len(set(fetched)) + 1)  # only the failed one is asked twice
        dates = [x["date"] for x in second["transactions"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


def q1():
    return sec.parse_info_table(fb("sec_13f_infotable_2026q1.xml"))


def q2():
    return sec.parse_info_table(fb("sec_13f_infotable_2026q2.xml"))


class Sec13FTest(unittest.TestCase):
    FUND = {"name": "Berkshire Hathaway", "cik": 1067983}
    CUR = {"cik": 1067983, "accession": "0001193125-26-352200", "form": "13F-HR", "period": "2026-06-30",
           "filed": "2026-08-14"}
    PREV = {"cik": 1067983, "accession": "0001193125-26-226661", "form": "13F-HR", "period": "2026-03-31",
            "filed": "2026-05-15"}

    def row(self, cusip):
        a, b = q2()["holdings"].get(cusip), q1()["holdings"].get(cusip)
        return sec.fund_row(self.FUND, a, b, self.CUR, self.PREV)

    def test_tables(self):
        a, b = q1(), q2()
        self.assertEqual(len(a["holdings"]), 12)
        self.assertEqual(len(b["holdings"]), 12)
        self.assertEqual((b["options_rows"], b["prn_rows"], b["rows"]), (2, 1, 45))
        self.assertNotIn("002121AB7", b["holdings"])  # A10 Networks, a principal amount row
        ally = b["holdings"]["02005N100"]
        self.assertEqual((ally["shares"], ally["rows"], ally["name"], ally["title"]),
                         (27000000.0, 6, "ALLY FINL INC", "COM"))
        self.assertEqual(b["holdings"]["191216100"]["shares"], 400000000.0)  # the two option rows left out

    def test_quarter_over_quarter_by_cusip(self):
        ally = self.row("02005N100")
        self.assertEqual((ally["change"], ally["prev_shares"], ally["shares"], ally["change_shares"],
                          ally["change_pct"]), ("trimmed", 29000000.0, 27000000.0, -2000000.0, -6.9))
        self.assertEqual(ally["url"], "https://www.sec.gov/Archives/edgar/data/1067983/000119312526352200/")
        self.assertEqual((ally["period"], ally["previous_period"], ally["form"], ally["filed"]),
                         ("2026-06-30", "2026-03-31", "13F-HR", "2026-08-14"))
        bac = self.row("060505104")
        self.assertEqual(q1()["holdings"]["060505104"]["name"], "BANK AMERICA CORP")
        self.assertEqual(q2()["holdings"]["060505104"]["name"], "BANK OF AMER CORP")
        self.assertEqual((bac["change"], bac["prev_shares"], bac["shares"]), ("trimmed", 513624165.0, 483394015.0))
        self.assertEqual(self.row("H1467J104")["change"], "unchanged")
        stz = self.row("21036P108")
        self.assertEqual((stz["change"], stz["shares"], stz["change_pct"]), ("exited", 0.0, -100.0))
        dhi = self.row("23331A109")
        self.assertEqual((dhi["change"], dhi["shares"], dhi["prev_shares"], dhi["change_pct"]),
                         ("new", 3564.0, 0.0, None))
        for cusip in ("247361702", "02079K305", "02079K107"):
            self.assertEqual(self.row(cusip)["change"], "added", cusip)
        for cusip in ("025816109", "191216100"):
            self.assertEqual(self.row(cusip)["change"], "unchanged", cusip)
        self.assertIsNone(sec.fund_row(self.FUND, None, None, self.CUR, self.PREV))
        only = sec.fund_row(self.FUND, {"shares": 5.0, "value_usd": 50.0}, None, self.CUR, None)
        self.assertEqual((only["change"], only["prev_shares"], only["previous_period"]), ("new", None, None))

    def test_matching_by_name_and_cusip(self):
        m = ticker_map()
        table = q2()
        cases = {"ALLY": "02005N100", "BAC": "060505104", "CB": "H1467J104", "DAL": "247361702",
                 "KO": "191216100", "AXP": "025816109", "KR": "501044101"}
        for t, cusip in cases.items():
            h, note = sec.match_holding_detail(table, sec.identity(t, m, None))
            self.assertEqual((h or {}).get("cusip"), cusip, t)
            self.assertIsNone(note)
        self.assertEqual(sec.match_holding(q1(), sec.identity("STZ", m, None))["cusip"], "21036P108")
        self.assertIsNone(sec.match_holding(table, sec.identity("AAPL", m, None)))
        # Alphabet's two classes share one SEC title: ambiguous by design.
        for t in ("GOOGL", "GOOG"):
            h, note = sec.match_holding_detail(table, sec.identity(t, m, None))
            self.assertIsNone(h)
            self.assertEqual(note, "Several share classes match; add the CUSIP to the overrides.")
        goog = sec.identity("GOOG", m, None)
        self.assertEqual(sec.match_holding(table, goog, {"GOOG": {"cusips": ["02079K107"]}})["cusip"], "02079K107")
        self.assertEqual(sec.match_holding(table, dict(goog, cusips=["02079K305"]))["cusip"], "02079K305")
        # Chubb's old name does not match, but the CUSIP learned from the new one does.
        cb = sec.identity("CB", m, None)
        self.assertIsNone(sec.match_holding(q1(), cb))
        self.assertEqual(sec.match_holding(q1(), dict(cb, cusips=["H1467J104"]))["shares"], 34249183.0)

    def test_horton_name_order_miss_and_override(self):
        m = ticker_map()
        dhi = sec.identity("DHI", m, None)
        self.assertEqual(dhi["issuer_key"], "HORTON D R")
        self.assertEqual(sec.issuer_key("D R HORTON INC"), "D R HORTON")
        self.assertIsNone(sec.match_holding(q2(), dhi))
        self.assertEqual(sec.match_holding(q2(), dhi, {"DHI": {"names": ["D R HORTON INC"]}})["shares"], 3564.0)
        self.assertEqual(sec.match_holding(q2(), dhi, {"cusips": ["23331a109"]})["cusip"], "23331A109")

    def test_issuer_key(self):
        pairs = [("ALLY FINL INC", "ALLY FINANCIAL"), ("Ally Financial Inc.", "ALLY FINANCIAL"),
                 ("BANK OF AMER CORP", "BANK OF AMERICA"), ("BANK OF AMERICA CORP /DE/", "BANK OF AMERICA"),
                 ("CHUBB LIMITED", "CHUBB"), ("Chubb Ltd", "CHUBB"), ("DELTA AIR LINES INC", "DELTA AIR LINES"),
                 ("DELTA AIR LINES, INC.", "DELTA AIR LINES"), ("Spotify Technology S.A.", "SPOTIFY TECHNOLOGY"),
                 ("SPOTIFY TECHNOLOGY S A", "SPOTIFY TECHNOLOGY"), ("PROCTER & GAMBLE Co", "PROCTER AND GAMBLE"),
                 ("OCCIDENTAL PETROLEUM CORP /DE/", "OCCIDENTAL PETROLEUM"), ("OCCIDENTAL PETE CORP",
                                                                            "OCCIDENTAL PETROLEUM"),
                 ("Example Holdings Group, Inc. /NEW/", "EXAMPLE"), ("CISCO SYSTEMS, INC.", "CISCO SYSTEMS"),
                 ("COCA-COLA CO", "COCA COLA"), ("", "")]
        for name, key in pairs:
            self.assertEqual(sec.issuer_key(name), key, name)

    def test_share_class_rules(self):
        def h(cusip, name, title, shares=1.0):
            return {"cusip": cusip, "name": name, "title": title, "shares": shares, "value_usd": 1.0, "rows": 1}
        table = {"holdings": {"084670108": h("084670108", "BERKSHIRE HATHAWAY INC DEL", "CL A"),
                              "084670702": h("084670702", "BERKSHIRE HATHAWAY INC DEL", "CL B NEW"),
                              "084670AA1": h("084670AA1", "BERKSHIRE HATHAWAY INC DEL", "NOTE 1.5%")}}
        m = ticker_map()
        self.assertEqual(sec.match_holding(table, sec.identity("BRK-B", m, None))["cusip"], "084670702")
        self.assertEqual(sec.match_holding(table, sec.identity("BRK-A", m, None))["cusip"], "084670108")
        one = {"ticker": "EXMPL", "issuer_key": "EXAMPLE", "classes": 1, "cusips": []}
        table = {"holdings": {"000000001": h("000000001", "EXAMPLE CORP", "COM"),
                              "000000002": h("000000002", "EXAMPLE CORP", "PFD SER A"),
                              "000000003": h("000000003", "EXAMPLE CORP", "DEBT 5% 2030")}}
        self.assertEqual(sec.match_holding(table, one)["cusip"], "000000001")
        self.assertIsNone(sec.match_holding(table, dict(one, classes=2)))

    def test_cover_pages(self):
        r = sec.parse_cover(fb("sec_13f_cover_restatement.xml"))
        self.assertEqual((r["form"], r["period"], r["is_amendment"], r["amendment_type"], r["report_type"]),
                         ("13F-HR/A", "2026-06-30", True, "RESTATEMENT", "13F HOLDINGS REPORT"))
        self.assertEqual((r["entries"], r["value_total"]), (16122, 875011794337.0))
        n = sec.parse_cover(fb("sec_13f_cover_notice.xml"))
        self.assertEqual((n["form"], n["report_type"], n["is_amendment"], n["amendment_type"], n["entries"]),
                         ("13F-NT", "13F NOTICE", False, None, None))
        self.assertEqual(n["other_managers"], [{"cik": "0002026053", "name": "PERSHING SQUARE INC."}])
        with self.assertRaises(ValueError):
            sec.parse_cover(fb("sec_form4_planned_sale.xml"))

    def test_fund_refs_and_pick_periods(self):
        refs = sec.fund_refs(fj("sec_submissions_berkshire.json"))
        self.assertTrue(refs)
        self.assertTrue(all(r["form"] in ("13F-HR", "13F-HR/A", "13F-NT") for r in refs))
        picks = sec.pick_periods(refs)
        self.assertEqual([p["period"] for p in picks], ["2026-06-30", "2026-03-31"])
        self.assertEqual(picks[0]["accession"], "0001193125-26-352200")

        def ref(acc, form, period, filed):
            return {"cik": 1, "accession": acc, "form": form, "period": period, "filed": filed}
        refs = [ref("0000000001-26-000009", "13F-NT", "2026-09-30", "2026-11-10"),
                ref("0000000001-26-000005", "13F-HR", "2026-06-30", "2026-08-14"),
                ref("0000000001-26-000008", "13F-HR/A", "2026-06-30", "2026-09-02"),
                ref("0000000001-26-000003", "13F-HR", "2026-03-31", "2026-05-15"),
                ref("0000000001-26-000004", "13F-HR/A", "2026-03-31", "2026-05-20"),
                ref("0000000001-26-000006", "13F-HR/A", "2026-03-31", "2026-06-01"),
                ref("0000000001-26-000001", "13F-HR", "2025-12-31", "2026-02-14")]
        need = sec.covers_needed(refs, 2)
        self.assertEqual(sorted(r["accession"] for r in need),
                         ["0000000001-26-000004", "0000000001-26-000006", "0000000001-26-000008"])
        covers = {"0000000001-26-000008": {"amendment_type": "RESTATEMENT"},
                  "0000000001-26-000004": {"amendment_type": "NEW HOLDINGS"}}
        picks = sec.pick_periods(refs, covers, 2)
        self.assertEqual([(p["period"], p["accession"]) for p in picks],
                         [("2026-06-30", "0000000001-26-000008"), ("2026-03-31", "0000000001-26-000003")])
        self.assertEqual([m["accession"] for m in picks[1]["merge"]], ["0000000001-26-000004"])
        self.assertEqual(picks[0]["merge"], [])
        # Without covers an amendment is left out and the original stands.
        self.assertEqual(sec.pick_periods(refs, None, 1)[0]["accession"], "0000000001-26-000005")
        self.assertEqual(len(sec.pick_periods(refs, covers, 3)), 3)

    def test_index_table_and_fund_periods_over_http(self):
        cik, acc = 1067983, "0001193125-26-352200"
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/000119312526352200/"
        routes = {folder + "index.json": ok(fb("sec_13f_filing_index.json")),
                  folder + "56757.xml": ok(fb("sec_13f_infotable_2026q2.xml")),
                  sec.SUBMISSIONS.format(cik=cik): ok(fb("sec_submissions_berkshire.json"))}
        http, t, _ = client(routes)
        items = sec.filing_index(http, cik, acc)
        self.assertEqual(sec.info_table_name(items), "56757.xml")
        self.assertEqual(sec.info_table_name([{"name": "primary_doc.xml", "size": 5}]), None)
        picks, notes = sec.fund_periods(http, cik, 2)
        self.assertEqual((len(picks), notes), (2, []))
        table = sec.period_table(http, picks[0])
        self.assertEqual(len(table["holdings"]), 12)
        merged = sec.merge_tables([table, table])
        self.assertEqual(merged["holdings"]["02005N100"]["shares"], 54000000.0)
        self.assertEqual(merged["rows"], 90)


class FundsFlowTest(unittest.TestCase):
    """The funds flow, one ticker at a time: a shared table is read, each
    ticker's own cache gets only its own numbers."""
    PICKS = [{"cik": 1067983, "period": "2026-06-30", "accession": "0001193125-26-352200", "form": "13F-HR",
              "filed": "2026-08-14", "merge": []},
             {"cik": 1067983, "period": "2026-03-31", "accession": "0001193125-26-226661", "form": "13F-HR",
              "filed": "2026-05-15", "merge": []}]
    FUNDS = [{"name": "Berkshire Hathaway", "cik": 1067983}, {"name": "Example Fund", "cik": 1}]

    def build(self, ticker, overrides=None):
        m = ticker_map()
        ident = sec.identity(ticker, m, None)
        cache = {}
        for pick, table in zip(self.PICKS, (q2(), q1())):
            periods = cache.setdefault(str(pick["cik"]), {"periods": {}})["periods"]
            self.assertEqual(sec.periods_to_read(periods, [pick]), [pick])
            entry, cusip, note = sec.period_entry(table, ident, pick, overrides)
            periods[pick["period"]] = entry
            if cusip and cusip not in ident["cusips"]:
                ident["cusips"].append(cusip)
        return ident, cache

    def test_two_tickers_keep_separate_caches(self):
        ally_ident, ally = self.build("ALLY")
        cb_ident, cb = self.build("CB")
        self.assertEqual(ally_ident["cusips"], ["02005N100"])
        self.assertEqual(cb_ident["cusips"], ["H1467J104"])  # learned from the new name, used for the old one
        self.assertEqual(cb["1067983"]["periods"]["2026-03-31"]["shares"], 34249183.0)
        rows, checked = sec.ticker_fund_rows(self.FUNDS, ally, {1067983: self.PICKS})
        self.assertEqual(checked, 1)
        self.assertEqual([(r["fund"], r["change"], r["shares"]) for r in rows],
                         [("Berkshire Hathaway", "trimmed", 27000000.0)])
        rows, _ = sec.ticker_fund_rows(self.FUNDS, cb, {"1067983": self.PICKS})
        self.assertEqual([r["change"] for r in rows], ["unchanged"])
        self.assertNotIn("02005N100", json.dumps(cb))
        self.assertNotIn("H1467J104", json.dumps(ally))

    def test_not_held_and_stale_entries(self):
        ident, cache = self.build("AAPL")
        periods = cache["1067983"]["periods"]
        self.assertEqual(periods["2026-06-30"]["shares"], 0.0)
        self.assertEqual(sec.ticker_fund_rows(self.FUNDS, cache, {1067983: self.PICKS}), ([], 1))
        self.assertEqual(sec.periods_to_read(periods, self.PICKS), [])
        restated = dict(self.PICKS[0], accession="0001193125-26-400000", form="13F-HR/A")
        self.assertEqual(sec.periods_to_read(periods, [restated]), [restated])
        merged = dict(self.PICKS[0], merge=[{"cik": 1067983, "accession": "0001193125-26-400001"}])
        self.assertEqual(sec.periods_to_read(periods, [merged]), [merged])
        entry, _, _ = sec.period_entry(q2(), ident, merged)
        self.assertEqual(entry["merge"], ["0001193125-26-400001"])
        self.assertTrue(sec.entry_current(entry, merged))
        # A fund with a period missing from the cache is not counted as checked.
        del periods["2026-03-31"]
        self.assertEqual(sec.ticker_fund_rows(self.FUNDS, cache, {1067983: self.PICKS}), ([], 0))


# ================================================================ FINRA

class FinraTest(unittest.TestCase):
    def test_api_and_file_totals_match(self):
        rows = fj("finra_regsho_daily_aapl.json")
        random.Random(4).shuffle(rows)
        daily = finra.parse_regsho(rows)
        dates = [r["date"] for r in daily]
        self.assertEqual(dates, sorted(set(dates)))
        api = {r["date"]: r for r in daily}["2026-11-10"]
        cnms = finra.parse_cnms(ft("finra_cnms_shvol_20261110.txt"))["AAPL"]
        self.assertEqual(api, cnms)
        self.assertEqual((api["short_volume"], api["total_volume"], api["exempt_volume"]),
                         (5044508.820252, 9399064.247494, 27381.0))

    def test_cnms_file(self):
        raw = ft("finra_cnms_shvol_20261110.txt")
        self.assertIn("\r\n", raw)
        table = finra.parse_cnms(raw)
        self.assertEqual(len(table), 13)
        self.assertIn("BRK/B", table)
        self.assertIn("BF/B", table)
        self.assertEqual(finra.parse_cnms(""), {})
        with self.assertRaises(ValueError):
            finra.parse_cnms("a|b\n1|2")

    def test_query_shape_and_204(self):
        rows = fj("finra_regsho_daily_aapl.json")
        http, t, _ = client({("POST", finra.REGSHO): ok(rows), ("POST", finra.SI): ok(b"", status=204)})
        out = finra.short_volume(http, "AAPL", date(2025, 11, 12), date(2026, 11, 12))
        self.assertEqual(len(out), 34)
        body = json.loads(t.calls[0]["body"])
        self.assertEqual(body, {"limit": 5000, "compareFilters": [
            {"compareType": "equal", "fieldName": "securitiesInformationProcessorSymbolIdentifier",
             "fieldValue": "AAPL"}], "dateRangeFilters": [
            {"fieldName": "tradeReportDate", "startDate": "2025-11-12", "endDate": "2026-11-12"}]})
        self.assertEqual(finra.short_interest(http, "BRK/B", date(2025, 11, 12), date(2026, 11, 12)), [])
        body = json.loads(t.calls[1]["body"])
        self.assertEqual(body["compareFilters"][0]["fieldName"], "symbolCode")
        self.assertEqual(body["compareFilters"][0]["fieldValue"], "BRKB")
        self.assertEqual(body["dateRangeFilters"][0]["fieldName"], "settlementDate")
        self.assertEqual(finra.si_symbol("BRK-B"), "BRKB")

    def test_short_interest_sorted(self):
        rows = fj("finra_short_interest_aapl.json")
        shuffled = list(rows)
        random.Random(9).shuffle(shuffled)
        out = finra.parse_short_interest(shuffled)
        self.assertEqual(len(out), 24)
        self.assertEqual([r["settlement_date"] for r in out], sorted(r["settlementDate"] for r in rows))
        last = out[-1]
        self.assertEqual(last["settlement_date"], "2026-10-30")
        self.assertEqual(set(last), {"settlement_date", "short_interest", "previous", "change_pct",
                                     "avg_daily_volume", "days_to_cover"})
        first = out[0]
        self.assertEqual((first["short_interest"], first["previous"], first["change_pct"], first["avg_daily_volume"],
                          first["days_to_cover"]), (114913396.0, 115557836.0, -0.56, 47053455.0, 2.44))

    def test_file_missing_is_none(self):
        url = finra.CNMS.format(day="20261126")
        http, _, _ = client({url: ok(b"<Error><Code>AccessDenied</Code></Error>", status=403)})
        self.assertIsNone(finra.short_volume_file(http, date(2026, 11, 26)))

    def test_merge_by_date(self):
        old = [{"date": "2026-01-02", "v": 1}, {"date": "2026-01-05", "v": 1}]
        new = [{"date": "2026-01-05", "v": 2}, {"date": "2026-01-06", "v": 2}]
        self.assertEqual(finra.merge_by_date(old, new, 2), [{"date": "2026-01-05", "v": 2},
                                                           {"date": "2026-01-06", "v": 2}])
        si = finra.merge_by_date([{"settlement_date": "2026-10-15"}], [{"settlement_date": "2026-10-30"}], 10)
        self.assertEqual(len(si), 2)

    def test_refresh_uses_the_api_then_the_daily_files(self):
        cached = finra.parse_regsho(fj("finra_regsho_daily_aapl.json"))[:-3]
        last = cached[-1]["date"]
        http, t, _ = client({("POST", finra.REGSHO): ok(fj("finra_regsho_daily_aapl.json"))})
        rows, info = finra.refresh_short_volume(http, "AAPL", cached, date(2026, 11, 12))
        self.assertEqual(info, {"source": "api", "added": 3, "reason": None})
        self.assertEqual(json.loads(t.calls[0]["body"])["dateRangeFilters"][0]["startDate"],
                         (date.fromisoformat(last) - timedelta(days=5)).isoformat())
        self.assertEqual(len(rows), len(cached) + 3)

        file_url = finra.CNMS.format(day="20261110")
        routes = {("POST", finra.REGSHO): ok(b"", status=500), file_url: ok(fb("finra_cnms_shvol_20261110.txt"))}
        http, t, _ = client(routes)
        old = [r for r in cached if r["date"] < "2026-11-10"]
        rows, info = finra.refresh_short_volume(http, "AAPL", old, date(2026, 11, 12))
        self.assertEqual((info["source"], info["added"], info["reason"]), ("files", 1, "HTTP 500"))
        files = [c["url"] for c in t.calls if c["method"] == "GET"]
        self.assertLessEqual(len(files), 5)
        self.assertEqual(rows[-1]["date"], "2026-11-10")
        http, _, _ = client({("POST", finra.REGSHO): ok(b"", status=500)})
        rows, info = finra.refresh_short_volume(http, "AAPL", old, date(2026, 11, 12))
        self.assertEqual((info["source"], info["added"]), ("cache", 0))
        self.assertEqual(rows, old)

    def test_refresh_short_interest(self):
        rows = fj("finra_short_interest_aapl.json")
        http, _, _ = client({("POST", finra.SI): ok(rows)})
        out, info = finra.refresh_short_interest(http, "AAPL", [], date(2026, 11, 12))
        self.assertEqual((len(out), info["source"], info["added"]), (24, "api", 24))
        http, _, _ = client({("POST", finra.SI): ConnectionResetError()})
        out2, info = finra.refresh_short_interest(http, "AAPL", out, date(2026, 11, 12))
        self.assertEqual((out2, info["source"], info["reason"]), (out, "cache", "connection error"))

    def test_volume_cache_round_trip(self):
        rows = finra.parse_regsho(fj("finra_regsho_daily_aapl.json"))
        cache = finra.volume_cache(rows)
        self.assertEqual(cache["fields"], ["date", "short_volume", "exempt_volume", "total_volume"])
        self.assertEqual(cache["last_date"], rows[-1]["date"])
        self.assertEqual(finra.volume_rows(json.loads(json.dumps(cache))), rows)
        self.assertEqual(finra.volume_rows({"fields": ["x"], "rows": []}), [])


# ================================================================ FRED

def fred_data():
    out = {}
    for name in os.listdir(FIXTURES):
        if name.startswith("fred_") and name.endswith(".csv"):
            sid = name[5:-4].upper()
            out[sid] = fred.parse_csv(ft(name), sid)
    return out


MACRO_ROWS = [
    {"id": "fed_funds", "label": "Fed funds target range", "series": ["DFEDTARL", "DFEDTARU"], "transform": "range",
     "unit": "percent"},
    {"id": "dgs10", "label": "10 year Treasury yield", "series": ["DGS10"], "transform": "level", "unit": "percent"},
    {"id": "cpi_yoy", "label": "CPI, year over year", "series": ["CPIAUCSL"], "transform": "yoy", "unit": "percent"},
    {"id": "payrolls", "label": "Payrolls, change", "series": ["PAYEMS"], "transform": "diff", "unit": "thousands"},
    {"id": "gdp", "label": "Real GDP growth, annualized", "series": ["A191RL1Q225SBEA"], "transform": "level",
     "unit": "percent"},
    {"id": "claims", "label": "Initial jobless claims", "series": ["ICSA"], "transform": "thousands",
     "unit": "thousands"},
]


class FredTest(unittest.TestCase):
    def test_parse_csv_drops_missing_values(self):
        cpi = fred.parse_csv(ft("fred_cpiaucsl.csv"), "CPIAUCSL")
        self.assertNotIn("2025-10-01", dict(cpi))
        self.assertEqual(cpi[-1], ("2026-10-01", 337.476))
        dgs10 = dict(fred.parse_csv(ft("fred_dgs10.csv"), "DGS10"))
        for holiday in ("2025-06-19", "2025-07-04", "2025-09-01", "2025-10-13"):
            self.assertNotIn(holiday, dgs10)
        old = fred.parse_csv("DATE,X1\n2020-01-01,.\n2020-02-01,1.5\n", "X1")
        self.assertEqual(old, [("2020-02-01", 1.5)])
        with self.assertRaises(ValueError):
            fred.parse_csv(ft("fred_cpiaucsl.csv"), "DGS10")
        with self.assertRaises(ValueError):
            fred.parse_csv("", "DGS10")

    def test_series_request_and_fetch_many(self):
        url = fred.FRED_CSV.format(series="DGS2", start="2026-01-01")
        bad = fred.FRED_CSV.format(series="DGS10", start="2026-01-01")
        http, t, _ = client({url: ok(fb("fred_dgs2.csv")), bad: ok(b"", status=500)})
        data, errors = fred.fetch_many(http, ["DGS2", "dgs10", "DGS2", ""], date(2026, 1, 1))
        self.assertEqual(list(data), ["DGS2"])
        self.assertEqual(errors, {"DGS10": "HTTP 500"})
        self.assertEqual(t.calls[0]["url"], url)
        with self.assertRaises(ValueError):
            fred.series(http, "BAD ID", date(2026, 1, 1))
        http, t, c = client({url: ok(fb("fred_dgs2.csv"))}, budget=1)
        c.t += 2
        data, errors = fred.fetch_many(http, ["DGS2", "DGS10"], {"DGS2": date(2026, 1, 1), "DGS10": date(2026, 1, 1)})
        self.assertEqual(errors, {"DGS2": "time budget used up", "DGS10": "time budget used up"})

    def test_macro_table(self):
        rows = {r["id"]: r for r in fred.macro_table(fred_data(), MACRO_ROWS)}
        self.assertEqual(list(rows), [r["id"] for r in MACRO_ROWS])
        ff = rows["fed_funds"]
        self.assertEqual((ff["value"], ff["display"], ff["prior"], ff["prior_as_of"], ff["change"], ff["as_of"]),
                         (4.0, "3.75 to 4.00", 3.75, "2026-09-16", 0.25, "2026-11-12"))
        self.assertEqual(ff["series"], ["DFEDTARL", "DFEDTARU"])
        d10 = rows["dgs10"]
        self.assertEqual((d10["value"], d10["prior"], d10["change"], d10["as_of"], d10["frequency"]),
                         (4.98, 4.88, 0.1, "2026-11-10", "daily"))
        cpi = rows["cpi_yoy"]  # October 2025 has no value, so the latest year over year is September
        self.assertEqual((cpi["as_of"], cpi["value"], cpi["frequency"], cpi["status"]),
                         ("2026-09-01", 3.72, "monthly", "ok"))
        pay = rows["payrolls"]
        self.assertEqual((pay["value"], pay["prior"], pay["as_of"]), (165.0, 60.0, "2026-10-01"))
        self.assertEqual((rows["gdp"]["value"], rows["gdp"]["frequency"]), (3.4, "quarterly"))
        self.assertEqual(rows["claims"]["status"], "not_available")
        self.assertIsNone(rows["claims"]["value"])
        self.assertEqual(set(ff), {"id", "label", "unit", "frequency", "value", "prior", "change", "as_of",
                                   "prior_as_of", "display", "series", "status"})
        claims = fred.macro_table({"ICSA": [("2026-10-31", 191000.0), ("2026-11-07", 200000.0)]}, MACRO_ROWS[-1:])
        self.assertEqual((claims[0]["value"], claims[0]["prior"], claims[0]["frequency"]), (200.0, 191.0, "weekly"))

    def test_run_starts(self):
        starts = fred.run_starts(["DGS10", "CPIAUCSL", "vixcls"], date(2026, 11, 12))
        self.assertEqual(starts, {"DGS10": date(2020, 11, 12), "CPIAUCSL": date(2023, 11, 12),
                                  "VIXCLS": date(2020, 11, 12)})
        self.assertEqual(fred.run_starts(["UNRATE"], date(2028, 2, 29))["UNRATE"], date(2025, 2, 28))

    def test_yoy_and_diff(self):
        pts = [("2025-01-01", 100.0), ("2025-02-01", 101.0), ("2026-01-01", 103.0), ("2026-02-01", 102.01)]
        self.assertEqual([(d, round(v, 4)) for d, v in fred.yoy(pts)], [("2026-01-01", 3.0), ("2026-02-01", 1.0)])
        self.assertEqual(fred.diff([("a", 1.0), ("b", 3.5)]), [("b", 2.5)])

    def test_fx(self):
        cfg = {"EUR": {"series": "DEXUSEU", "quote": "usd_per_unit"}, "JPY": {"series": "DEXJPUS",
                                                                          "quote": "units_per_usd"}}
        data = {"DEXUSEU": [("2026-11-06", 1.1154)], "DEXJPUS": [("2026-11-06", 158.23)]}
        self.assertEqual(fred.fx_usd_per_unit(data, "eur", cfg), (1.1154, "DEXUSEU", "2026-11-06"))
        self.assertEqual(fred.fx_usd_per_unit(data, "JPY", cfg), (round(1 / 158.23, 6), "DEXJPUS", "2026-11-06"))
        self.assertIsNone(fred.fx_usd_per_unit(data, "SEK", cfg))
        self.assertIsNone(fred.fx_usd_per_unit({}, "EUR", cfg))
        self.assertEqual(fred.fx_usd_per_unit(data, "USD", cfg), (1.0, "", ""))


# ================================================================ calendar

class CalendarTest(unittest.TestCase):
    def test_fomc(self):
        events = cal.parse_fomc(ft("fomc_calendars.html"))
        by = {e["date"]: e for e in events}
        years = {}
        for e in events:
            years[e["date"][:4]] = years.get(e["date"][:4], 0) + 1
        self.assertEqual(years, {"2023": 8, "2025": 8, "2026": 8, "2027": 8})
        dec = by["2026-12-09"]
        self.assertEqual(dec, {"date": "2026-12-09", "time": "14:00", "tz": "America/New_York", "kind": "fomc",
                               "title": "FOMC meeting, statement and press conference",
                               "flags": {"start_date": "2026-12-08", "press_conference": True,
                                         "press_conference_confirmed": False, "projections": True,
                                         "tentative": False},
                               "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"})
        self.assertEqual(by["2026-10-28"]["flags"]["projections"], False)
        self.assertTrue(by["2026-10-28"]["flags"]["press_conference_confirmed"])
        self.assertTrue(by["2026-09-16"]["flags"]["projections"])
        jan27 = by["2027-01-27"]["flags"]
        self.assertEqual((jan27["projections"], jan27["tentative"]), (None, True))
        self.assertIn("2027-10-27", by)  # the row with a clean class attribute
        self.assertEqual(by["2023-02-01"]["flags"]["start_date"], "2023-01-31")  # Jan/Feb, 31-1
        self.assertEqual(by["2023-11-01"]["flags"]["start_date"], "2023-10-31")  # Oct/Nov
        self.assertNotIn("2025-08-22", by)  # a notation vote
        self.assertEqual([e["date"] for e in events], sorted(e["date"] for e in events))

    def test_ics_unfolding(self):
        text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Personal Income and Outlays\\, Oct\r\n ober 2026\r\n"
                "DTSTART;VALUE=DATE-TIME:20261125T133000Z\r\nUID:a\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\n"
                "SUMMARY:All day\r\nDTSTART;VALUE=DATE:20261126\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        events = cal.parse_ics(text)
        self.assertEqual(events[0]["summary"], "Personal Income and Outlays, October 2026")
        self.assertEqual(events[0]["start"], datetime(2026, 11, 25, 13, 30, tzinfo=timezone.utc))
        self.assertTrue(events[1]["all_day"])
        self.assertEqual(cal.bea_events(text)[0]["time"], "08:30")

    def test_bls(self):
        events = cal.bls_events(ft("bls_schedule.ics"))
        kinds = {}
        for e in events:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        self.assertEqual(kinds, {"cpi": 4, "jobs": 4, "ppi": 4, "jolts": 4})
        by = {(e["kind"], e["date"]): e for e in events}
        self.assertEqual(by[("cpi", "2026-12-10")]["time"], "08:30")
        self.assertEqual(by[("cpi", "2026-12-10")]["title"], "CPI release")
        self.assertEqual(by[("jolts", "2026-12-01")]["time"], "10:00")
        self.assertEqual(by[("jobs", "2026-12-04")]["title"], "Jobs report")
        self.assertEqual(by[("ppi", "2026-11-13")]["flags"], {})
        self.assertTrue(all(e["source"].startswith("https://www.bls.gov/") for e in events))

    def test_bea(self):
        events = cal.bea_events(ft("bea_schedule.ics"))
        self.assertEqual(len(events), 12)
        by = {(e["kind"], e["date"]): e for e in events}
        self.assertEqual(by[("gdp", "2026-10-29")]["time"], "08:30")  # 12:30 UTC in daylight time
        self.assertEqual(by[("gdp", "2026-11-25")]["time"], "08:30")  # 13:30 UTC in standard time
        self.assertEqual(by[("gdp", "2026-12-23")]["title"],
                         "GDP (Third Estimate), Industries, Corporate Profits, State GDP, and State Personal Income, "
                         "3rd Quarter 2026")
        self.assertEqual(by[("pce", "2026-11-25")]["title"], "Personal Income and Outlays, October 2026")
        self.assertFalse(any("County" in e["title"] or "Trade" in e["title"] for e in events))

    def test_release_dates_and_upcoming(self):
        dates = cal.release_dates(ft("bls_cpi_archive.html"), "cpi")
        self.assertEqual(len(dates), 40)
        self.assertEqual(dates, sorted(dates))
        self.assertIn("2026-11-10", dates)
        with self.assertRaises(ValueError):
            cal.release_dates("", "../x")
        events = cal.bls_events(ft("bls_schedule.ics"))
        up = cal.upcoming(events, date(2026, 11, 12), 30)
        self.assertEqual([e["date"] for e in up], ["2026-11-13", "2026-12-01", "2026-12-04", "2026-12-10"])

    def test_fetch_calendar(self):
        routes = {cal.FOMC_URL: ok(fb("fomc_calendars.html")), cal.BLS_ICS: ok(fb("bls_schedule.ics")),
                  cal.BEA_ICS: ok(b"", status=503), cal.BLS_ARCHIVE.format(release="cpi"): ok(fb("bls_cpi_archive.html"))}
        http, _, _ = client(routes)
        out = cal.fetch_calendar(http, date(2026, 11, 12))
        self.assertEqual(out["errors"], {"bea": "HTTP 503"})
        self.assertTrue(all("2026-10-12" <= e["date"] <= "2027-12-17" for e in out["events"]))
        self.assertIn("2026-12-09", [e["date"] for e in out["events"] if e["kind"] == "fomc"])
        self.assertEqual(out["history"]["fomc"][0], "2023-02-01")
        self.assertEqual(out["history"]["fomc"][-1], "2026-10-28")
        self.assertTrue(all(d < "2026-11-12" for d in out["history"]["cpi"]))
        self.assertIn("2026-11-10", out["history"]["cpi"])


# ================================================================ news

class NewsTest(unittest.TestCase):
    def test_fed_rss_with_bom(self):
        raw = fb("fed_press_monetary.xml")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        items = news.parse_fed_rss(raw)
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual((first["kind"], first["source"], first["published"]),
                         ("fed", "Federal Reserve", "2026-11-12T17:45:00+00:00"))
        self.assertEqual(first["url"], "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261112a.htm")
        self.assertEqual(first["summary"], "")  # the description only repeats the title
        self.assertIn("Board's", items[2]["title"])

    def test_bls_atom(self):
        items = news.parse_bls_atom(fb("bls_cpi_feed.atom"), "cpi")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["published"], "2026-11-10T12:55:47+00:00")
        self.assertEqual(items[0]["url"], "https://www.bls.gov/news.release/archives/cpi_11102026.htm")
        self.assertLessEqual(max(len(i["summary"]) for i in items), 300)
        with self.assertRaises(ValueError):
            news.parse_bls_atom(fb("bls_cpi_feed.atom"), "other")

    def test_bea_table(self):
        items = news.parse_bea_releases(ft("bea_current_releases.html"))
        self.assertEqual(len(items), 10)
        self.assertEqual(items[0]["url"],
                         "https://www.bea.gov/news/2026/us-international-trade-goods-and-services-september-2026")
        self.assertEqual(items[0]["published"], "2026-11-04T13:30:00+00:00")
        html = '<table><tr><td><a href="/news/x">Old release</a></td><td>March 3, 2026</td></tr></table>'
        self.assertEqual(news.parse_bea_releases(html)[0]["published"], "2026-03-03T13:30:00+00:00")

    def test_links_must_be_https_on_the_source_host(self):
        rss = ("<rss><channel><item><title>A</title><link>http://www.federalreserve.gov/a.htm</link>"
               "<pubDate>Thu, 12 Nov 2026 17:45:00 GMT</pubDate></item><item><title>B</title>"
               "<link>https://evil.example.com/b</link><pubDate>Thu, 12 Nov 2026 17:45:00 GMT</pubDate></item>"
               "</channel></rss>").encode()
        self.assertEqual(news.parse_fed_rss(rss), [])

    def test_digest_items_and_collect(self):
        now = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)
        doc = {"items": [
            {"title": "Fed holds — again", "url": "https://example.com/a", "source": "Example",
             "published": "2026-11-12T10:00:00+00:00", "themes": ["Macro/Fed"], "summary": "<b>Rates</b> held."},
            {"title": "Old", "url": "https://example.com/b", "published": "2026-10-01T10:00:00+00:00",
             "themes": ["Macro/Fed"]},
            {"title": "Other theme", "url": "https://example.com/c", "published": "2026-11-12T10:00:00+00:00",
             "themes": ["AI/ML"]},
            {"title": "Plain http", "url": "http://example.com/d", "published": "2026-11-12T10:00:00+00:00",
             "themes": ["Macro/Fed"]}]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "items.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            items = news.digest_items(path, now)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Fed holds, again")
            self.assertEqual(items[0]["summary"], "Rates held.")
            self.assertEqual(items[0]["kind"], "digest")
            routes = {news.FED_PRESS: ok(fb("fed_press_monetary.xml")),
                      news.BLS_FEEDS["cpi"]: ok(fb("bls_cpi_feed.atom")),
                      news.BEA_RELEASES: ok(fb("bea_current_releases.html"))}
            http, _, _ = client(routes)
            out, errors = news.collect(http, path, now, days=14, limit=4)
            self.assertEqual(set(errors), {"bls_jobs", "bls_ppi", "bls_jolts"})
            self.assertEqual(len(out), 4)
            stamps = [i["published"] for i in out]
            self.assertEqual(stamps, sorted(stamps, reverse=True))
            self.assertEqual(len({i["url"] for i in out}), 4)
            self.assertEqual(out[0]["url"], news.parse_fed_rss(fb("fed_press_monetary.xml"))[0]["url"])
        self.assertEqual(news.digest_items("/no/such/file.json", now), [])


# ================================================================ congress

def house_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.txt", "x")
        z.writestr("2026FD.xml", fb("house_2026FD.xml"))
    return buf.getvalue()


HERN = {"member": "Hon. Kevin Hern", "district": "OK01", "filed": "2026-09-25", "doc_id": "20035491", "year": 2026}
PETERS = {"member": "Hon. Scott H. Peters", "district": "CA50", "filed": "2026-09-21", "doc_id": "20035434",
          "year": 2026}


class CongressTest(unittest.TestCase):
    def test_house_index(self):
        idx = congress.house_index(house_zip())
        self.assertEqual(len(idx), 17)
        self.assertEqual(idx[0], HERN)
        self.assertEqual(idx[1]["member"], "Hon. Thomas H. Kean Jr")
        # An empty FilingDate is tolerated (the fixture has one on a W filing, here one on a P filing too).
        xml = fb("house_2026FD.xml").replace(b"<FilingDate>9/25/2026</FilingDate>", b"<FilingDate />")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("2026FD.xml", xml)
        idx = congress.house_index(buf.getvalue())
        self.assertEqual(len(idx), 17)
        self.assertIsNone(idx[0]["filed"])
        with self.assertRaises(ValueError):
            congress.house_index(b"not a zip")

    def test_ptr_with_tickers(self):
        trades = congress.parse_ptr(ft("house_ptr_20035491.txt"), HERN)
        self.assertEqual(len(trades), 99)
        self.assertEqual(sum(1 for t in trades if t["ticker"]), 86)
        self.assertEqual(trades[0], {
            "member": "Hon. Kevin Hern", "chamber": "House", "district": "OK01", "ticker": "AAPL",
            "asset": "Apple Inc. - Common Stock", "asset_type": "ST", "type": "partial sale", "owner": "dependent",
            "date": "2026-09-08", "notified": "2026-09-15", "filed": "2026-09-25", "amount_range": "$1,001 - $15,000",
            "source": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf"})
        cut = [t for t in trades if t["amount_range"] == "$100,001 -"]
        self.assertEqual([t["date"] for t in cut], ["2026-09-02"])
        self.assertTrue(all(t["type"] in congress.TRADE_TYPES and t["owner"] in congress.TRADE_OWNERS
                            for t in trades))
        selfs = [t for t in trades if t["owner"] == "self"]
        self.assertTrue(selfs)
        self.assertTrue(all(t["asset"] and not t["asset"].startswith(("S O", "F S")) for t in selfs))

    def test_ptr_without_tickers(self):
        trades = congress.parse_ptr(ft("house_ptr_20035434.txt"), PETERS)
        self.assertEqual(len(trades), 17)
        self.assertEqual(sum(1 for t in trades if t["ticker"]), 0)
        self.assertTrue(all(t["owner"] == "spouse" for t in trades))
        self.assertEqual(trades[0]["asset"], "Energy Northwest WA PWR UTIL")
        self.assertEqual(trades[0]["amount_range"], "$500,001 - $1,000,000")

    def test_ptr_edge_cases(self):
        self.assertEqual(congress.parse_ptr("", HERN), [])
        self.assertEqual(congress.parse_ptr("short text", HERN), [])
        text = ("Name: Hon. Example Member Status: Member State/District:ZZ01\n"
                "ID Owner Asset Transaction\nType\nDate Notification\nDate\nAmount Cap.\nGains >\n$200?\n"
                "SP Example Fund LP [OT] P 07/29/202607/31/2026Spouse/DC Over $1,000,000\n"
                "F      S     : New\nS          O : Example Trust\n"
                "Example Corp Class B (EXM.B) [ST] E 08/01/202608/02/2026$1,001 - $15,000\n"
                "* For the complete list of asset type abbreviations")
        trades = congress.parse_ptr(text, {"doc_id": "20099999", "year": 2026, "filed": "2026-08-03"})
        self.assertEqual(len(trades), 2)
        self.assertEqual((trades[0]["amount_range"], trades[0]["owner"], trades[0]["asset"]),
                         ("Spouse/DC Over $1,000,000", "spouse", "Example Fund LP"))
        self.assertEqual((trades[1]["ticker"], trades[1]["type"], trades[1]["owner"], trades[1]["asset"]),
                         ("EXM-B", "exchange", "self", "Example Corp Class B"))
        self.assertEqual((trades[1]["member"], trades[1]["district"]), ("Hon. Example Member", "ZZ01"))

    @unittest.skipUnless(HAVE_PYPDF, "pypdf is not installed")
    def test_ptr_pdfs(self):
        text = congress.ptr_text(fb("house_ptr_20035591.pdf"))
        trades = congress.parse_ptr(text, {"member": "Hon. Thomas H. Kean Jr", "district": "NJ07",
                                           "filed": "2026-10-30", "doc_id": "20035591", "year": 2026})
        self.assertEqual([t["ticker"] for t in trades], ["ABT", "ESAB", "JNJ", "MDLZ", "SYK", "TXN"])
        self.assertEqual(congress.ptr_text(fb("house_ptr_9116342.pdf")).strip(), "")
        with self.assertRaises(ValueError):
            congress.ptr_text(b"not a pdf")

    def _house_http(self, extra=None):
        idx = congress.house_index(house_zip())
        routes = {congress.HOUSE_ZIP.format(year=2026): ok(house_zip()),
                  congress.HOUSE_ZIP.format(year=2025): ok(b"", status=404)}
        texts = {"20035491": ft("house_ptr_20035491.txt"), "20035434": ft("house_ptr_20035434.txt")}
        for f in idx:
            routes[congress.HOUSE_PTR.format(year=f["year"], doc_id=f["doc_id"])] = ok(
                texts.get(f["doc_id"], "").encode("utf-8"))
        routes.update(extra or {})
        return client(routes), texts

    def test_fetch_house(self):
        (http, t, _), _ = self._house_http({congress.HOUSE_PTR.format(year=2026, doc_id="20035591"):
                                            ok(b"", status=500)})
        bundle = congress.fetch_house(http, date(2026, 11, 12), 365, 300, text_of=lambda b: b.decode("utf-8"),
                                      workers=2)
        idx = congress.house_index(house_zip())
        in_window = [f for f in idx if f["filed"] and f["filed"] >= "2025-11-12"]
        self.assertEqual(bundle["filings"], len(in_window))
        self.assertEqual((bundle["electronic"], bundle["failed"], bundle["skipped_for_time"]), (2, 1, 0))
        self.assertEqual(bundle["scanned"], len(in_window) - 3)
        self.assertEqual(len(bundle["trades"]), 99 + 17)
        dates = [x["date"] for x in bundle["trades"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(congress.check_bundle(json.loads(json.dumps(bundle)))["trades"], bundle["trades"])

    def test_fetch_house_stops_at_the_time_limit(self):
        (http, t, _), _ = self._house_http()
        ticks = iter(range(0, 10_000, 100))
        bundle = congress.fetch_house(http, date(2026, 11, 12), 365, 250, clock=lambda: next(ticks),
                                      text_of=lambda b: b.decode("utf-8"), workers=1)
        self.assertGreater(bundle["skipped_for_time"], 0)
        self.assertEqual(bundle["filings"], bundle["electronic"] + bundle["scanned"] + bundle["failed"]
                         + bundle["skipped_for_time"])

    def test_check_bundle(self):
        trades = congress.parse_ptr(ft("house_ptr_20035491.txt"), HERN)
        good = {"kind": "stocks.congress", "version": 1, "generated_at": "2026-11-12T13:00:00+00:00",
                "window_days": 365, "filings": 1, "electronic": 1, "scanned": 0, "failed": 0,
                "skipped_for_time": 0, "trades": trades}
        self.assertEqual(len(congress.check_bundle(good)["trades"]), 99)
        extra = dict(good, unknown=1)
        self.assertNotIn("unknown", congress.check_bundle(extra))

        def broken(**change):
            t = dict(trades[0], **change)
            return dict(good, trades=[t])
        for doc in (dict(good, kind="x"), dict(good, version=2), dict(good, filings=-1), dict(good, filings="1"),
                    dict(good, trades="x"), broken(ticker="not a ticker"), broken(source="https://evil.example/x.pdf"),
                    broken(source="http://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/1.pdf"),
                    broken(type="gift"), broken(owner="friend"), broken(date="2026/01/01"), broken(extra="x"),
                    broken(chamber="Senate"), broken(member=["x"]), broken(district="California"),
                    dict(good, generated_at="yesterday"), "not a dict"):
            with self.assertRaises(ValueError):
                congress.check_bundle(doc)
        with self.assertRaises(ValueError):
            congress.check_bundle(dict(good, trades=[trades[0]] * (congress.MAX_TRADES + 1)))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "house.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(good, f)
            self.assertEqual(len(congress.load_bundle(path)["trades"]), 99)
            with self.assertRaises(ValueError):
                congress.load_bundle(os.path.join(d, "missing.json"))

    def test_for_ticker(self):
        trades = congress.parse_ptr(ft("house_ptr_20035491.txt"), HERN)
        aapl = congress.for_ticker(trades, "aapl", date(2026, 1, 1))
        self.assertEqual(len(aapl), 2)
        self.assertEqual(congress.for_ticker(trades, "AAPL", date(2026, 10, 1)), [])

    def test_senate(self):
        trades = congress.parse_fmp(fj("fmp_senate_trades_unverified.json"))
        self.assertEqual([(t["type"], t["owner"], t["date"], t["filed"], t["chamber"]) for t in trades],
                         [("purchase", "spouse", "2026-10-02", "2026-10-20", "Senate"),
                          ("partial sale", "self", "2026-07-29", "2026-08-14", "Senate")])
        self.assertEqual(trades[0]["ticker"], "EXMPL")
        self.assertEqual(congress.senate_trades(None, "EXMPL", ""), ([], "off"))
        url = congress.FMP_SENATE.format(symbol="EXMPL") + "&apikey=SECRETKEY"
        for status, coverage in ((402, "not available on this plan"), (401, "not available on this plan"),
                                 (500, "error")):
            http, t, _ = client({url: ok(b"{}", status=status)})
            self.assertEqual(congress.senate_trades(http, "EXMPL", "SECRETKEY"), ([], coverage))
        http, t, _ = client({url: ok(fb("fmp_senate_trades_unverified.json"))})
        out, coverage = congress.senate_trades(http, "EXMPL", "SECRETKEY")
        self.assertEqual((len(out), coverage), (2, "ok"))
        self.assertEqual(t.calls[0]["url"], url)
        http, t, _ = client({url: ok({"Error Message": "Invalid API KEY SECRETKEY"})})
        with self.assertRaises(FetchError) as e:
            congress.senate_fmp(http, "EXMPL", "SECRETKEY")
        self.assertNotIn("SECRETKEY", e.exception.reason)

    @unittest.skipUnless(HAVE_PYPDF, "pypdf is not installed")
    def test_cli_logs_counts_only(self):
        (http, t, _), _ = self._house_http()
        orig = congress.ptr_text
        congress.ptr_text = lambda b: b.decode("utf-8")
        try:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "sub", "house.json")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = congress.main(["house", "--out", out, "--days", "365", "--max-seconds", "300"], http=http,
                                       today=date(2026, 11, 12))
                self.assertEqual(rc, 0)
                with open(out, encoding="utf-8") as f:
                    bundle = congress.check_bundle(json.load(f))
                self.assertEqual(len(bundle["trades"]), 116)
                lines = buf.getvalue().splitlines()
                self.assertEqual(lines, [f"Read 2 electronic reports, {bundle['scanned']} scanned reports skipped, "
                                         "116 trades."])
                self.assertNotIn("Hern", buf.getvalue())
        finally:
            congress.ptr_text = orig


# ================================================================ trading days

class TradingDaysTest(unittest.TestCase):
    EXPECTED = {
        2021: ["01-01", "01-18", "02-15", "04-02", "05-31", "07-05", "09-06", "11-25", "12-24"],
        2022: ["01-17", "02-21", "04-15", "05-30", "06-20", "07-04", "09-05", "11-24", "12-26"],
        2025: ["01-01", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"],
        2026: ["01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"],
        2027: ["01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"],
        2028: ["01-17", "02-21", "04-14", "05-29", "06-19", "07-04", "09-04", "11-23", "12-25"],
    }

    def test_holidays(self):
        for year, days in self.EXPECTED.items():
            got = sorted(d.strftime("%m-%d") for d in trading_days.nyse_holidays(year))
            self.assertEqual(got, days, year)
        # A Saturday New Year's Day closes nothing the year before.
        self.assertTrue(trading_days.is_trading_day(date(2021, 12, 31)))
        self.assertTrue(trading_days.is_trading_day(date(2027, 12, 31)))
        self.assertEqual(trading_days.nyse_holidays(2026)[date(2026, 11, 26)], "Thanksgiving")
        self.assertEqual(trading_days.easter(2027), date(2027, 3, 28))

    def test_special_closure_and_names(self):
        self.assertFalse(trading_days.is_trading_day(date(2025, 1, 9)))
        self.assertEqual(trading_days.closure_name(date(2025, 1, 9)), "National day of mourning")
        self.assertEqual(trading_days.closure_name(date(2026, 11, 14)), "Saturday")
        self.assertEqual(trading_days.closure_name(date(2026, 11, 15)), "Sunday")
        self.assertIsNone(trading_days.closure_name(date(2026, 11, 13)))
        self.assertEqual(trading_days.closure_name(date(2026, 7, 3)), "Independence Day")
        self.assertFalse(trading_days.is_trading_day(date(2026, 11, 13), (date(2026, 11, 13),)))
        self.assertTrue(trading_days.is_trading_day(date(2026, 11, 27)))  # an early close is a trading day

    def test_last_closed_day(self):
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 12, 17, 59)), date(2026, 11, 11))
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 12, 18, 0)), date(2026, 11, 12))
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 12, 5, 45)), date(2026, 11, 11))
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 26, 20, 0)), date(2026, 11, 25))
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 30, 8, 0)), date(2026, 11, 27))
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 14, 12, 0)), date(2026, 11, 13))
        self.assertEqual(trading_days.last_closed_day(ny(2026, 11, 12, 16, 30), "16:15"), date(2026, 11, 12))
        utc = datetime(2026, 11, 12, 23, 30, tzinfo=timezone.utc)  # 18:30 in New York
        self.assertEqual(trading_days.last_closed_day(utc), date(2026, 11, 12))
        self.assertEqual(trading_days.last_closed_day(datetime(2026, 11, 12, 23, 30)), date(2026, 11, 12))

    def test_stepping(self):
        self.assertEqual(trading_days.previous_trading_day(date(2026, 11, 27)), date(2026, 11, 25))
        self.assertEqual(trading_days.next_trading_day(date(2026, 11, 25)), date(2026, 11, 27))
        self.assertEqual(trading_days.next_trading_day(date(2026, 12, 24)), date(2026, 12, 28))
        self.assertEqual(trading_days.add_trading_days(date(2026, 11, 12), 5), date(2026, 11, 19))
        self.assertEqual(trading_days.add_trading_days(date(2026, 11, 12), 20), date(2026, 12, 11))
        self.assertEqual(trading_days.add_trading_days(date(2026, 11, 19), -5), date(2026, 11, 12))
        self.assertEqual(trading_days.add_trading_days(date(2026, 11, 14), 0), date(2026, 11, 14))
        self.assertEqual(trading_days.trading_days_between(date(2026, 11, 12), date(2026, 11, 19)), 5)
        self.assertEqual(trading_days.trading_days_between(date(2026, 11, 19), date(2026, 11, 12)), -5)
        self.assertEqual(trading_days.trading_days_between(date(2026, 11, 25), date(2026, 11, 30)), 2)
        self.assertEqual(trading_days.trading_days_between(date(2026, 1, 1), date(2026, 12, 31)), 251)

    def test_gate(self):
        self.assertEqual(trading_days.gate(date(2026, 11, 26), "scheduled"),
                         (False, "2026-11-26 is an NYSE holiday (Thanksgiving). Nothing to do."))
        self.assertEqual(trading_days.gate(date(2026, 11, 14), "scheduled"),
                         (False, "2026-11-14 is a Saturday. Nothing to do."))
        self.assertEqual(trading_days.gate(date(2025, 1, 9), "scheduled")[0], False)
        self.assertIn("National day of mourning", trading_days.gate(date(2025, 1, 9), "scheduled")[1])
        self.assertTrue(trading_days.gate(date(2026, 11, 12), "scheduled")[0])
        self.assertTrue(trading_days.gate(date(2026, 11, 26), "manual")[0])
        with self.assertRaises(ValueError):
            trading_days.gate(date(2026, 11, 26), "other")

    def test_gate_cli(self):
        script = os.path.join(DATA_DIR, "trading_days.py")
        for args, out, err in (
                (["--reason", "scheduled", "--date", "2026-11-26"], "run=false\n",
                 "2026-11-26 is an NYSE holiday (Thanksgiving). Nothing to do.\n"),
                (["--reason", "scheduled", "--date", "2026-11-12"], "run=true\n",
                 "2026-11-12 is an NYSE trading day. Going ahead.\n"),
                (["--reason", "manual", "--date", "2026-11-28"], "run=true\n",
                 "Manual run on 2026-11-28: going ahead.\n")):
            p = subprocess.run([sys.executable, "-I", script, "gate"] + args, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, out, err), args)
        p = subprocess.run([sys.executable, "-I", script, "gate", "--reason", "scheduled"], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertIn(p.stdout, ("run=true\n", "run=false\n"))
        p = subprocess.run([sys.executable, "-I", script, "gate", "--reason", "nightly"], capture_output=True,
                           text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_offset_fallback_matches_the_tz_database(self):
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
        for s in ("2026-03-08T06:59:00", "2026-03-08T07:00:00", "2026-11-01T05:59:00", "2026-11-01T06:00:00",
                  "2027-07-01T12:00:00", "2028-01-15T12:00:00"):
            utc = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            self.assertEqual(trading_days._us_eastern_offset(utc), utc.astimezone(tz).utcoffset(), s)

    def test_standard_library_only(self):
        import ast

        with open(os.path.join(DATA_DIR, "trading_days.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertLessEqual(names, {"argparse", "datetime", "os", "sys", "zoneinfo"})


# ================================================================ conventions

class ConventionsTest(unittest.TestCase):
    def test_library_code_never_prints(self):
        import ast

        for name in sorted(os.listdir(DATA_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef):
                    continue
                prints = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print"]
                if prints:
                    self.assertEqual(fn.name, "main", f"{name}: {fn.name} prints")

    def test_parsers_are_silent(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            sec.eps_summary(fj("sec_companyfacts_aapl.json"))
            sec.parse_info_table(fb("sec_13f_infotable_2026q2.xml"))
            cal.parse_fomc(ft("fomc_calendars.html"))
            congress.parse_ptr(ft("house_ptr_20035491.txt"), HERN)
            news.parse_fed_rss(fb("fed_press_monetary.xml"))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_copy_uses_plain_punctuation(self):
        for name in sorted(os.listdir(DATA_DIR)):
            if name.endswith(".py"):
                with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
                    text = f.read()
                self.assertNotIn("—", text, name)
                self.assertNotIn("–", text, name)

    def test_only_the_allowed_third_party_import(self):
        import ast

        allowed_third_party = {"pypdf"}
        stdlib = set(sys.stdlib_module_names)
        for name in sorted(os.listdir(DATA_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [(node.module or "").split(".")[0]]
                for m in mods:
                    self.assertTrue(m in stdlib or m in ("stocks",) or m in allowed_third_party, f"{name}: {m}")
                    if m == "pypdf":
                        self.assertEqual(name, "congress.py")


if __name__ == "__main__":
    unittest.main()
