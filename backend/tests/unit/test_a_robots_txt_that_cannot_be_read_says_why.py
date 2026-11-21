"""A robots.txt that cannot be read says why, so a timeout is never told as an outage (F1661).

Knowledge never fetches a page on a host whose robots.txt it cannot read (RFC 9309). Every way of not reading one
used to give the same words, "could not be read … tried again at the next crawl", so a site that only kept SynapseBot
waiting read exactly like a site that was down. Each case now names itself: a timeout says it timed out, a server error
names its status, a host that never answered names that, and an oversized file says so. The page is still not
fetched in every one of them.
"""
from __future__ import annotations

import http.server
import socket
import threading
import time

import pytest

from core import egress_guard, safe_fetch
from core.knowledge import fetch as F

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
              "no_proxy")


@pytest.fixture(autouse=True)
def _public_is_127(monkeypatch):
    """Loopback plays the internet; a developer's own proxy settings never route the test traffic."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1")
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


def _serve(handler) -> tuple[http.server.ThreadingHTTPServer, int]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _site(robots: str, seen: list[str]):
    """A site whose robots.txt answers as `robots` says, and whose pages answer 200."""

    class Site(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            if self.path == "/robots.txt":
                if robots == "slow":
                    time.sleep(2.0)
                    status, body = 200, b"User-agent: *\nAllow: /\n"
                elif robots == "503":
                    status, body = 503, b"down"
                elif robots == "huge":
                    status, body = 200, b"# " + b"x" * (F.ROBOTS_MAX_BYTES + 1024) + b"\n"
                elif robots == "404":
                    status, body = 404, b""
                else:
                    status, body = 200, b"User-agent: *\nAllow: /\n"
            else:
                status, body = 200, b"<p>page</p>"
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the fetcher gave up waiting and closed the connection

    return Site


async def _robots_refusal(robots: str, monkeypatch) -> tuple[str, list[str]]:
    """The words `get` refuses a page with when the host's robots.txt answers as `robots`, and the paths it asked."""
    monkeypatch.setattr(F, "REQUEST_TIMEOUT_S", 0.5)
    seen: list[str] = []
    srv, port = _serve(_site(robots, seen))
    try:
        async with F.Fetcher(rps=100.0) as fetcher:
            with pytest.raises(F.RobotsUnreachable) as caught:
                await fetcher.get(f"http://127.0.0.1:{port}/page")
    finally:
        srv.shutdown()
    return str(caught.value), seen


class TestEachWayOfNotReadingRobotsNamesItself:
    async def test_a_timeout_says_it_timed_out_and_is_never_called_a_server_error(self, monkeypatch):
        said, seen = await _robots_refusal("slow", monkeypatch)
        assert "timed out" in said and F.BOT in said and "0.5 seconds" in said, said
        assert "server error" not in said and "could not be read" not in said, said
        assert seen == ["/robots.txt"]  # the page itself was never asked for

    async def test_a_server_error_names_its_status_and_is_never_called_a_timeout(self, monkeypatch):
        said, seen = await _robots_refusal("503", monkeypatch)
        assert "503" in said and "server error" in said, said
        assert "timed out" not in said and "could not be read" not in said, said
        assert seen == ["/robots.txt"]

    async def test_an_oversized_robots_txt_says_it_is_too_large(self, monkeypatch):
        said, _ = await _robots_refusal("huge", monkeypatch)
        assert "larger than 512 KiB" in said, said
        assert "timed out" not in said and "server error" not in said, said

    async def test_a_host_that_never_answers_says_it_could_not_be_reached(self, monkeypatch):
        with socket.socket() as probe:  # a port nothing listens on: the connection is refused at once
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        async with F.Fetcher(rps=100.0) as fetcher:
            with pytest.raises(F.RobotsUnreachable) as caught:
                await fetcher.get(f"http://127.0.0.1:{port}/page")
        said = str(caught.value)
        assert "could not be reached" in said, said
        assert "timed out" not in said and "server error" not in said, said

    @pytest.mark.parametrize("robots", ["404", "ok"])
    async def test_a_robots_txt_that_answers_is_no_refusal_at_all(self, robots, monkeypatch):
        # The near misses: a missing robots.txt allows everything, a readable one is obeyed. Neither is "unreachable".
        monkeypatch.setattr(F, "REQUEST_TIMEOUT_S", 5.0)
        seen: list[str] = []
        srv, port = _serve(_site(robots, seen))
        try:
            async with F.Fetcher(rps=100.0) as fetcher:
                got = await fetcher.get(f"http://127.0.0.1:{port}/page")
        finally:
            srv.shutdown()
        assert got.status == 200 and seen == ["/robots.txt", "/page"]

    def test_every_way_still_disallows_the_page_and_keeps_the_old_words_when_no_reason_is_given(self):
        for why in ("timed out", "answered 503, a server error", ""):
            assert not F.Robots.unreachable(why).allows("https://a.test/x")
        assert "could not be read" in str(F.RobotsUnreachable("https://a.test/x"))


class TestATimeoutIsItsOwnRefusal:
    def test_a_timeout_is_still_a_refusal_so_every_handler_of_one_still_catches_it(self):
        assert issubclass(safe_fetch.FetchTimedOut, safe_fetch.FetchRefused)
        assert not issubclass(safe_fetch.FetchTimedOut, (safe_fetch.FetchBlocked, safe_fetch.FetchTooLarge))

    async def test_the_deadline_raises_a_timeout_and_a_refused_connection_does_not(self):
        import httpx

        seen: list[str] = []
        srv, port = _serve(_site("slow", seen))
        try:
            async with httpx.AsyncClient() as client:
                with pytest.raises(safe_fetch.FetchTimedOut) as slow:
                    await safe_fetch.fetch(f"http://127.0.0.1:{port}/robots.txt", client=client, timeout_s=0.3)
        finally:
            srv.shutdown()
        assert "did not answer within 0.3 seconds" in str(slow.value)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        async with httpx.AsyncClient() as client:
            with pytest.raises(safe_fetch.FetchRefused) as refused:
                await safe_fetch.fetch(f"http://127.0.0.1:{closed}/robots.txt", client=client, timeout_s=5.0)
        assert not isinstance(refused.value, safe_fetch.FetchTimedOut)
