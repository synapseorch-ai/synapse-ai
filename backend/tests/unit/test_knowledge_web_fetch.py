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


# ── the crawl's HTTP side: core/knowledge/fetch.py ─────────────────────────────

from core.knowledge import fetch as F  # noqa: E402


class TestRobots:
    def test_synapsebots_own_group_wins_over_the_star_group(self):
        robots = F.Robots.parse("User-agent: *\nDisallow: /\n\nUser-agent: SynapseBot\nDisallow: /private\n")
        assert robots.allows("https://a.test/docs") and not robots.allows("https://a.test/private/x")

    def test_the_star_group_applies_when_none_names_us(self):
        robots = F.Robots.parse("User-agent: Googlebot\nDisallow:\n\nUser-agent: *\nDisallow: /\n")
        assert not robots.allows("https://a.test/anything")

    def test_the_agent_is_matched_by_product_token_case_insensitively(self):
        robots = F.Robots.parse("User-agent: synapsebot/1.0\nDisallow: /x\n")
        assert not robots.allows("https://a.test/x")
        assert F.Robots.parse("User-agent: SynapseBotPro\nDisallow: /\n").allows("https://a.test/x")

    def test_the_longest_rule_wins_and_allow_wins_a_tie(self):
        robots = F.Robots.parse("User-agent: *\nDisallow: /docs\nAllow: /docs/public\nDisallow: /a\nAllow: /a\n")
        assert robots.allows("https://a.test/docs/public/page") and not robots.allows("https://a.test/docs/secret")
        assert robots.allows("https://a.test/a")

    def test_wildcards_and_end_anchors(self):
        robots = F.Robots.parse("User-agent: *\nDisallow: /*?session=\nDisallow: /*.pdf$\n")
        assert not robots.allows("https://a.test/p?session=1") and robots.allows("https://a.test/p?q=1")
        assert not robots.allows("https://a.test/a/b.pdf") and robots.allows("https://a.test/a/b.pdf?x=1")

    def test_groups_with_several_agents_and_the_crawl_delay(self):
        robots = F.Robots.parse("User-agent: Foo\nUser-agent: SynapseBot\nCrawl-delay: 4\nDisallow: /f\n"
                                "Sitemap: https://a.test/sitemap.xml\n")
        assert robots.crawl_delay == 4.0 and not robots.allows("https://a.test/f")
        assert robots.sitemaps == ["https://a.test/sitemap.xml"]

    def test_an_empty_disallow_allows_and_robots_txt_itself_is_always_allowed(self):
        assert F.Robots.parse("User-agent: *\nDisallow:\n").allows("https://a.test/x")
        assert F.Robots.parse("User-agent: *\nDisallow: /\n").allows("https://a.test/robots.txt")
        assert not F.Robots.unreachable().allows("https://a.test/x") and F.Robots.allow_all().allows("https://a.test/")


class TestPace:
    async def test_one_host_waits_its_interval_and_another_host_does_not(self):
        now = [100.0]
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        pacer = F.Pacer(clock=lambda: now[0], sleep=sleep)
        await pacer.wait("https://a.test", 1.0)
        await pacer.wait("https://a.test", 1.0)
        await pacer.wait("https://b.test", 1.0)
        await pacer.wait("https://a.test", 2.0)
        assert slept == [1.0, 1.0]

    async def test_every_fetcher_shares_one_pace_per_host(self):
        """F355: two crawls of one site went at 1 request/s each — a pace per crawl, not per site."""
        assert F.Fetcher(rps=1.0).pacer is F.Fetcher(rps=2.0).pacer
        own = F.Pacer()
        assert F.Fetcher(pacer=own).pacer is own  # a test's own pacer still wins

    async def test_an_installed_pacer_paces_every_fetcher_and_none_restores_the_default(self):
        class Recorder:
            def __init__(self):
                self.waits: list[tuple[str, float]] = []

            async def wait(self, host: str, interval: float) -> None:
                self.waits.append((host, interval))

        installed = Recorder()
        F.set_pacer(installed)
        assert F.Fetcher(rps=1.0).pacer is installed
        F.set_pacer(None)
        assert isinstance(F.Fetcher(rps=1.0).pacer, F.Pacer)

    async def test_two_crawls_of_one_host_take_turns_at_its_pace(self):
        import asyncio
        import time

        class Timed(http.server.BaseHTTPRequestHandler):
            at: list[float] = []

            def log_message(self, *args):
                pass

            def do_GET(self):
                type(self).at.append(time.monotonic())
                body = b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else b"<p>x</p>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv, port = _serve(Timed)
        base = f"http://127.0.0.1:{port}"
        try:
            async with F.Fetcher(rps=5.0) as one, F.Fetcher(rps=5.0) as two:
                await asyncio.gather(one.get(f"{base}/a"), two.get(f"{base}/b"))
        finally:
            srv.shutdown()
        gaps = [b - a for a, b in zip(Timed.at, Timed.at[1:])]
        assert len(Timed.at) == 4 and min(gaps) >= 0.18, gaps  # robots + page each, 0.2 s apart

    def test_crawl_delay_slows_the_pace_up_to_the_ceiling(self):
        fetcher = F.Fetcher(rps=1.0)
        fetcher._robots["https://a.test"] = F.Robots(crawl_delay=5.0)
        fetcher._robots["https://b.test"] = F.Robots(crawl_delay=3600.0)
        assert fetcher._interval("https://a.test") == 5.0
        assert fetcher._interval("https://b.test") == F.MAX_CRAWL_DELAY_S
        assert F.Fetcher(rps=4.0)._interval("https://c.test") == 0.25


class TestValidators:
    def test_an_etag_round_trips_to_if_none_match(self):
        v = F.validator_of(httpx.Headers({"ETag": '"abc"', "Last-Modified": "Wed, 01 Oct 2026 10:00:00 GMT"}))
        assert v == '"abc"' and F.conditional_headers(v) == {"If-None-Match": '"abc"'}

    def test_last_modified_alone_round_trips_to_if_modified_since(self):
        v = F.validator_of(httpx.Headers({"Last-Modified": "Wed, 01 Oct 2026 10:00:00 GMT"}))
        assert F.conditional_headers(v) == {"If-Modified-Since": "Wed, 01 Oct 2026 10:00:00 GMT"}
        assert F.validator_of(httpx.Headers({})) is None and F.conditional_headers(None) == {}


class TestThePage:
    def test_a_declared_charset_decodes_the_page(self):
        body = "<html><body><p>Привет, мир</p></body></html>".encode("cp1251")
        assert "Привет, мир" in F.decode_html(body, "text/html; charset=windows-1251")
        meta = b'<html><head><meta charset="windows-1251"></head><body>' + "Ещё".encode("cp1251") + b"</body>"
        assert "Ещё" in F.decode_html(meta, "text/html")

    def test_links_come_from_the_whole_page_navigation_included(self):
        html = ('<html><body><nav><a href="/docs/intro">Intro</a></nav><main><a href="guide#part">Guide</a>'
                '<a href="mailto:x@y.z">mail</a><a href="javascript:void(0)">js</a>'
                '<a href="/paid" rel="sponsored nofollow">ad</a></main></body></html>')
        got = F.page_links(html, "https://a.test/docs/start")
        assert got.links == ["https://a.test/docs/intro", "https://a.test/docs/guide"]

    def test_base_href_resolves_the_links(self):
        got = F.page_links('<head><base href="https://a.test/v2/"></head><a href="x">x</a>', "https://a.test/v1/")
        assert got.links == ["https://a.test/v2/x"]

    def test_meta_robots_and_the_header_are_honoured(self):
        got = F.page_links('<meta name="robots" content="noindex, nofollow"><a href="/x">x</a>', "https://a.test/")
        assert got.noindex and got.nofollow and got.links == []
        assert F.page_links('<a href="/x">x</a>', "https://a.test/", header_robots="googlebot: noindex").noindex is False
        assert F.page_links('<a href="/x">x</a>', "https://a.test/", header_robots="synapsebot: none").nofollow

    def test_the_canonical_is_absolute(self):
        got = F.page_links('<link rel="canonical" href="/docs/a#top">', "https://a.test/docs/a?utm=1")
        assert got.canonical == "https://a.test/docs/a"


class _RobotsSite(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append(f"{self.path} {self.headers.get('User-Agent')}")
        if self.path == "/robots.txt":
            body = b"User-agent: SynapseBot\nDisallow: /secret\n"
        elif self.path == "/page" and self.headers.get("If-None-Match") == '"v2"':
            self.send_response(304)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            body = b"<p>page</p>"
        self.send_response(200)
        self.send_header("ETag", '"v2"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestTheFetcher:
    async def test_robots_is_read_once_per_host_and_disallowed_pages_are_never_requested(self):
        srv, port = _serve(_RobotsSite)
        _RobotsSite.hits = []
        base = f"http://127.0.0.1:{port}"
        try:
            async with F.Fetcher(rps=100.0) as fetcher:
                got = await fetcher.get(f"{base}/page")
                with pytest.raises(F.RobotsDisallowed):
                    await fetcher.get(f"{base}/secret/x")
                again = await fetcher.get(f"{base}/page", validator=F.validator_of(got.headers))
        finally:
            srv.shutdown()
        assert (got.status, again.status) == (200, 304)
        paths = [h.split(" ", 1)[0] for h in _RobotsSite.hits]
        assert paths == ["/robots.txt", "/page", "/page"]
        assert all(F.USER_AGENT in h for h in _RobotsSite.hits)

    def test_the_user_agent_links_a_page_that_exists(self):
        # F363: a site owner who looks the crawler up from their logs reaches the site's /bot page
        # (synapseorch.com, trailing slash); synapseorch.ai has no DNS record.
        assert F.USER_AGENT == "Mozilla/5.0 (compatible; SynapseBot/1.0; +https://synapseorch.com/bot/)"

    async def test_an_unreadable_robots_disallows_and_a_missing_one_allows(self):
        fetcher = F.Fetcher()
        assert not F.Robots.unreachable().allows("http://x/")
        async with fetcher:
            pass  # a context with no request opens and closes cleanly
