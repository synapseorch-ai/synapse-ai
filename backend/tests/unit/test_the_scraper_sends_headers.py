"""The scraper's `headers` reach the site (F1123).

crawl4ai 0.9's `CrawlerRunConfig` has no `headers`, so every call that passed them failed with a TypeError before
the page was opened: `scrape_url`, `scrape_structured`, `crawl_multiple` and `scrape_with_session`. They are set on
the call's own page now, and sent to that page's own site only: an `Authorization` meant for one API is not handed
to every script and image host the page loads. One real browser, a loopback site that echoes what it was sent.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core import egress_guard
from tools import web_scraper

SECRET = "Bearer test-token-0123"


def _chromium() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


pytestmark = pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")


class Site:
    """Serves a page that shows the Authorization it was sent and loads an image from another host."""

    def __init__(self):
        self.other_seen: list[str] = []

    async def __aenter__(self):
        self.other = await asyncio.start_server(self._other, "127.0.0.1", 0)
        self.other_port = self.other.sockets[0].getsockname()[1]
        self.main = await asyncio.start_server(self._main, "127.0.0.1", 0)
        self.url = f"http://localhost:{self.main.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        for server in (self.main, self.other):
            server.close()

    @staticmethod
    def _auth(head: str) -> str:
        return next((line.split(":", 1)[1].strip() for line in head.split("\r\n")
                     if line.lower().startswith("authorization:")), "none")

    async def _main(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            body = (f"<html><body><div class=row><span class=auth>auth is [{self._auth(head)}]</span>"
                    f"<p>{'words ' * 20}</p></div>"
                    f"<img src='http://127.0.0.1:{self.other_port}/pixel.png'></body></html>").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _other(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            self.other_seen.append(self._auth(head))
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def site(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost,127.0.0.1")
    egress_guard._resolve_cache.clear()
    monkeypatch.setattr(web_scraper, "_crawler", None)
    async with Site() as s:
        yield s
    await web_scraper.close_crawler()


def _out(result) -> dict:
    return json.loads(result[0].text)


HEADERS = {"Authorization": SECRET}


async def test_scrape_url_sends_them_to_the_site_and_not_to_another_host(site):
    out = _out(await web_scraper._handle_scrape_url({"url": f"{site.url}/", "headers": HEADERS,
                                                      "js_code": "await new Promise(r => setTimeout(r, 500));"}))
    assert out["success"], out
    assert f"auth is [{SECRET}]" in out["markdown"]
    assert site.other_seen and SECRET not in site.other_seen


async def test_scrape_structured_sends_them(site):
    schema = {"baseSelector": "div.row", "fields": [{"name": "auth", "selector": "span.auth", "type": "text"}]}
    out = _out(await web_scraper._handle_scrape_structured({"url": f"{site.url}/", "schema": schema,
                                                             "headers": HEADERS}))
    assert out["success"], out
    assert out["data"] == [{"auth": f"auth is [{SECRET}]"}]


async def test_crawl_multiple_sends_them_to_every_page(site):
    out = _out(await web_scraper._handle_crawl_multiple({"urls": [f"{site.url}/a", f"{site.url}/b"],
                                                          "headers": HEADERS}))
    assert out["succeeded"] == 2, out
    assert all(f"auth is [{SECRET}]" in r["markdown"] for r in out["results"])


async def test_scrape_with_session_sends_them_at_every_step(site):
    out = _out(await web_scraper._handle_scrape_with_session({
        "session_id": "s", "headers": HEADERS, "steps": [{"url": f"{site.url}/1"}, {"url": f"{site.url}/2"}]}))
    assert out["success"], out
    assert all(f"auth is [{SECRET}]" in r["markdown"] for r in out["results"])


async def test_a_call_without_them_sends_none(site):
    out = _out(await web_scraper._handle_scrape_url({"url": f"{site.url}/"}))
    assert out["success"], out
    assert "auth is [none]" in out["markdown"]


async def test_the_egress_guard_still_sees_every_request_of_a_page_with_headers(site, monkeypatch):
    seen: list[str] = []
    guard = web_scraper._guard_route

    async def counting(route):
        seen.append(route.request.url)
        await guard(route)

    monkeypatch.setattr(web_scraper, "_guard_route", counting)
    out = _out(await web_scraper._handle_scrape_url({"url": f"{site.url}/", "headers": HEADERS,
                                                      "js_code": "await new Promise(r => setTimeout(r, 500));"}))
    assert out["success"], out
    assert any(u.startswith(site.url) for u in seen)
    assert any("/pixel.png" in u for u in seen)
