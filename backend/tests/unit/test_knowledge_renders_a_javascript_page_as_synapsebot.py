"""Knowledge's headless render, for real (F1632 c): a page whose words only JavaScript writes is read in Chromium as
SynapseBot, never as a browser it is not, and never at an address the egress guard refuses. Without Chromium the
render answers None, and the crawl keeps the words that say the page needs JavaScript."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from core import egress_guard
from core.knowledge import fetch as F

PAGE = (b'<html><head><title>App</title></head><body><div id="root"></div>'
        b'<script>document.getElementById("root").textContent = "Words only JavaScript writes";</script>'
        b"</body></html>")


def _chromium() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


@pytest.fixture
async def site(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    egress_guard._resolve_cache.clear()
    seen: list[str] = []

    async def serve(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            seen.append(next((line.split(":", 1)[1].strip() for line in head.split("\r\n")
                              if line.lower().startswith("user-agent:")), ""))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         + f"Content-Length: {len(PAGE)}\r\n\r\n".encode() + PAGE)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    yield f"http://localhost:{server.sockets[0].getsockname()[1]}/app", seen
    server.close()
    from core.knowledge import render

    await render.close()


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
async def test_a_javascript_page_is_rendered_as_synapsebot(site, monkeypatch):
    from core.knowledge import render

    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    url, seen = site
    got = await render.render_page(url, user_agent=F.USER_AGENT)
    assert got is not None and "Words only JavaScript writes" in got.html
    assert got.url == url
    assert seen and set(seen) == {F.USER_AGENT}, seen  # never a browser's own user agent


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
async def test_an_address_the_egress_guard_refuses_is_never_rendered(site, monkeypatch):
    from core.knowledge import render

    monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
    url, seen = site
    assert await render.render_page(url, user_agent=F.USER_AGENT) is None
    assert seen == []


async def test_without_chromium_the_render_answers_none(monkeypatch):
    from core.knowledge import render

    monkeypatch.setattr(render, "_chromium_installed", lambda: False)
    assert await render.render_page("https://example.com/app", user_agent=F.USER_AGENT) is None
