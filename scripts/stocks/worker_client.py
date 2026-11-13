"""The stock run's side of the Worker's ingest routes (contract 10.6 and 11.3).

    client = WorkerClient(os.environ["WORKER_URL"], os.environ["STOCKS_INGEST_KEY"])
    cfg = client.get_config(models=stocks_cfg["models"], default=stocks_cfg["model"])
    state = client.get_state("AAPL")          # None when the Worker has none yet
    client.put_state("AAPL", state)           # bytes stored
    client.put_ticker("AAPL", document)
    client.put_overview(overview)

Every request sends "Authorization: Bearer <STOCKS_INGEST_KEY>", JSON, the
User-Agent news-digest-stocks/1.0 and no Origin (the Worker refuses ingest
requests from a browser). A 5xx answer or a connection error is retried once.
A body over its cap is never sent. WorkerError.reason is short and safe to
log: it never holds the address, a ticker, the key or a body.
"""
import gzip
import json
import re
import time
import urllib.parse

import stocks

USER_AGENT = "news-digest-stocks/1.0"
BASE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+$")
CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
RETRY_WAIT = 2.0
MAX_OVERRIDE_VALUES = 10
MAX_NAME_CHARS = 200
# The largest answer read: a state of 1 MiB, with room for the JSON envelope.
READ_CAP = stocks.STATE_MAX_BYTES + 65_536


class WorkerError(Exception):
    """A request to the Worker failed. reason is plain and safe to log."""

    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.status = status


def _default_transport():
    from stocks.data import urllib_transport

    return urllib_transport(max_bytes=READ_CAP)


def _reason(status):
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "refused"
    if status == 413:
        return "too large"
    return f"HTTP {status}"


def clean_overrides(raw, tickers):
    """The overrides of the tickers in the list only: {T: {"cusips": [...],
    "names": [...]}}, each list at most 10 checked values."""
    out = {}
    if not isinstance(raw, dict):
        return out
    listed = set(tickers)
    for key, entry in raw.items():
        t = stocks.normalize_ticker(key)
        if t is None or t not in listed or not isinstance(entry, dict):
            continue
        cusips = []
        for c in entry.get("cusips") or ():
            c = str(c or "").strip().upper()
            if CUSIP_RE.match(c) and c not in cusips:
                cusips.append(c)
        names = []
        for n in entry.get("names") or ():
            n = " ".join(str(n or "").split())[:MAX_NAME_CHARS]
            if n and n not in names:
                names.append(n)
        if cusips or names:
            out[t] = {"cusips": cusips[:MAX_OVERRIDE_VALUES], "names": names[:MAX_OVERRIDE_VALUES]}
    return out


def clean_config(doc, models=None, default=stocks.DEFAULT_MODEL):
    """(config, info) from the Worker's answer: tickers normalized,
    deduplicated and at most MAX_TICKERS; the model in ALLOWED_MODELS and in
    the configured models, else the default. info counts what was changed
    ({"dropped": n, "over_limit": n, "model_reset": bool}), safe to log."""
    if not isinstance(doc, dict):
        raise WorkerError("the settings are not a JSON object")
    raw = doc.get("tickers")
    if not isinstance(raw, list):
        raise WorkerError("the settings have no ticker list")
    tickers, dropped = [], 0
    for value in raw:
        t = stocks.normalize_ticker(value)
        if t is None:
            dropped += 1
        elif t not in tickers:
            tickers.append(t)
    over = max(0, len(tickers) - stocks.MAX_TICKERS)
    tickers = tickers[:stocks.MAX_TICKERS]
    allowed = [m for m in stocks.ALLOWED_MODELS if models is None or m in models]
    fallback = default if default in allowed else (stocks.DEFAULT_MODEL if stocks.DEFAULT_MODEL in allowed
                                                   else (allowed[0] if allowed else stocks.DEFAULT_MODEL))
    model = doc.get("model")
    reset = not (isinstance(model, str) and model in allowed)
    if reset:
        model = fallback
    cfg = {"tickers": tickers, "model": model, "overrides": clean_overrides(doc.get("overrides"), tickers)}
    return cfg, {"dropped": dropped, "over_limit": over, "model_reset": bool(reset and doc.get("model") is not None)}


class WorkerClient:
    def __init__(self, base_url, ingest_key, *, timeout=30.0, transport=None, sleep=time.sleep):
        url = str(base_url or "").strip().rstrip("/")
        if not BASE_URL_RE.match(url):
            raise WorkerError("the Worker address must look like https://name.example.workers.dev, with no path")
        key = str(ingest_key or "").strip()
        if not key:
            raise WorkerError("no ingest key")
        self._base = url
        self._key = key
        self.timeout = float(timeout)
        self._transport = transport
        self._sleep = sleep
        self.requests = 0
        self.last_config_info = None

    def __repr__(self):  # never show the address or the key
        return f"WorkerClient(requests={self.requests})"

    # ------------------------------------------------------------ requests

    def _send(self, method, path, body=None, allow=()):
        """(status, parsed JSON or None) for one request, with one retry on a
        5xx answer or a connection error."""
        if self._transport is None:
            self._transport = _default_transport()
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        url = self._base + path
        tries = 2
        while True:
            tries -= 1
            self.requests += 1
            try:
                status, rheaders, raw = self._transport(method, url, headers, body, self.timeout)
            except Exception as e:  # connection errors, timeouts, an oversized answer
                reason = getattr(e, "reason", None)
                if isinstance(reason, str) and reason == "response too large":
                    raise WorkerError("too large") from None
                if tries > 0:
                    self._sleep(RETRY_WAIT)
                    continue
                raise WorkerError("connection error") from None
            status = int(status)
            if 500 <= status <= 599 and tries > 0:
                self._sleep(RETRY_WAIT)
                continue
            if not (200 <= status < 300) and status not in allow:
                raise WorkerError(_reason(status), status)
            return status, self._parse(rheaders, raw)

    @staticmethod
    def _parse(rheaders, raw):
        raw = raw or b""
        enc = str({str(k).lower(): v for k, v in (rheaders or {}).items()}.get("content-encoding") or "").lower()
        if enc in ("gzip", "x-gzip"):
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                raise WorkerError("the answer could not be read") from None
        if len(raw) > READ_CAP:
            raise WorkerError("too large")
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: nested too deep
            raise WorkerError("the answer is not JSON") from None

    def _put(self, path, doc, cap):
        body = stocks.dumps(doc).encode("utf-8")
        if len(body) > cap:
            raise WorkerError("too large")
        _, answer = self._send("PUT", path, body)
        n = answer.get("bytes") if isinstance(answer, dict) else None
        return int(n) if isinstance(n, int) and not isinstance(n, bool) else len(body)

    @staticmethod
    def _ticker_path(kind, ticker):
        t = stocks.normalize_ticker(ticker)
        if t is None or t != ticker:
            raise WorkerError("not a ticker")
        return f"/stocks/ingest/{kind}/" + urllib.parse.quote(t, safe="")

    # ------------------------------------------------------------ the routes

    def get_config(self, models=None, default=stocks.DEFAULT_MODEL):
        """{"tickers": [...], "model": str, "overrides": {...}}, checked (see
        clean_config). last_config_info says what the check changed."""
        _, doc = self._send("GET", "/stocks/ingest/config")
        if len(stocks.dumps(doc).encode("utf-8")) > stocks.CONFIG_MAX_BYTES:
            raise WorkerError("too large")
        cfg, info = clean_config(doc, models, default)
        self.last_config_info = info
        return cfg

    def get_state(self, ticker):
        """The ticker's learning state, or None when the Worker has none."""
        status, doc = self._send("GET", self._ticker_path("state", ticker), allow=(404,))
        if status == 404:
            return None
        if not isinstance(doc, dict):
            raise WorkerError("the state is not a JSON object")
        return doc

    def put_state(self, ticker, state):
        return self._put(self._ticker_path("state", ticker), state, stocks.STATE_MAX_BYTES)

    def put_ticker(self, ticker, doc):
        return self._put(self._ticker_path("ticker", ticker), doc, stocks.DOC_MAX_BYTES)

    def put_overview(self, doc):
        return self._put("/stocks/ingest/overview", doc, stocks.OVERVIEW_MAX_BYTES)
