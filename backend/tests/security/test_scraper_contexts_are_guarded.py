"""Every page the scraper opens goes through the egress proxy and the route guard (F1157).

The scraper drives Playwright itself now, not through crawl4ai: it makes a context per caller and a page per run.
SEC2's tests (test_scraper_egress, test_scraper_sessions_are_scoped, test_browser_egress_proxy) hold the guard's
policy, the caller's own context and the proxy's refusals. These hold that every context it opens, for a named
session or for one call, carries both, and that a page's requests really leave through the proxy. A PDF the address
downloads is read through the same two, under the size limit.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core import egress_guard
from tools import web_scraper


def _chromium() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


real_chromium = pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")


def _out(result) -> dict:
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def _clean_guard_state(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


# ── Every context: the proxy, with the caller's label, and the guard on its route ───────────────────────────────

class _Page:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class _Context:
    def __init__(self, options):
        self.options = options
        self.routes: list = []
        self.closed = False

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def add_init_script(self, script):
        pass

    async def new_page(self):
        return _Page()

    async def close(self):
        self.closed = True


class _Chromium:
    def __init__(self):
        self.contexts: list[_Context] = []

    async def new_context(self, **options):
        self.contexts.append(_Context(options))
        return self.contexts[-1]


@pytest.fixture
def browser(monkeypatch):
    """The scraper's own `_Browser` over a Chromium that records the contexts it is asked for; each run's page read
    is a stub, so no page is loaded."""
    chromium = _Chromium()
    crawler = web_scraper._Browser(web_scraper.browser_config("http://127.0.0.1:4750"))
    crawler._browser = chromium

    async def get_crawler():
        return crawler

    async def read(page, url, config):
        return web_scraper.CrawlResult(success=True, markdown="words " * 20)

    monkeypatch.setattr(web_scraper, "get_crawler", get_crawler)
    monkeypatch.setattr(web_scraper, "_read_page", read)
    monkeypatch.setattr(web_scraper, "_browser_proxy", "http://127.0.0.1:4750")
    return chromium


async def test_a_call_with_no_session_gets_a_context_through_the_proxy_with_the_guard(browser):
    await web_scraper._handle_scrape_url({"url": "https://93.184.216.34/"})
    (context,) = browser.contexts
    proxy = context.options["proxy"]
    assert proxy["server"] == "http://127.0.0.1:4750" and proxy["username"].startswith("synapse-")
    assert ("**/*", web_scraper._guard_route) in context.routes
    assert context.closed, "a context of one call is closed when the call ends"


async def test_a_named_sessions_context_has_both_too(browser):
    await web_scraper._handle_scrape_with_session({"session_id": "s", "steps": [{"url": "https://93.184.216.34/"}]})
    (context,) = browser.contexts
    assert context.options["proxy"]["server"] == "http://127.0.0.1:4750"
    assert ("**/*", web_scraper._guard_route) in context.routes


async def test_two_callers_never_share_a_context(browser):
    from core import tool_scope
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(web_scraper.app) as client:
        for owner in ("chat-a", "chat-b"):
            await client.call_tool("scrape_with_session", {"session_id": "s", "steps": [{"url": "https://93.184.216.34/"}]},
                                   meta=tool_scope.call_meta("web_scraper", owner))
    a, b = browser.contexts
    assert a is not b and a.options["proxy"]["username"] != b.options["proxy"]["username"]
    assert all(("**/*", web_scraper._guard_route) in c.routes for c in (a, b))


async def test_no_service_worker_runs_where_the_route_cannot_see_it(browser):
    await web_scraper._handle_scrape_url({"url": "https://93.184.216.34/"})
    assert browser.contexts[0].options["service_workers"] == "block"


# ── For real: the page's request leaves through the proxy ────────────────────────────────────────────────────────

class _RecordingProxy:
    """An HTTP proxy on loopback that answers every request itself and records what it was asked for."""

    def __init__(self):
        self.asked: list[str] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def _serve(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            self.asked.append(head.split("\r\n", 1)[0])
            body = f"<html><body><p>answered by the proxy {'words ' * 20}</p></body></html>".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def real_scraper(monkeypatch):
    monkeypatch.setattr(web_scraper, "_crawler", None)
    yield web_scraper
    await web_scraper.close_crawler()


@real_chromium
async def test_a_pages_request_leaves_through_the_hosts_proxy(real_scraper, monkeypatch):
    # `site.invalid` resolves nowhere: the page can only come back if the browser asked the proxy for it. The proxy
    # is handed to the scraper, not put in the environment, which Chromium on Linux reads by itself: the page reaches
    # the proxy only if the scraper's own wiring sends it there (reading it from the environment is SEC2's tests').
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "site.invalid")
    async with _RecordingProxy() as proxy:
        monkeypatch.setattr(web_scraper, "_egress_proxy", lambda: proxy.url)
        out = _out(await real_scraper._handle_scrape_url({"url": "http://site.invalid/page"}))
    assert out["success"] and "answered by the proxy" in out["markdown"], out
    assert "GET http://site.invalid/page HTTP/1.1" in proxy.asked


# ── A PDF the address downloads ──────────────────────────────────────────────────────────────────────────────────

def _pdf(text: str) -> bytes:
    """A one-page PDF that says `text`."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


class _FileSite:
    def __init__(self, body: bytes, content_type: str):
        self.body, self.content_type = body, content_type
        self.served = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://localhost:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def _serve(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.served += 1
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {self.content_type}\r\nConnection: close\r\n"
                         f"Content-Length: {len(self.body)}\r\n\r\n".encode() + self.body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@real_chromium
async def test_a_pdf_is_read(real_scraper, monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    async with _FileSite(_pdf("Quarterly revenue grew"), "application/pdf") as site:
        out = _out(await real_scraper._handle_scrape_url({"url": f"{site.url}/report.pdf"}))
    assert out["success"] and "Quarterly revenue grew" in out["markdown"], out


@real_chromium
async def test_a_pdf_over_the_limit_is_not_read(real_scraper, monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    monkeypatch.setattr(web_scraper, "MAX_PDF_BYTES", 100)
    async with _FileSite(_pdf("Quarterly revenue grew"), "application/pdf") as site:
        out = _out(await real_scraper._handle_scrape_url({"url": f"{site.url}/report.pdf"}))
    assert not out["success"] and "MB" in out["error"], out


@real_chromium
async def test_a_file_that_is_not_a_pdf_is_not_read(real_scraper, monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    async with _FileSite(b"PK\x03\x04 not a page", "application/zip") as site:
        out = _out(await real_scraper._handle_scrape_url({"url": f"{site.url}/archive.zip"}))
    assert not out["success"] and "not a page" in out["error"], out


@real_chromium
async def test_a_pdf_the_guard_refuses_is_never_fetched(real_scraper, monkeypatch):
    # localhost is not allowed here: the guard refuses the download's request before the site is reached.
    async with _FileSite(_pdf("Quarterly revenue grew"), "application/pdf") as site:
        monkeypatch.setattr(web_scraper, "_preflight", _no_preflight)
        out = _out(await real_scraper._handle_scrape_url({"url": f"{site.url}/report.pdf"}))
        assert site.served == 0
    assert not out["success"], out


async def _no_preflight(url):
    """The handler's own check off, so the browser's route guard is the one that must refuse."""
    return None
