"""The browser component against a real Chromium (BRW1, D328).

Starts `core.browser.host` as its own process, as the backend does, and drives it
through its routes against a page served here. What it proves needs a real
browser: the network gate stops a send that Playwright MCP's own click triggered,
a private address is refused even when the agent navigates to it, every action is
logged with a screenshot, and the person's view streams frames. Skipped where
Chromium or npx is missing.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web


def _chromium_installed() -> bool:
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
        root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache/ms-playwright")
        return any(root.glob("chromium-*"))
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not (shutil.which("npx") and _chromium_installed()),
                                reason="needs Chromium (playwright install chromium) and npx")

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
async def site():
    hits = {"submit": 0, "ws": 0}

    async def index(_):
        return web.Response(content_type="text/html", text=(
            "<html><body><h1>Fixture</h1><form method='post' action='/submit'>"
            "<input name='q' aria-label='Query'><button type='submit'>Send it</button></form>"
            "<a href='/two'>Page two</a></body></html>"))

    async def submit(_):
        hits["submit"] += 1
        return web.Response(text="submitted")

    async def live(request):
        port = request.url.port
        return web.Response(content_type="text/html", text=(
            "<html><body><p id='out'>waiting</p><script>"
            "const seen = []; const show = () => { document.getElementById('out').textContent = seen.join(' '); };"
            f"for (const [name, url] of [['inside', 'ws://127.0.0.1:{port}/ws'], ['public', 'ws://localhost:{port}/ws']]) {{"
            "  const ws = new WebSocket(url);"
            "  ws.onopen = () => ws.send('hi');"
            "  ws.onmessage = (e) => { seen.push(name + '-got-' + e.data); show(); };"
            "  ws.onclose = () => { seen.push(name + '-closed'); show(); };"
            "}</script></body></html>"))

    async def socket(request):
        hits["ws"] += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str("echo-" + msg.data)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/submit", submit)
    app.router.add_get("/live", live)
    app.router.add_get("/ws", socket)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    port = tcp._server.sockets[0].getsockname()[1]
    yield port, hits
    await runner.cleanup()


@pytest.fixture
async def host():
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "SYNAPSE_BROWSER_TOKEN": token, "EGRESS_ALLOW_HOSTS": "localhost",
           "PYTHONPATH": str(BACKEND)}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "core.browser.host", cwd=str(BACKEND),
                                                env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    line = (await asyncio.wait_for(proc.stdout.readline(), 60)).decode().split()
    assert line[0] == "BROWSER_HOST_READY"
    http = aiohttp.ClientSession(headers={"X-Browser-Token": token})
    yield http, int(line[1]), int(line[2])
    await http.close()
    proc.terminate()
    await proc.wait()


async def _post(http, port, path, body):
    async with http.post(f"http://127.0.0.1:{port}{path}", json=body) as resp:
        return await resp.json()


async def test_the_component_reads_but_never_sends_and_never_reaches_inside(site, host):
    port, hits = site
    http, control, viewer = host

    async with aiohttp.ClientSession() as anon:
        async with anon.post(f"http://127.0.0.1:{control}/session", json={}) as resp:
            assert resp.status == 401  # no token, no browser

    sid = (await _post(http, control, "/session", {"owner": "chat:e2e", "mode": "read_only"}))["session_id"]
    nav = await _post(http, control, "/tool", {"session_id": sid, "name": "browser_navigate",
                                               "args": {"url": f"http://localhost:{port}/"}})
    assert not nav["is_error"] and 'button "Send it"' in nav["text"]  # the snapshot comes back inline

    refused = await _post(http, control, "/tool", {"session_id": sid, "name": "browser_evaluate",
                                                   "args": {"function": "() => document.cookie"}})
    assert refused == {"refused": "browser_evaluate is not available in Synapse's browser."}

    ref = re.search(r'button "Send it" \[ref=(e\d+)\]', nav["text"]).group(1)
    click = await _post(http, control, "/tool", {"session_id": sid, "name": "browser_click",
                                                 "args": {"element": "Send it button", "target": ref}})
    assert hits["submit"] == 0, "a read-only browser sent a form"
    assert f"a POST to localhost: this browser is read-only" in click["text"]

    inside = await _post(http, control, "/tool", {"session_id": sid, "name": "browser_navigate",
                                                  "args": {"url": f"http://127.0.0.1:{port}/"}})
    assert inside["is_error"] and "ERR_BLOCKED_BY_CLIENT" in inside["text"]

    async with http.get(f"http://127.0.0.1:{control}/log", params={"session_id": sid}) as resp:
        entries = (await resp.json())["entries"]
    agent = [e for e in entries if e["actor"] == "agent"]
    assert [e["action"] for e in agent] == ["browser_navigate", "browser_click", "browser_navigate"]
    assert all(e["shot_ref"] for e in agent)
    assert any(e["actor"] == "guard" and "127.0.0.1" in e["target"] for e in entries)
    async with http.get(f"http://127.0.0.1:{control}/shot",
                        params={"session_id": sid, "ref": agent[0]["shot_ref"]}) as resp:
        assert resp.content_type == "image/jpeg" and (await resp.read())[:2] == b"\xff\xd8"

    frames = 0
    try:
        async with http.get(f"http://127.0.0.1:{viewer}/frames", params={"session_id": sid},
                            timeout=aiohttp.ClientTimeout(total=3)) as resp:
            async for chunk in resp.content:
                frames += chunk.startswith(b"data:")
    except asyncio.TimeoutError:
        pass
    assert frames >= 1

    async with http.post(f"http://127.0.0.1:{viewer}/input", json={"type": "mouse"}) as resp:
        assert resp.status == 409  # v1: the person watches

    closed = await _post(http, control, "/close", {"session_id": sid})
    assert closed["closed"] is True
    async with http.get(f"http://127.0.0.1:{control}/log", params={"session_id": sid}) as resp:
        assert resp.status == 404


async def test_a_page_s_socket_passes_the_same_guard(site, host):
    """F1122: a WebSocket never passes the request route. A read-only browser opens none; co-drive opens one to a
    public address and its messages flow, and none reaches a private address either way."""
    port, hits = site
    http, control, _viewer = host

    async def opened(mode: str, wait_for: str) -> str:
        sid = (await _post(http, control, "/session", {"owner": f"chat:ws-{mode}", "mode": mode}))["session_id"]
        await _post(http, control, "/tool", {"session_id": sid, "name": "browser_navigate",
                                             "args": {"url": f"http://localhost:{port}/live"}})
        await _post(http, control, "/tool", {"session_id": sid, "name": "browser_wait_for",
                                             "args": {"text": wait_for}})
        snap = await _post(http, control, "/tool", {"session_id": sid, "name": "browser_snapshot", "args": {}})
        async with http.get(f"http://127.0.0.1:{control}/log", params={"session_id": sid}) as resp:
            guard = [e["target"] for e in (await resp.json())["entries"] if e["actor"] == "guard"]
        await _post(http, control, "/close", {"session_id": sid})
        return snap["text"], guard

    text, guard = await opened("read_only", "public-closed")
    assert hits["ws"] == 0, "a read-only browser opened a socket"
    assert "inside-got" not in text and "public-got" not in text
    assert any("this browser is read-only" in g for g in guard)
    assert any("127.0.0.1 is not a public address" in g for g in guard)

    text, guard = await opened("co_drive", "public-got-echo-hi")
    assert hits["ws"] == 1, "only the public socket reached the server"
    assert "inside-got" not in text and "inside-closed" in text
    assert any("127.0.0.1 is not a public address" in g for g in guard)
