"""`safe_fetch.fetch`, the crawler's GET (S1): the egress policy on every hop, the answer handed back.

The stand-in for "the public internet" is a loopback server named in
`EGRESS_ALLOW_HOSTS` (as `tests/security/test_parser_egress.py`); `localhost`
is not on the list, so a redirect there is a hop into a refused range. A second
loopback server plays I1's Smokescreen: it refuses a proxied request with 407
and `X-Smokescreen-Error`, and an HTTPS tunnel (CONNECT) with a bare 407 —
which httpcore turns into `httpx.ProxyError`, header lost.
"""
from __future__ import annotations

import http.server
import threading

import httpx
import pytest

from core import egress_guard, safe_fetch

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
              "no_proxy")


class _Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path == "/page":
            if self.headers.get("If-None-Match") == '"v1"':
                self._send(304, ETag='"v1"')
            else:
                self._send(200, b"<p>hello</p>", Content_Type="text/html; charset=utf-8", ETag='"v1"')
        elif path.startswith("/redirect?to="):
            self.send_response(302)
            self.send_header("Location", path.split("?to=", 1)[1])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/loop":
            self._send(301, Location="/loop")
        elif path == "/agent":
            self._send(200, self.headers.get("User-Agent", "").encode())
        elif path == "/big":
            self._send(200, b"x" * 5000)
        elif path == "/missing":
            self._send(404, b"x" * 5000)
        else:
            self._send(500, b"oops")


class _Smokescreen(http.server.BaseHTTPRequestHandler):
    """Refuses everything, as Smokescreen does an address in a refused range."""

    def log_message(self, *args):
        pass

    def do_GET(self):  # a plain-HTTP request through the proxy: the request line holds the absolute URL
        self.send_response(407, "Request Rejected by Proxy")
        self.send_header("X-Smokescreen-Error", "Egress proxying is denied to host 'example.test': private range")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_CONNECT(self):  # an HTTPS tunnel
        self.send_response(407, "Request Rejected by Proxy")
        self.send_header("X-Smokescreen-Error", "Egress proxying is denied to host 'example.test': private range")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler) -> tuple[http.server.ThreadingHTTPServer, int]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture(scope="module")
def site():
    srv, port = _serve(_Site)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def smokescreen():
    srv, port = _serve(_Smokescreen)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _public_is_127(monkeypatch):
    """Loopback plays the internet; a developer's own proxy settings never route the test traffic."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1,example.test")
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


async def _fetch(url: str, **kw) -> safe_fetch.Fetched:
    async with httpx.AsyncClient() as client:
        return await safe_fetch.fetch(url, client=client, **kw)


class TestTheAnswer:
    async def test_a_page_comes_back_with_its_headers(self, site):
        got = await _fetch(f"{site}/page")
        assert (got.status, got.body, got.url) == (200, b"<p>hello</p>", f"{site}/page")
        assert got.headers["etag"] == '"v1"' and got.redirects == ()

    async def test_a_conditional_get_is_a_304_with_no_body(self, site):
        got = await _fetch(f"{site}/page", headers={"If-None-Match": '"v1"'})
        assert (got.status, got.body) == (304, b"")

    async def test_an_error_status_is_an_answer_and_its_body_is_never_read(self, site):
        got = await _fetch(f"{site}/missing", max_bytes=100)  # a 5000-byte body over the cap: not read
        assert (got.status, got.body) == (404, b"")

    async def test_redirects_are_followed_here_and_named(self, site):
        got = await _fetch(f"{site}/redirect?to=/page")
        assert (got.status, got.url) == (200, f"{site}/page")
        assert got.redirects == (f"{site}/redirect?to=/page",)

    async def test_the_callers_headers_are_sent(self, site):
        got = await _fetch(f"{site}/agent", headers={"User-Agent": "SynapseBot/test"})
        assert got.body == b"SynapseBot/test"


class TestRefusals:
    async def test_a_redirect_into_a_refused_range_is_blocked(self, site):
        port = site.rsplit(":", 1)[1]
        with pytest.raises(safe_fetch.FetchBlocked):
            await _fetch(f"{site}/redirect?to=http://localhost:{port}/page")

    async def test_a_refused_first_address_is_blocked(self):
        with pytest.raises(safe_fetch.FetchBlocked):
            await _fetch("http://169.254.169.254/latest/meta-data/")

    async def test_a_body_over_the_cap_is_too_large(self, site):
        with pytest.raises(safe_fetch.FetchTooLarge):
            await _fetch(f"{site}/big", max_bytes=100)

    async def test_too_many_redirects_is_a_failure_not_a_block(self, site):
        with pytest.raises(safe_fetch.FetchRefused) as caught:
            await _fetch(f"{site}/loop")
        assert not isinstance(caught.value, safe_fetch.FetchBlocked)

    async def test_nothing_listening_is_a_failure_not_a_block(self):
        srv, port = _serve(_Site)
        srv.shutdown()
        srv.server_close()
        with pytest.raises(safe_fetch.FetchRefused) as caught:
            await _fetch(f"http://127.0.0.1:{port}/page")
        assert not isinstance(caught.value, (safe_fetch.FetchBlocked, safe_fetch.FetchTooLarge))


class TestThroughTheEgressProxy:
    """httpx's defaults read the proxy from the environment (`trust_env`), as cloud's knowledge
    task sets it (I1): the client here is built after the variables, with no `transport=`."""

    async def test_a_407_answer_naming_the_rule_is_blocked(self, smokescreen, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", smokescreen)
        with pytest.raises(safe_fetch.FetchBlocked) as caught:
            await _fetch("http://example.test/page")
        assert "private range" in str(caught.value)

    async def test_a_refused_https_tunnel_is_blocked_though_its_header_is_lost(self, smokescreen, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", smokescreen)
        with pytest.raises(safe_fetch.FetchBlocked) as caught:
            await _fetch("https://example.test/page")
        assert "407" in str(caught.value)

    def test_only_a_407_proxy_error_is_a_refusal(self):
        assert safe_fetch.proxy_refusal(httpx.ProxyError("407 Request Rejected by Proxy")) is not None
        assert safe_fetch.proxy_refusal(httpx.ProxyError("502 Bad Gateway")) is None
        assert safe_fetch.proxy_refusal(httpx.ConnectError("refused")) is None


class TestFetchBytesIsUnchanged:
    """`fetch_bytes` (the PDF and spreadsheet tools) still raises `FetchRefused` for each refusal."""

    async def test_its_refusals_are_still_fetch_refused(self, site):
        port = site.rsplit(":", 1)[1]
        with pytest.raises(safe_fetch.FetchRefused):
            await safe_fetch.fetch_bytes(f"{site}/redirect?to=http://localhost:{port}/page")
        with pytest.raises(safe_fetch.FetchRefused):
            await safe_fetch.fetch_bytes(f"{site}/missing")
        assert await safe_fetch.fetch_bytes(f"{site}/page") == b"<p>hello</p>"
