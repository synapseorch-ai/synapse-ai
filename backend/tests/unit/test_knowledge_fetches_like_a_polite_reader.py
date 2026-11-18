"""Knowledge's fetcher asks as a polite reader would, still as SynapseBot (F1632, request hygiene).

Some sites refuse a request that carries no Accept or Accept-Language header, or that drops the cookie the site set a
moment before, and some ask a reader to wait a few seconds with Retry-After. The fetcher sends the headers a reader
sends, keeps a site's cookies within one crawl, and waits once when asked to wait briefly, never long: a site asking for
more than half a minute is told so in the page's failure, and tried again at the next crawl. Its own name stays
SynapseBot throughout; nothing here passes for a browser.
"""
from __future__ import annotations

import http.server
import threading
import time

import pytest

from core import egress_guard
from core.knowledge import fetch as F

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
              "no_proxy")


@pytest.fixture(autouse=True)
def _public_is_127(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1")
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


class _Site(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []
    plan: dict[str, list[tuple[int, dict]]] = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
        if self.path == "/robots.txt":
            status, headers = 200, {"Set-Cookie": "visit=abc123; Path=/"}
            body = b"User-agent: *\nAllow: /\n"
        else:
            answers = type(self).plan.get(self.path) or [(200, {})]
            status, headers = answers.pop(0) if len(answers) > 1 else answers[0]
            body = b"<p>page</p>" if status == 200 else b""
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Site.seen, _Site.plan = [], {}
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _asked(path: str) -> list[dict]:
    return [s for s in _Site.seen if s["path"] == path]


async def test_a_page_is_asked_for_with_a_readers_headers_and_its_own_name(site):
    async with F.Fetcher(rps=100.0) as fetcher:
        await fetcher.get(f"{site}/page")
    [asked] = _asked("/page")
    assert "text/html" in asked.get("accept", "") and asked.get("accept-language"), asked
    assert asked["user-agent"] == F.USER_AGENT


async def test_a_cookie_the_site_sets_is_sent_back_within_the_crawl(site):
    async with F.Fetcher(rps=100.0) as fetcher:
        await fetcher.get(f"{site}/page")
    [asked] = _asked("/page")
    assert "visit=abc123" in asked.get("cookie", ""), asked


async def test_a_brief_retry_after_is_waited_out_once(site):
    _Site.plan["/busy"] = [(503, {"Retry-After": "1"}), (200, {})]
    started = time.monotonic()
    async with F.Fetcher(rps=100.0) as fetcher:
        got = await fetcher.get(f"{site}/busy")
    assert got.status == 200 and len(_asked("/busy")) == 2
    assert time.monotonic() - started >= 0.9


async def test_a_long_retry_after_is_not_waited_for(site):
    _Site.plan["/later"] = [(429, {"Retry-After": "120"})]
    started = time.monotonic()
    async with F.Fetcher(rps=100.0) as fetcher:
        got = await fetcher.get(f"{site}/later")
    assert got.status == 429 and len(_asked("/later")) == 1 and time.monotonic() - started < 5


async def test_a_refusal_without_retry_after_is_asked_once(site):
    _Site.plan["/down"] = [(503, {})]
    async with F.Fetcher(rps=100.0) as fetcher:
        got = await fetcher.get(f"{site}/down")
    assert got.status == 503 and len(_asked("/down")) == 1


async def test_a_second_refusal_after_the_wait_is_the_answer(site):
    _Site.plan["/still"] = [(503, {"Retry-After": "1"}), (503, {"Retry-After": "1"}), (200, {})]
    async with F.Fetcher(rps=100.0) as fetcher:
        got = await fetcher.get(f"{site}/still")
    assert got.status == 503 and len(_asked("/still")) == 2  # one retry, never a loop


@pytest.mark.parametrize("value,seconds", [("1", 1.0), ("30", 30.0), ("120", 120.0), ("soon", None), ("", None)])
def test_retry_after_is_read_as_seconds(value, seconds):
    assert F.retry_after_s({"retry-after": value}) == seconds
