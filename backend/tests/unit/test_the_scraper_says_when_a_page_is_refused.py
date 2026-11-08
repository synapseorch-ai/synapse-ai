"""A refused page is a failure, never a page of content (F1157).

crawl4ai failed a call whose page was a bot check or an HTTP 403/429/503, and the Playwright scraper must too: the
side-by-side (pro drafts/scrape/sxs) caught it returning WSJ's captcha and Hacker News' 503 as the sites' words, and
a reader given a block page as the site's words is misled. The egress proxy's own refusal says it is one.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import egress_guard
from tools import web_scraper


def _response(status=200, headers=None):
    return SimpleNamespace(status=status, headers=headers or {})


@pytest.mark.parametrize("status", [403, 429, 503])
def test_a_refusing_status_is_a_failure(status):
    assert web_scraper._refusal(_response(status), "<html><body>Sorry</body></html>") == (
        f"Blocked by anti-bot protection: HTTP {status}")


def test_a_bot_check_is_a_failure_whatever_its_status():
    html = '<html><body><script src="https://geo.captcha-delivery.com/captcha/"></script></body></html>'
    assert "DataDome" in web_scraper._refusal(_response(200), html)


def test_the_egress_proxys_refusal_says_it_is_one():
    refused = _response(403, {"x-synapse-egress": "refused"})
    assert web_scraper._refusal(refused, "<html></html>").startswith("URL blocked")


@pytest.mark.parametrize("status", [200, 404])
def test_a_page_is_its_content(status):
    assert web_scraper._refusal(_response(status), "<html><body><p>An article about captchas</p></body></html>") is None


# ── For real ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _chromium() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


@pytest.fixture
async def site(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    egress_guard._resolve_cache.clear()
    monkeypatch.setattr(web_scraper, "_crawler", None)
    pages = {"/busy": (503, b"<html><body><p>Service unavailable, try later</p></body></html>"),
             "/check": (200, b'<html><body><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/x"></script>'
                             b"<p>Just a moment</p></body></html>")}

    async def serve(reader, writer):
        try:
            path = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split(" ", 2)[1]
            status, body = pages[path]
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, KeyError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    yield f"http://localhost:{server.sockets[0].getsockname()[1]}"
    server.close()
    await web_scraper.close_crawler()


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
@pytest.mark.parametrize("path,why", [("/busy", "HTTP 503"), ("/check", "Cloudflare")])
async def test_a_refused_page_fails_the_call(site, path, why):
    out = json.loads((await web_scraper._handle_scrape_url({"url": site + path}))[0].text)
    assert out["success"] is False and why in out["error"], out
