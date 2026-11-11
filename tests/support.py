"""Offline test helpers: a fake requests session, the recorded feeds and a
local HTTP server on 127.0.0.1 for the tests that need a real socket."""
import http.server
import os
import sys
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
FIXTURES = os.path.join(TESTS, "fixtures")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
YT_FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
YT_ABOUT_URL = "https://www.youtube.com/@ExampleChannel/about"
SUBSTACK_URL = "https://example.substack.com/feed"
OTHER_URL = "https://other.example.com/feed"


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        data = f.read()
    if name == "substack_feed.xml":
        # A post of about 1,300 words (6.5 minutes at 200 words a minute).
        paragraph = "<p>" + " ".join(["word"] * 100) + "</p>"
        data = data.replace(b"{{LONG_BODY}}", (paragraph * 13).encode("utf-8"))
    return data


class FakeResponse:
    def __init__(self, status=200, body=b"", url="", content_type="application/xml; charset=utf-8"):
        self.status_code = status
        self._body = body
        self.url = url
        self.headers = {"content-type": content_type}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Answers GETs from a route table: url -> FakeResponse, an exception to
    raise, or a list of those consumed one per call. Unknown URLs raise a
    connection error, so a test never reaches the network."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.headers = {}

    def get(self, url, headers=None, timeout=None, stream=False, **kwargs):
        import requests

        self.calls.append(url)
        route = self.routes.get(url)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if route is None:
            raise requests.ConnectionError(f"no route for {url}")
        if isinstance(route, BaseException):
            raise route
        if route.url == "":
            route.url = url
        return route


def ok(body, url=""):
    return FakeResponse(200, body, url)


def status(code):
    return FakeResponse(code, b"")


DRIP = object()  # a LocalServer route that never finishes its body


class LocalServer:
    """A real HTTP server on 127.0.0.1, run in a thread. routes maps a path to
    a body, or to DRIP: a 200 with Content-Length 100000 whose body arrives one
    byte every drip_interval seconds, the way a slow-drip host holds a reader."""

    def __init__(self, routes, drip_interval=0.05):
        stop = self.stop = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = routes.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Content-Length", "100000" if body is DRIP else str(len(body)))
                self.end_headers()
                if body is not DRIP:
                    self.wfile.write(body)
                    return
                try:
                    while not stop.is_set():
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(drip_interval)
                except OSError:  # the client hung up
                    pass

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
