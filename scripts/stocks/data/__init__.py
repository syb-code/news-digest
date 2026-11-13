"""The data layer of the stock section: one small HTTP client every source uses
(contract section 5.1).

HttpClient sends the caller's User-Agent and "Accept-Encoding: gzip" on every
request, decompresses gzip, caps the body size, retries a 5xx answer or a
connection error once after 2 seconds, waits out one 429 (Retry-After, at most
10 seconds), spaces requests to SEC hosts at least sec_interval seconds apart,
and stops making requests once its optional time budget is used up. It never
logs and never puts a URL, a header or a body in an error: FetchError.reason is
short and safe to log ("HTTP 404", "connection error", "rate limited").

Library functions in stocks.data never print. They raise FetchError (network,
HTTP) or ValueError (bad data); the caller decides what to log.
"""
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

SEC_HOSTS = frozenset({"data.sec.gov", "www.sec.gov"})
RETRY_WAIT = 2.0
MAX_RETRY_AFTER = 10.0
CHUNK = 65536


class FetchError(Exception):
    """A network or HTTP failure. reason is plain and safe to log: it never holds
    a URL, a ticker, a key or a response body."""

    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.status = status


class Response:
    def __init__(self, status, headers, body):
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body or b""

    def text(self):
        return self.body.decode("utf-8-sig", errors="replace")

    def json(self):
        """The parsed body; ValueError when it is not JSON."""
        try:
            return json.loads(self.text())
        except ValueError:
            raise ValueError("the answer is not valid JSON") from None


def _decompress(raw, encoding, max_bytes):
    """raw decoded per Content-Encoding (gzip or deflate), at most max_bytes."""
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        if len(raw) > max_bytes:
            raise FetchError("response too large")
        return raw
    if enc in ("gzip", "x-gzip"):
        wbits = 16 + zlib.MAX_WBITS
    elif enc == "deflate":
        wbits = zlib.MAX_WBITS
    else:
        raise FetchError("unsupported content encoding")
    try:
        d = zlib.decompressobj(wbits)
        out = d.decompress(raw, max_bytes + 1)
        if len(out) > max_bytes or d.unconsumed_tail:
            raise FetchError("response too large")
        out += d.flush()
    except zlib.error:
        if enc == "deflate":  # some servers send raw deflate without the zlib header
            try:
                return _decompress_raw_deflate(raw, max_bytes)
            except zlib.error:
                pass
        raise FetchError("bad compressed body") from None
    if len(out) > max_bytes:
        raise FetchError("response too large")
    return out


def _decompress_raw_deflate(raw, max_bytes):
    d = zlib.decompressobj(-zlib.MAX_WBITS)
    out = d.decompress(raw, max_bytes + 1)
    if len(out) > max_bytes or d.unconsumed_tail:
        raise FetchError("response too large")
    return out + d.flush()


_SECRET_HEADERS = ("authorization", "cookie", "proxy-authorization")


class _NoRedirectAcrossSchemes(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to https URLs, and never carries a credential
    header to another host (urllib copies every header to the new request)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https":
            raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
            if (target.hostname or "").lower() != old_host:
                for name in list(new.headers):
                    if name.lower() in _SECRET_HEADERS:
                        del new.headers[name]
                for name in list(new.unredirected_hdrs):
                    if name.lower() in _SECRET_HEADERS:
                        del new.unredirected_hdrs[name]
        return new


def urllib_transport(max_bytes=40_000_000, clock=time.monotonic):
    """The default transport: urllib.request (honors HTTPS_PROXY and the system
    CA store, SSL_CERT_FILE included). Returns (status, headers, raw body)."""
    opener = urllib.request.build_opener(_NoRedirectAcrossSchemes())

    def transport(method, url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        deadline = clock() + max(60.0, 3.0 * float(timeout))
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            resp = e
        try:
            status = resp.getcode() if hasattr(resp, "getcode") else resp.code
            hdrs = {k.lower(): v for k, v in resp.headers.items()} if resp.headers else {}
            # The compressed body can be up to the cap too; a bigger one cannot
            # decompress to anything we would keep.
            chunks, total = [], 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("response too large")
                if clock() > deadline:
                    raise FetchError("timed out")
                chunks.append(chunk)
            return status, hdrs, b"".join(chunks)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    return transport


_CONNECTION_ERRORS = (OSError, http.client.HTTPException, socket.timeout, TimeoutError, EOFError)


class HttpClient:
    """GET and POST with the rules in the module docstring.

    user_agent    sent on every request (never logged or stored)
    timeout       seconds per request (connect and each read)
    max_bytes     largest body accepted, after decompression
    sec_interval  minimum seconds between two requests to an SEC host
    transport     (method, url, headers, body, timeout) -> (status, headers, raw)
    budget        optional seconds this client may spend in all; once used up,
                  every request raises FetchError("time budget used up")
    """

    def __init__(self, user_agent, *, timeout=30.0, max_bytes=40_000_000, sec_interval=0.2,
                 transport=None, sleep=time.sleep, clock=time.monotonic, budget=None,
                 cache_bytes=64_000_000):
        if not user_agent or not str(user_agent).strip():
            raise ValueError("a User-Agent is required")
        self._user_agent = str(user_agent).strip()
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        self.sec_interval = float(sec_interval)
        self._transport = transport or urllib_transport(self.max_bytes, clock)
        self._sleep = sleep
        self._clock = clock
        self._started = clock()
        self.budget = None if budget is None else float(budget)
        self._last_sec = None
        self._lock = threading.Lock()  # one client is shared by the fetch threads
        self._cache = {}
        self._cache_bytes = 0
        self._cache_cap = int(cache_bytes)
        self.requests = 0
        self.failures = 0

    def __repr__(self):  # never show the User-Agent
        return f"HttpClient(requests={self.requests})"

    # ------------------------------------------------------------ budget

    def remaining(self):
        """Seconds left in the budget, or None without one."""
        if self.budget is None:
            return None
        return self.budget - (self._clock() - self._started)

    def out_of_time(self, reserve=0.0):
        left = self.remaining()
        return left is not None and left <= reserve

    # ------------------------------------------------------------ requests

    def get(self, url, *, headers=None, allow=(), cache=False):
        """GET url. cache=True keeps a 2xx answer in memory for the rest of the
        run (the same URL is not requested twice)."""
        if cache:
            with self._lock:
                hit = self._cache.get(url)
            if hit is not None:
                return hit
        resp = self._request("GET", url, headers, None, allow)
        if cache and 200 <= resp.status < 300:
            with self._lock:
                if url not in self._cache and self._cache_bytes + len(resp.body) <= self._cache_cap:
                    self._cache[url] = resp
                    self._cache_bytes += len(resp.body)
        return resp

    def post_json(self, url, payload, *, headers=None, allow=()):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h.update(headers or {})
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._request("POST", url, h, body, allow)

    def _space_sec(self, host):
        """Waits for this request's slot to an SEC host. The slot is reserved
        under a lock, so threads sharing the client stay sec_interval apart."""
        if host not in SEC_HOSTS or self.sec_interval <= 0:
            return
        with self._lock:
            now = self._clock()
            slot = now if self._last_sec is None else max(now, self._last_sec + self.sec_interval)
            self._last_sec = slot
        wait = slot - now
        if wait > 0:
            self._sleep(wait)

    def _count(self, failed=False):
        with self._lock:
            if failed:
                self.failures += 1
            else:
                self.requests += 1

    def _request(self, method, url, headers, body, allow):
        parts = urllib.parse.urlsplit(str(url))
        if parts.scheme != "https" or not parts.hostname:
            raise FetchError("only https URLs are fetched")
        host = parts.hostname.lower()
        send = {}
        for k, v in (headers or {}).items():
            if str(k).lower() not in ("user-agent", "accept-encoding"):
                send[str(k)] = str(v)
        send["User-Agent"] = self._user_agent
        send["Accept-Encoding"] = "gzip"

        server_retries = 1
        rate_retries = 1
        while True:
            left = self.remaining()
            if left is not None and left <= 0:
                raise FetchError("time budget used up")
            timeout = self.timeout if left is None else max(1.0, min(self.timeout, left))
            self._space_sec(host)
            self._count()
            try:
                status, rheaders, raw = self._transport(method, url, send, body, timeout)
            except FetchError:
                self._count(failed=True)
                raise
            except _CONNECTION_ERRORS:
                self._count(failed=True)
                if server_retries > 0 and not self.out_of_time(RETRY_WAIT):
                    server_retries -= 1
                    self._sleep(RETRY_WAIT)
                    continue
                raise FetchError("connection error") from None
            except Exception:
                # Anything else (http.client's ValueError for a header value, for
                # one) can quote a header in its message: a key must not escape.
                self._count(failed=True)
                raise FetchError("request could not be sent") from None
            rheaders = {str(k).lower(): str(v) for k, v in (rheaders or {}).items()}
            status = int(status)
            if 200 <= status < 300 or status in allow:
                data = _decompress(raw or b"", rheaders.get("content-encoding"), self.max_bytes)
                return Response(status, rheaders, data)
            self._count(failed=True)
            if status == 429:
                if rate_retries > 0:
                    wait = _retry_after(rheaders.get("retry-after"))
                    if not self.out_of_time(wait):
                        rate_retries -= 1
                        self._sleep(wait)
                        continue
                raise FetchError("rate limited", 429)
            if 500 <= status <= 599:
                if server_retries > 0 and not self.out_of_time(RETRY_WAIT):
                    server_retries -= 1
                    self._sleep(RETRY_WAIT)
                    continue
                raise FetchError(f"HTTP {status}", status)
            raise FetchError(f"HTTP {status}", status)


def _retry_after(value):
    """Seconds to wait for a 429: Retry-After in seconds, at most 10; 2 when absent
    or given as a date."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return RETRY_WAIT
    if seconds != seconds or seconds < 0:
        return RETRY_WAIT
    return min(seconds, MAX_RETRY_AFTER)


def attempt(fn, *args, **kwargs):
    """(result, None) from fn(*args, **kwargs), or (None, reason) when it raised
    FetchError or ValueError: the graceful path for one source."""
    try:
        return fn(*args, **kwargs), None
    except (FetchError, ValueError) as e:
        return None, safe_reason(e)


def safe_reason(e):
    """A short reason for any exception a source raised, safe to log."""
    if isinstance(e, FetchError):
        return e.reason
    if isinstance(e, ValueError):
        return "bad data"
    return type(e).__name__


__all__ = ["FetchError", "Response", "HttpClient", "urllib_transport", "attempt", "safe_reason", "SEC_HOSTS"]
