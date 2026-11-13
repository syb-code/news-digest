"""Fakes for the offline tests of the stock run (scripts/stocks/run.py,
claude.py, worker_client.py): a web of public sources served from the
fixtures and synthetic data, a Worker with an in-memory KV namespace, and an
Anthropic client that answers from the request it gets.

Tickers are public examples (AAPL, MSFT) or made up (EXMPL). No helper
names a ticker the owner tracks.
"""
import gzip
import json
import math
import re
import threading
import urllib.parse
from datetime import date
from types import SimpleNamespace

import stocks_support as ss  # puts scripts/ on sys.path

NOW = "2026-11-13T13:50:00+00:00"  # Friday 08:50 in New York; the last close is 2026-11-12
AS_OF = "2026-11-12"
INGEST_KEY = "ingest-key-for-tests-0123456789abcdef"
USER_AGENT = "news-digest tests contact@example.com"
BARS = 520


# ---------------------------------------------------------------- synthetic series

def tiingo_rows(bars):
    """Bars in the shape Tiingo serves (adjusted fields equal to raw ones)."""
    out = []
    for b in bars:
        out.append({"date": b["date"] + "T00:00:00.000Z", "open": b["open"], "high": b["high"], "low": b["low"],
                    "close": b["raw_close"], "volume": b["raw_volume"], "adjOpen": b["open"], "adjHigh": b["high"],
                    "adjLow": b["low"], "adjClose": b["close"], "adjVolume": b["raw_volume"], "divCash": 0.0,
                    "splitFactor": 1.0})
    return out


def series_bars(seed, price, n=BARS, end=AS_OF):
    start = ss.start_for(end, n)
    return ss.synthetic_bars(n=n, seed=seed, start=start, price=price, drift=0.0004, vol=0.017)


def fred_csv(points, sid):
    lines = [f"observation_date,{sid}"] + [f"{d},{v}" for d, v in points]
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------- the public web

class FakeWeb:
    """A transport (method, url, headers, body, timeout) -> (status, headers, raw)
    that serves the public sources. Records every request."""

    def __init__(self, prices=None, spy=None, tiingo_status=None, fail_hosts=()):
        self.prices = prices or {}
        self.spy = spy
        self.tiingo_status = tiingo_status
        self.fail_hosts = set(fail_hosts)
        self.requests = []
        self.lock = threading.Lock()
        dates = [b["date"] for b in (spy or series_bars(11, 420.0))]
        self.fred_synthetic = ss.synthetic_fred(dates)

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if host in self.fail_hosts:
            return 503, {}, b""
        try:
            status, payload, ctype = self.route(method, host, parts.path, urllib.parse.parse_qs(parts.query), body)
        except KeyError:
            return 404, {}, b"not found"
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        return status, {"content-type": ctype}, payload

    def route(self, method, host, path, query, body):
        fx = ss.fixture_bytes
        if host == "api.tiingo.com":
            m = re.match(r"^/tiingo/daily/([A-Za-z0-9.\-]+)/prices$", path)
            symbol = m.group(1)
            if self.tiingo_status:
                return self.tiingo_status, b"{}", "application/json"
            bars = self.spy if symbol == "SPY" else self.prices.get(symbol)
            if bars is None:
                return 404, {"detail": "Error: Ticker not found"}, "application/json"
            start = (query.get("startDate") or ["1900-01-01"])[0]
            return 200, tiingo_rows([b for b in bars if b["date"] >= start]), "application/json"
        if host == "www.sec.gov":
            if path == "/files/company_tickers.json":
                return 200, fx("sec_company_tickers.json"), "application/json"
            m = re.match(r"^/Archives/edgar/data/(\d+)/(\d{18})/(.+)$", path)
            if m:
                cik, acc, name = m.groups()
                if cik == "1067983" and name == "index.json":
                    return 200, fx("sec_13f_filing_index.json"), "application/json"
                if cik == "1067983" and name == "56757.xml":
                    table = {"000119312526352200": "sec_13f_infotable_2026q2.xml",
                             "000119312526226661": "sec_13f_infotable_2026q1.xml"}[acc]
                    return 200, fx(table), "application/xml"
                if cik == "320193" and name == "form4.xml":
                    which = "sec_form4_planned_sale.xml" if int(acc[-1]) % 2 else "sec_form4_exercise_and_sale.xml"
                    return 200, fx(which), "application/xml"
            raise KeyError(path)
        if host == "data.sec.gov":
            subs = {"/submissions/CIK0000320193.json": "sec_submissions_aapl.json",
                    "/submissions/CIK0001067983.json": "sec_submissions_berkshire.json"}
            facts = {"/api/xbrl/companyfacts/CIK0000320193.json": "sec_companyfacts_aapl.json"}
            name = subs.get(path) or facts[path]
            return 200, fx(name), "application/json"
        if host == "api.finra.org":
            req = json.loads(body.decode("utf-8"))
            symbol = req["compareFilters"][0]["fieldValue"]
            if symbol != "AAPL":
                return 204, b"", "application/json"
            name = "finra_regsho_daily_aapl.json" if path.endswith("regShoDaily") else "finra_short_interest_aapl.json"
            return 200, fx(name), "application/json"
        if host == "cdn.finra.org":
            return 403, b"<Error>AccessDenied</Error>", "application/xml"
        if host == "fred.stlouisfed.org":
            sid = query["id"][0]
            if sid in self.fred_synthetic:
                return 200, fred_csv(self.fred_synthetic[sid], sid), "text/csv"
            return 200, fx(f"fred_{sid.lower()}.csv"), "text/csv"
        if host == "www.federalreserve.gov":
            if path == "/monetarypolicy/fomccalendars.htm":
                return 200, fx("fomc_calendars.html"), "text/html"
            if path == "/feeds/press_monetary.xml":
                return 200, fx("fed_press_monetary.xml"), "application/xml"
        if host == "www.bls.gov":
            if path == "/schedule/news_release/bls.ics":
                return 200, fx("bls_schedule.ics"), "text/calendar"
            if path == "/bls/news-release/cpi.htm":
                return 200, fx("bls_cpi_archive.html"), "text/html"
            if path == "/feed/cpi.rss":
                return 200, fx("bls_cpi_feed.atom"), "application/atom+xml"
        if host == "www.bea.gov":
            if path == "/news/schedule/ics/online-calendar-subscription.ics":
                return 200, fx("bea_schedule.ics"), "text/calendar"
            if path == "/news/current-releases":
                return 200, fx("bea_current_releases.html"), "text/html"
        raise KeyError(path)


# ---------------------------------------------------------------- the Worker

class FakeWorker:
    """The Worker's ingest routes over a dict (contract 11.3), as a transport
    for WorkerClient. Records every request."""

    def __init__(self, tickers, model="claude-sonnet-5-5", overrides=None, fail=None):
        self.kv = {"stocks:config": {"version": 1, "tickers": list(tickers), "overrides": overrides or {}},
                   "stocks:settings": {"version": 1, "model": model}}
        self.requests = []
        self.fail = fail or {}  # path -> status
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.requests.append({"method": method, "url": url, "headers": dict(headers),
                                  "body": None if body is None else body.decode("utf-8")})
        path = urllib.parse.urlsplit(url).path
        if path in self.fail:
            return self.fail[path], {}, b'{"error": "test"}'
        if headers.get("Authorization") != f"Bearer {INGEST_KEY}" or "Origin" in headers:
            return 401, {}, b'{"error": "unauthorized"}'
        cfg = self.kv["stocks:config"]
        if method == "GET" and path == "/stocks/ingest/config":
            doc = {"version": 1, "tickers": cfg["tickers"], "model": self.kv["stocks:settings"]["model"],
                   "overrides": cfg["overrides"]}
            return 200, {"content-encoding": "gzip"}, gzip.compress(json.dumps(doc).encode("utf-8"))
        m = re.match(r"^/stocks/ingest/(state|ticker)/([^/]+)$", path)
        if m:
            kind, t = m.group(1), urllib.parse.unquote(m.group(2))
            if t not in cfg["tickers"]:
                return 409, {}, b'{"error": "not in the list"}'
            key = f"stocks:{kind}:{t}"
            if method == "GET" and kind == "state":
                if key not in self.kv:
                    return 404, {}, b'{"error": "no state"}'
                return 200, {}, json.dumps(self.kv[key]).encode("utf-8")
            if method == "PUT":
                doc = json.loads(body.decode("utf-8"))
                want = "stocks.state" if kind == "state" else "stocks.ticker"
                if doc.get("kind") != want or doc.get("version") != 1 or doc.get("ticker") != t:
                    return 400, {}, b'{"error": "bad document"}'
                self.kv[key] = doc
                return 200, {}, json.dumps({"ok": True, "bytes": len(body)}).encode("utf-8")
        if method == "PUT" and path == "/stocks/ingest/overview":
            doc = json.loads(body.decode("utf-8"))
            if doc.get("kind") != "stocks.overview":
                return 400, {}, b'{"error": "bad document"}'
            self.kv["stocks:overview"] = doc
            return 200, {}, json.dumps({"ok": True, "bytes": len(body)}).encode("utf-8")
        return 404, {}, b'{"error": "not found"}'


# ---------------------------------------------------------------- Anthropic

def usage(input_tokens=12000, output_tokens=3000, iterations=None):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, cache_creation=None, iterations=iterations)


def message(answer, stop_reason="end_turn", model="claude-sonnet-5-5", use=None, stop_details=None):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return SimpleNamespace(stop_reason=stop_reason, stop_details=stop_details, model=model,
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           usage=use or usage(), _request_id="req_test")


def payload_of(request):
    text = request["messages"][0]["content"]
    m = re.search(r"<(stock|market)>\n(.*)\n</(?:stock|market)>", text, re.S)
    return m.group(1), json.loads(m.group(2))


def ticker_answer(payload, lessons=(), off_candidate=False):
    """An answer whose prices are the code's own candidates."""
    cands = {c["id"]: c["price"] for c in payload["levels"]["candidates"]}
    close = payload["technicals"]["close"]
    target = next((cands[k] for k in ("r1", "r2", "hi52", "atr_p2") if k in cands and cands[k] > cands["atr_m1"]),
                  cands["atr_p2"])
    return {
        "stance": "neutral", "confidence": "low",
        "summary": "The stock is in a range. The model's own record here is close to a coin flip.",
        "technical": "Price sits near its 50 day average.", "valuation": "No view on value today.",
        "shorts": "Short volume is normal for this stock.", "holders": "Nothing unusual from insiders.",
        "macro": "Rates matter for this stock.", "risks": ["A broad sell off."],
        "entry_zones": [{"low": cands["atr_m2"], "high": cands["atr_m1"], "rationale": "Two to one ATR below."}],
        "targets": [{"price": round(close * 1.37, 2) if off_candidate else target, "horizon_days": 20,
                     "rationale": "The nearest level above."}],
        "stop": {"price": cands["atr_m3"], "rationale": "Three ATR below."},
        "change_my_mind": "A close below the stop.", "lessons": list(lessons)}


def overview_answer(payload):
    dates = [e["date"] for e in payload["calendar"]]
    urls = [n["url"] for n in payload["news"]]
    return {"headline": "Inflation data and the Fed set the tone",
            "summary": "Yields are high. Inflation data this week matters most.",
            "this_week": [{"date": d, "event": "A release", "why_it_matters": "It moves rates."} for d in dates[:3]],
            "watch": ["The 10 year yield."], "risks": ["A hot print."],
            "news": [{"url": u, "why": "Context."} for u in urls[:2]]}


class FakeAnthropic:
    """client.beta.messages.stream(**request) that answers from the request:
    the overview from its calendar and news, each ticker with its own
    candidate levels. behavior(kind, payload, request) may return a message or
    an exception to use instead."""

    def __init__(self, behavior=None, lessons=()):
        self.requests = []
        self.lock = threading.Lock()
        self.behavior = behavior
        self.lessons = lessons

        def stream(**request):
            with self.lock:
                self.requests.append(request)
            kind, payload = payload_of(request)
            out = self.behavior(kind, payload, request) if self.behavior else None
            if isinstance(out, BaseException):
                raise out
            if out is None:
                answer = overview_answer(payload) if kind == "market" else ticker_answer(payload, self.lessons)
                out = message(answer, model=request["model"])
            return FakeStream(out)

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


def api_error(cls, status):
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers={"request-id": "req_err"})
    return cls("error", response=response, body=None)


# ---------------------------------------------------------------- the House artifact

def house_bundle(ticker="AAPL", when="2026-09-08"):
    trade = {"member": "Hon. Example Member", "chamber": "House", "district": "XX01", "ticker": ticker,
             "asset": "Example Asset - Common Stock", "asset_type": "ST", "type": "purchase", "owner": "self",
             "date": when, "notified": "2026-09-15", "filed": "2026-09-25", "amount_range": "$1,001 - $15,000",
             "source": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf"}
    return {"kind": "stocks.congress", "version": 1, "generated_at": NOW, "window_days": 365, "filings": 1,
            "electronic": 1, "scanned": 0, "failed": 0, "skipped_for_time": 0, "trades": [trade]}


def prices_text(bars):
    """Every way a price of these bars could be printed (for leak checks)."""
    out = set()
    for b in bars[-30:]:
        for key in ("close", "high", "low", "raw_close"):
            v = b[key]
            out.update({f"{v:.2f}", f"{v:.4f}".rstrip("0").rstrip("."), str(v)})
    return {s for s in out if len(s) >= 5}


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


__all__ = ["FakeWeb", "FakeWorker", "FakeAnthropic", "message", "usage", "api_error", "house_bundle", "NOW",
           "AS_OF", "INGEST_KEY", "USER_AGENT", "series_bars", "prices_text", "math"]
