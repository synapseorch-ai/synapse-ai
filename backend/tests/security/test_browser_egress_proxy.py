"""Every browser Synapse starts goes through one egress proxy (`core.egress_proxy`).

A browser's own network layer is where a page's requests are made — redirects,
scripts, in-page fetches, WebSockets, service workers — and a route hook inside
the browser does not see all of them. A proxy does: Chromium sends every request
to it, loopback included (Playwright adds `<-loopback>` to the bypass list), and
it opens no connection the egress policy (`core.egress_guard`) refuses.

It dials the address it checked, never the name again, so a name that answers
one thing to the check and another to the connect reaches only what was checked.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import egress_guard, egress_proxy


@pytest.fixture(autouse=True)
def _clean_guard_state(monkeypatch):
    monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


class Canary:
    """A loopback server that records every connection made to it."""

    def __init__(self, *, echo: bool = False):
        self.echo = echo
        self.connections = 0
        self.heads: list[bytes] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def __aenter__(self) -> "Canary":
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer) -> None:
        self.connections += 1
        try:
            if self.echo:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
                return
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def proxy():
    p = egress_proxy.EgressProxy()
    await p.start()
    yield p
    await p.close()


async def _ask(proxy, raw: bytes) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    """Send `raw` to the proxy; the response head, and the open stream."""
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    writer.write(raw)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return head, reader, writer


def _status(head: bytes) -> int:
    return int(head.split(b" ", 2)[1])


def _resolve_to(monkeypatch, table: dict[str, list[str]]):
    calls: list[str] = []

    async def fake_resolve(host):
        calls.append(host)
        if host not in table:
            raise egress_guard.UnresolvedHost(f"could not resolve {host}")
        return table[host]

    monkeypatch.setattr(egress_guard, "resolve", fake_resolve)
    return calls


# ── Refused: nothing is dialled ──────────────────────────────────────────────────────────────────────────────────

async def test_a_tunnel_to_loopback_is_refused_and_never_dialled(proxy):
    async with Canary() as canary:
        head, _r, w = await _ask(proxy, f"CONNECT 127.0.0.1:{canary.port} HTTP/1.1\r\n\r\n".encode())
        w.close()
        assert _status(head) == 403
        assert canary.connections == 0


async def test_a_plain_request_to_loopback_is_refused_and_never_dialled(proxy):
    async with Canary() as canary:
        raw = (f"GET http://127.0.0.1:{canary.port}/api/settings HTTP/1.1\r\n"
               f"Host: 127.0.0.1:{canary.port}\r\n\r\n").encode()
        head, _r, w = await _ask(proxy, raw)
        w.close()
        assert _status(head) == 403
        assert canary.connections == 0


@pytest.mark.parametrize("target", [
    "169.254.169.254:80",
    "169.254.170.2:80",
    "[::ffff:127.0.0.1]:8765",
    "[::1]:8765",
    "10.0.0.8:443",
    "192.168.1.1:80",
    "100.64.0.1:80",
    "0.0.0.0:8765",
])
async def test_a_tunnel_to_a_private_or_special_address_is_refused(proxy, target):
    head, _r, w = await _ask(proxy, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    w.close()
    assert _status(head) == 403


async def test_a_name_with_one_private_record_is_refused_without_naming_it(proxy, monkeypatch):
    _resolve_to(monkeypatch, {"mixed.test": ["93.184.216.34", "10.0.0.5"]})
    head, reader, w = await _ask(proxy, b"CONNECT mixed.test:443 HTTP/1.1\r\n\r\n")
    body = await asyncio.wait_for(reader.read(), 5)
    w.close()
    assert _status(head) == 403
    assert b"mixed.test" in head + body
    assert b"10.0.0.5" not in head + body  # the internal address is never read back


async def test_a_name_that_does_not_resolve_is_refused(proxy, monkeypatch):
    _resolve_to(monkeypatch, {})
    head, _r, w = await _ask(proxy, b"CONNECT nowhere.test:443 HTTP/1.1\r\n\r\n")
    w.close()
    assert _status(head) == 403


@pytest.mark.parametrize("raw", [
    b"GET ftp://example.com/ HTTP/1.1\r\n\r\n",
    b"GET file:///etc/passwd HTTP/1.1\r\n\r\n",
    b"GET /relative HTTP/1.1\r\nHost: example.com\r\n\r\n",
    b"CONNECT example.com HTTP/1.1\r\n\r\n",
    b"nonsense\r\n\r\n",
])
async def test_what_is_not_a_proxy_request_is_refused(proxy, raw):
    head, _r, w = await _ask(proxy, raw)
    w.close()
    assert _status(head) == 400


# ── Allowed: dialled at the address that was checked ─────────────────────────────────────────────────────────────

async def test_an_allowed_request_reaches_the_address_checked_in_origin_form(proxy, monkeypatch):
    """`site.test` resolves only through the guard's resolver, so reaching it at all proves the proxy dialled the
    address it checked rather than looking the name up again."""
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "site.test")
    calls = _resolve_to(monkeypatch, {"site.test": ["127.0.0.1"]})
    async with Canary() as canary:
        raw = (f"GET http://site.test:{canary.port}/page?q=1 HTTP/1.1\r\n"
               f"Host: site.test:{canary.port}\r\nProxy-Connection: keep-alive\r\n"
               f"Proxy-Authorization: Basic eDp5\r\nConnection: keep-alive\r\n\r\n").encode()
        head, reader, w = await _ask(proxy, raw)
        body = await asyncio.wait_for(reader.read(), 5)
        w.close()
        assert _status(head) == 200 and body == b"ok"
        assert calls == ["site.test"]
        sent = canary.heads[0].decode()
        assert sent.startswith("GET /page?q=1 HTTP/1.1\r\n")
        assert f"Host: site.test:{canary.port}" in sent
        assert "Proxy-" not in sent
        # One request per connection: a second one on it could name another host.
        assert "Connection: close" in sent and "keep-alive" not in sent


async def test_an_upgrade_keeps_its_connection_header(proxy, monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "site.test")
    _resolve_to(monkeypatch, {"site.test": ["127.0.0.1"]})
    async with Canary() as canary:
        raw = (f"GET http://site.test:{canary.port}/ws HTTP/1.1\r\nHost: site.test:{canary.port}\r\n"
               "Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n").encode()
        head, _r, w = await _ask(proxy, raw)
        w.close()
        sent = canary.heads[0].decode()
        assert "Connection: Upgrade" in sent and "Upgrade: websocket" in sent


async def test_an_allowed_tunnel_carries_bytes_both_ways(proxy, monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "site.test")
    _resolve_to(monkeypatch, {"site.test": ["127.0.0.1"]})
    async with Canary(echo=True) as canary:
        head, reader, writer = await _ask(proxy, f"CONNECT site.test:{canary.port} HTTP/1.1\r\n\r\n".encode())
        assert _status(head) == 200
        writer.write(b"hello through the tunnel")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(24), 5) == b"hello through the tunnel"
        writer.close()


async def test_the_proxy_listens_on_loopback_only(proxy):
    assert proxy.host == "127.0.0.1"
    assert proxy.url == f"http://127.0.0.1:{proxy.port}"


async def test_one_shared_proxy_per_event_loop():
    first = await egress_proxy.shared()
    assert await egress_proxy.shared() is first
    await first.close()
    again = await egress_proxy.shared()
    assert again is not first  # a closed one is replaced, not handed out
    await again.close()


# ── The scraper's own browser, for real ──────────────────────────────────────────────────────────────────────────
#
# Its route hook (`web_scraper._guard_route`) sees the page's requests but not its WebSockets. Through the proxy,
# nothing a page opens reaches a refused address. One Chromium, started and closed here.

def _chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        import os
        from pathlib import Path

        root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
        return any(p.name.startswith("chromium") for p in root.iterdir()) if root.is_dir() else False
    except Exception:
        return False


@pytest.fixture
async def scraper(monkeypatch):
    from tools import web_scraper

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_scraper, "_crawler", None)
    yield web_scraper
    if web_scraper._crawler is not None:  # 1.9.x has no close_crawler()
        await web_scraper._crawler.close()
        web_scraper._crawler = None


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
async def test_a_scraped_pages_websocket_and_fetch_never_reach_loopback(scraper, monkeypatch):
    # The page is served from `localhost`, which the allow-list lets through; what it opens is on 127.0.0.1, which
    # nothing does.
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    async with Canary(echo=True) as ws_canary, Canary() as fetch_canary:
        page = (f"<html><body><p id=done>{'words ' * 20}</p><script>"
                f"try {{ new WebSocket('ws://127.0.0.1:{ws_canary.port}/'); }} catch (e) {{}}"
                # A worker's own WebSocket, which no route in the browser sees (found reviewing BRW1's F1122).
                f"try {{ new Worker(URL.createObjectURL(new Blob(["
                f"\"new WebSocket('ws://127.0.0.1:{ws_canary.port}/worker')\"], {{type: 'text/javascript'}}))); }}"
                f" catch (e) {{}}"
                f"fetch('http://127.0.0.1:{fetch_canary.port}/').catch(() => {{}});"
                f"</script></body></html>").encode()

        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                             + f"Content-Length: {len(page)}\r\n\r\n".encode() + page)
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        site = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = site.sockets[0].getsockname()[1]
        try:
            result = json.loads((await scraper._handle_scrape_url({
                "url": f"http://localhost:{port}/",
                "js_code": "await new Promise(r => setTimeout(r, 1500));",
            }))[0].text)
            await asyncio.sleep(0.5)
        finally:
            site.close()
        assert result["success"], result  # the page itself was read
        assert ws_canary.connections == 0
        assert fetch_canary.connections == 0
