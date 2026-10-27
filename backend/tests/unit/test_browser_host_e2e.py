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
    hits = {"submit": 0, "ws": 0, "spawn": 0}

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

    async def workers(request):
        port = request.url.port
        return web.Response(content_type="text/html", text=(
            "<html><body><p id='out'>waiting</p><script>"
            "const src = (u) => URL.createObjectURL(new Blob([`new WebSocket('${u}')`]));"
            f"const inside = 'ws://127.0.0.1:{port}/ws', outside = 'ws://localhost:{port}/ws';"
            "let made = 0;"
            "for (const u of [inside, outside]) { try { new Worker(src(u)); made++; } catch (e) {} }"
            "try { new SharedWorker(src(inside)); made++; } catch (e) {}"
            # A fresh frame's own globals, used before anything could load in it.
            "const f = document.createElement('iframe'); document.body.append(f);"
            "try { new f.contentWindow.Worker(src(inside)); made++; } catch (e) {}"
            "try { const w = window.open(''); new w.Worker(src(inside)); made++; } catch (e) {}"
            "try { const w = f.contentWindow.open(''); new w.Worker(src(inside)); made++; } catch (e) {}"
            # A popup that loads a page of its own, which starts its own worker.
            "window.open('/spawn');"
            "document.getElementById('out').textContent = 'made-' + made;"
            "</script></body></html>"))

    async def spawn(request):
        hits["spawn"] += 1
        port = request.url.port
        return web.Response(content_type="text/html", text=(
            "<html><body><script>"
            f"try {{ new Worker(URL.createObjectURL(new Blob([\"new WebSocket('ws://localhost:{port}/ws')\"]))); }}"
            " catch (e) {}</script></body></html>"))

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
    app.router.add_get("/workers", workers)
    app.router.add_get("/spawn", spawn)
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

    async with http.post(f"http://127.0.0.1:{viewer}/input", params={"session_id": sid},
                         json={"type": "mouse"}) as resp:
        assert resp.status == 409  # the agent holds the wheel: the person's input waits for Take over

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
        # v2: while the agent drives, a socket opens only on its sites (the next test).
        sid = (await _post(http, control, "/session", {"owner": f"chat:ws-{mode}", "mode": mode,
                                                       "sites": ["localhost"]}))["session_id"]
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


async def test_a_read_only_page_starts_no_worker(site, host):
    """F1122: a worker's own WebSocket passes no socket route, so a read-only browser, which opens no socket at all,
    starts no worker either (SEC2's review): not from the page, a fresh frame's own globals, or a popup it opened.
    Eight loads in one warm browser: a popup is a new target, and Playwright MCP, a second client of the same
    Chromium, may let it run before the guard reaches it (found by BRW2), so the page gets no handle on one."""
    port, hits = site
    http, control, _viewer = host
    sid = (await _post(http, control, "/session", {"owner": "chat:workers", "mode": "read_only"}))["session_id"]
    for _ in range(8):
        await _post(http, control, "/tool", {"session_id": sid, "name": "browser_navigate",
                                             "args": {"url": f"http://localhost:{port}/workers"}})
        await _post(http, control, "/tool", {"session_id": sid, "name": "browser_wait_for",
                                             "args": {"text": "made-"}})
        snap = await _post(http, control, "/tool", {"session_id": sid, "name": "browser_snapshot", "args": {}})
        assert "made-0" in snap["text"], snap["text"]
    await asyncio.sleep(1.5)  # time for any socket a worker opened to arrive
    assert hits["spawn"] >= 1, "the popup's own page loaded"
    assert hits["ws"] == 0, "a worker opened a socket from a read-only browser"
    await _post(http, control, "/close", {"session_id": sid})


# ── v2: co-driving (F1096, F1097, F1098, F1101) ──────────────────────────────

@pytest.fixture
async def form_site():
    hits = {"submit": 0, "read": 0}

    async def index(request):
        port = request.url.port
        return web.Response(content_type="text/html", text=(
            "<html><body><h1>Form</h1><form method='post' action='/submit'>"
            "<input name='q' aria-label='Query'><input type='password' name='pw' aria-label='Password'>"
            "<button type='submit'>Send it</button></form>"
            f"<a href='http://other.localhost:{port}/'>Elsewhere</a></body></html>"))

    async def submit(_):
        hits["submit"] += 1
        return web.Response(content_type="text/html", text="<h1>Sent</h1>")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/submit", submit)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    yield tcp._server.sockets[0].getsockname()[1], hits
    await runner.cleanup()


@pytest.fixture
async def codrive_host():
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "SYNAPSE_BROWSER_TOKEN": token, "EGRESS_ALLOW_HOSTS": "localhost,other.localhost",
           "PYTHONPATH": str(BACKEND)}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "core.browser.host", cwd=str(BACKEND),
                                                env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    line = (await asyncio.wait_for(proc.stdout.readline(), 60)).decode().split()
    http = aiohttp.ClientSession(headers={"X-Browser-Token": token})
    yield http, int(line[1]), int(line[2])
    await http.close()
    proc.terminate()
    await proc.wait()


async def _tool(http, control, sid, name, args, press=None):
    return await _post(http, control, "/tool", {"session_id": sid, "name": name, "args": args, "press": press})


#: The engine's word that the agent quoted the person asking for this press (F1124).
ASKED = {"because": "send it for me please", "confirmed": True}


async def _view(http, viewer, sid, path, body=None):
    async with http.post(f"http://127.0.0.1:{viewer}{path}", params={"session_id": sid}, json=body or {}) as r:
        return r.status, await r.json()


async def _state(http, control, sid):
    async with http.get(f"http://127.0.0.1:{control}/state", params={"session_id": sid}) as r:
        return await r.json()


def _ref(text, role, name):
    return re.search(rf'{role} "{name}" \[ref=(\w+)\]', text).group(1)  # e5, or f2e5 after a reload


async def test_an_agents_send_waits_for_the_person(form_site, codrive_host):
    port, hits = form_site
    http, control, viewer = codrive_host
    sid = (await _post(http, control, "/session", {"owner": "chat:v2", "mode": "co_drive",
                                                   "sites": ["localhost"]}))["session_id"]
    page = await _tool(http, control, sid, "browser_navigate", {"url": f"http://localhost:{port}/"})
    typed = await _tool(http, control, sid, "browser_type",
                        {"element": "Query", "target": _ref(page["text"], "textbox", "Query"), "text": "hello"})
    assert not typed.get("handover"), typed  # a listed site takes typing

    pw = await _tool(http, control, sid, "browser_type",
                     {"element": "Password", "target": _ref(page["text"], "textbox", "Password"), "text": "x"})
    assert pw["handover"]["kind"] == "sensitive" and pw["is_error"]
    assert (await _state(http, control, sid))["wheel"] == "person"
    await _view(http, viewer, sid, "/wheel/hand-back", {})

    click = await _tool(http, control, sid, "browser_click",
                        {"element": "Send it", "target": _ref(page["text"], "button", "Send it")}, ASKED)
    assert click["handover"]["kind"] == "send", click
    state = await _state(http, control, sid)
    assert state["wheel"] == "nobody" and state["held"]["method"] == "POST" and hits["submit"] == 0

    status, _ = await _view(http, viewer, sid, "/release", {"allow_address": False})
    assert status == 200
    for _ in range(50):
        if hits["submit"]:
            break
        await asyncio.sleep(0.1)
    assert hits["submit"] == 1, "a released send goes"
    assert (await _state(http, control, sid))["wheel"] == "agent"
    status, _ = await _view(http, viewer, sid, "/release", {})
    assert status == 409  # nothing held now

    again = await _tool(http, control, sid, "browser_navigate", {"url": f"http://localhost:{port}/"})
    assert "### The person" in again["text"] and "only loaded data" in again["text"]


async def test_taking_over_drops_the_agents_send_and_the_person_sends_it(form_site, codrive_host):
    port, hits = form_site
    http, control, viewer = codrive_host
    sid = (await _post(http, control, "/session", {"owner": "chat:v2b", "mode": "co_drive",
                                                   "sites": ["localhost"]}))["session_id"]
    page = await _tool(http, control, sid, "browser_navigate", {"url": f"http://localhost:{port}/"})
    query = _ref(page["text"], "textbox", "Query")
    await _tool(http, control, sid, "browser_type", {"element": "Query", "target": query, "text": "hi"})
    status, _ = await _view(http, viewer, sid, "/input", {"type": "text", "text": "x"})
    assert status == 409  # the agent holds the wheel
    click = await _tool(http, control, sid, "browser_click",
                        {"element": "Send it", "target": _ref(page["text"], "button", "Send it")}, ASKED)
    assert click["handover"]["kind"] == "send"

    status, body = await _view(http, viewer, sid, "/wheel/take")
    assert status == 200 and body["wheel"] == "person"
    await asyncio.sleep(0.5)
    assert hits["submit"] == 0, "taking over drops the agent's send"
    waiting = await _tool(http, control, sid, "browser_snapshot", {})
    assert waiting == {"waiting": "person_driving"}

    await _view(http, viewer, sid, "/input", {"type": "nav", "url": f"http://localhost:{port}/"})
    await asyncio.sleep(0.5)
    status, _ = await _view(http, viewer, sid, "/input", {"type": "mouse", "event": "click", "x": 60, "y": 75})
    assert status == 200
    await _view(http, viewer, sid, "/input", {"type": "text", "text": "mine"})
    await _view(http, viewer, sid, "/input", {"type": "key", "key": "Enter"})
    for _ in range(50):
        if hits["submit"]:
            break
        await asyncio.sleep(0.1)
    assert hits["submit"] == 1, "the person's own send goes"

    await _view(http, viewer, sid, "/wheel/hand-back", {"note": "I sent it"})
    after = await _tool(http, control, sid, "browser_snapshot", {})
    assert 'said: "I sent it"' in after["text"]


async def test_typing_on_an_unlisted_site_asks_first_and_scope_holds_redirects_too(form_site, codrive_host):
    port, _ = form_site
    http, control, viewer = codrive_host
    open_sid = (await _post(http, control, "/session", {"owner": "chat:v2c", "mode": "co_drive"}))["session_id"]
    page = await _tool(http, control, open_sid, "browser_navigate", {"url": f"http://localhost:{port}/"})
    ask = await _tool(http, control, open_sid, "browser_type",
                      {"element": "Query", "target": _ref(page["text"], "textbox", "Query"), "text": "hi"})
    assert ask["handover"]["kind"] == "allow_typing" and ask["handover"]["host"] == "localhost"
    await _view(http, viewer, open_sid, "/allow-site", {"host": "localhost"})
    typed = await _tool(http, control, open_sid, "browser_type",
                        {"element": "Query", "target": _ref(page["text"], "textbox", "Query"), "text": "hi"})
    assert not typed.get("handover") and not typed["is_error"]

    scoped = (await _post(http, control, "/session", {"owner": "run:v2d", "mode": "co_drive",
                                                      "sites": ["localhost"]}))["session_id"]
    off = await _tool(http, control, scoped, "browser_navigate", {"url": f"http://other.localhost:{port}/"})
    assert off["is_error"] and "ERR_BLOCKED_BY_CLIENT" in off["text"]
    assert "other.localhost is not one of this agent's sites" in off["text"]


async def test_a_saved_login_reaches_the_site_and_comes_back_at_close(codrive_host):
    seen = {}

    async def whoami(request):
        seen["cookie"] = request.headers.get("Cookie", "")
        return web.Response(content_type="text/html", text="<h1>Account</h1>")

    app = web.Application()
    app.router.add_get("/", whoami)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    port = tcp._server.sockets[0].getsockname()[1]
    http, control, _ = codrive_host
    try:
        state = {"cookies": [{"name": "sid", "value": "abc123", "domain": "localhost", "path": "/",
                              "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"}], "origins": []}
        sid = (await _post(http, control, "/session", {"owner": "run:login", "mode": "read_only",
                                                       "login_state": state}))["session_id"]
        await _tool(http, control, sid, "browser_navigate", {"url": f"http://localhost:{port}/"})
        assert "sid=abc123" in seen["cookie"]
        closed = await _post(http, control, "/close", {"session_id": sid, "keep_login": True})
        assert any(c["name"] == "sid" and c["value"] == "abc123" for c in closed["login_state"]["cookies"])
    finally:
        await runner.cleanup()


async def test_the_pane_draws_the_popups_the_screencast_cannot(codrive_host):
    picked = {}

    async def page(_):
        return web.Response(content_type="text/html", text=(
            "<html><body style='margin:0'>"
            "<select id='s' aria-label='Size' style='position:absolute;left:20px;top:20px;width:200px;height:30px'>"
            "<option value='s'>Small</option><option value='l'>Large</option></select>"
            "<button onclick=\"alert('Saved!')\" style='position:absolute;left:20px;top:80px;width:120px;"
            "height:30px'>Save</button>"
            "<input id='f' type='file' accept='.pdf' style='position:absolute;left:20px;top:140px;width:200px;"
            "height:30px'></body></html>"))

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    port = tcp._server.sockets[0].getsockname()[1]
    http, control, viewer = codrive_host
    try:
        sid = (await _post(http, control, "/session", {"owner": "login:p", "mode": "co_drive",
                                                       "person_only": True}))["session_id"]
        await _view(http, viewer, sid, "/input", {"type": "nav", "url": f"http://localhost:{port}/"})
        await asyncio.sleep(0.8)

        status, body = await _view(http, viewer, sid, "/input", {"type": "mouse", "event": "click", "x": 100, "y": 35})
        assert status == 200 and [o["label"] for o in body["select"]["options"]] == ["Small", "Large"]
        status, _ = await _view(http, viewer, sid, "/select", {"target": body["select"]["target"], "value": "l"})
        assert status == 200 and "select" not in await _state(http, control, sid)

        await _view(http, viewer, sid, "/input", {"type": "mouse", "event": "click", "x": 60, "y": 95})
        for _ in range(30):
            state = await _state(http, control, sid)
            if state.get("dialog"):
                break
            await asyncio.sleep(0.1)
        assert state["dialog"] == {"type": "alert", "message": "Saved!", "default": None}
        status, _ = await _view(http, viewer, sid, "/dialog", {"accept": True})
        assert status == 200 and "dialog" not in await _state(http, control, sid)

        await _view(http, viewer, sid, "/input", {"type": "mouse", "event": "click", "x": 100, "y": 155})
        for _ in range(30):
            state = await _state(http, control, sid)
            if state.get("file"):
                break
            await asyncio.sleep(0.1)
        assert state["file"] == {"multiple": False, "accept": ".pdf"}
        import base64
        status, _ = await _view(http, viewer, sid, "/file", {"name": "invoice.pdf",
                                                              "b64": base64.b64encode(b"%PDF-1.4").decode()})
        assert status == 200 and "file" not in await _state(http, control, sid)
        status, _ = await _view(http, viewer, sid, "/dialog", {"accept": True})
        assert status == 409  # nothing open
    finally:
        await runner.cleanup()


async def test_while_the_agent_drives_a_socket_opens_only_on_its_sites(site, host):
    """F1122 in co-drive: with no listed site, the agent's page opens no socket until the person allows the site;
    while the person drives, a socket opens to any public address (V2-CONTRACT, Naveen 2026-10-28)."""
    port, hits = site
    http, control, viewer = host
    sid = (await _post(http, control, "/session", {"owner": "chat:ws-scope", "mode": "co_drive"}))["session_id"]

    async def agent_opens(wait_for: str) -> list[str]:
        await _post(http, control, "/tool", {"session_id": sid, "name": "browser_navigate",
                                             "args": {"url": f"http://localhost:{port}/live"}})
        await _post(http, control, "/tool", {"session_id": sid, "name": "browser_wait_for",
                                             "args": {"text": wait_for}})
        async with http.get(f"http://127.0.0.1:{control}/log", params={"session_id": sid}) as resp:
            return [e["target"] for e in (await resp.json())["entries"] if e["actor"] == "guard"]

    guard = await agent_opens("public-closed")
    assert hits["ws"] == 0, "the agent's page opened a socket off its sites"
    assert any("a WebSocket to localhost: not one of the agent's sites" in g for g in guard)

    await _view(http, viewer, sid, "/allow-site", {"host": "localhost"})
    await agent_opens("public-got-echo-hi")
    assert hits["ws"] == 1

    status, _ = await _view(http, viewer, sid, "/wheel/take")
    assert status == 200
    await _view(http, viewer, sid, "/input", {"type": "nav", "url": f"http://localhost:{port}/live"})
    for _ in range(50):
        if hits["ws"] == 2:
            break
        await asyncio.sleep(0.1)
    assert hits["ws"] == 2, "the person's page opens its socket"
    await _post(http, control, "/close", {"session_id": sid})


# ── final buttons: the click stop (F1124; Naveen 2026-10-28) ──────────────────

@pytest.fixture
async def press_site():
    hits = {"feedback": 0, "search": 0, "pay": 0, "message": 0}

    async def index(_):
        return web.Response(content_type="text/html", text=(
            "<html><body><h1>Shop</h1>"
            "<form method='get' action='/search'><input name='q' aria-label='Find'><button>Search</button></form>"
            "<form method='post' action='/feedback'><input name='note' aria-label='Note'>"
            "<button type='submit'>Submit feedback</button></form>"
            "<button onclick=\"fetch('/pay', {method: 'POST'})\">Pay now</button>"
            "<textarea aria-label='Message' onkeydown=\"if (event.key === 'Enter') "
            "fetch('/message', {method: 'POST', body: this.value})\"></textarea></body></html>"))

    def counter(name):
        async def count(_):
            hits[name] += 1
            return web.Response(content_type="text/html", text=f"<h1>{name} done</h1>")
        return count

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/search", counter("search"))
    for name in ("feedback", "pay", "message"):
        app.router.add_post(f"/{name}", counter(name))
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    yield tcp._server.sockets[0].getsockname()[1], hits
    await runner.cleanup()


async def test_a_press_that_acts_for_the_person_runs_only_on_their_words(press_site, codrive_host):
    port, hits = press_site
    http, control, viewer = codrive_host
    sid = (await _post(http, control, "/session", {"owner": "chat:press", "mode": "co_drive",
                                                   "sites": ["localhost"]}))["session_id"]

    async def page():
        return (await _tool(http, control, sid, "browser_navigate", {"url": f"http://localhost:{port}/"}))["text"]

    text = await page()
    search = await _tool(http, control, sid, "browser_click", {"element": "Search", "target": _ref(text, "button", "Search")})
    assert not search.get("handover") and hits["search"] == 1, "a search is pressed freely"

    text = await page()
    submit = {"element": "Submit feedback", "target": _ref(text, "button", "Submit feedback")}
    told = await _tool(http, control, sid, "browser_click", submit)
    assert told["text"].startswith("Not pressed: 'Submit feedback': it submits the form on localhost.")
    assert not told.get("handover") and hits["feedback"] == 0
    assert (await _state(http, control, sid))["wheel"] == "agent", "the agent decides; nobody is asked yet"

    wrong = await _tool(http, control, sid, "browser_click", submit, {"because": "go on", "confirmed": False})
    assert wrong["handover"]["kind"] == "press" and wrong["handover"]["press"]["tier"] == "acts"
    assert wrong["handover"]["press"]["label"] == "Submit feedback" and hits["feedback"] == 0
    assert (await _state(http, control, sid))["wheel"] == "person"
    await _view(http, viewer, sid, "/wheel/hand-back", {})

    text = await page()
    pay = await _tool(http, control, sid, "browser_click",
                      {"element": "Pay now", "target": _ref(text, "button", "Pay now")}, ASKED)
    assert pay["handover"]["press"]["tier"] == "money", "money is the person's whatever was said"
    await asyncio.sleep(0.5)
    assert hits["pay"] == 0
    await _view(http, viewer, sid, "/wheel/hand-back", {})

    text = await page()
    asked = await _tool(http, control, sid, "browser_click",
                        {"element": "Submit feedback", "target": _ref(text, "button", "Submit feedback")}, ASKED)
    assert asked.get("handover", {}).get("kind") != "press", asked
    async with http.get(f"http://127.0.0.1:{control}/log", params={"session_id": sid}) as resp:
        pressed = [e for e in (await resp.json())["entries"] if e["action"] == "pressed"]
    assert pressed and pressed[-1]["target"] == "Submit feedback" and pressed[-1]["because"] == ASKED["because"]
    if asked.get("handover", {}).get("kind") == "send":  # the network still holds its send until F1125
        await _view(http, viewer, sid, "/release", {"allow_address": False})
    for _ in range(50):
        if hits["feedback"]:
            break
        await asyncio.sleep(0.1)
    assert hits["feedback"] == 1

    text = await page()
    await _tool(http, control, sid, "browser_click", {"element": "Message", "target": _ref(text, "textbox", "Message")})
    enter = await _tool(http, control, sid, "browser_press_key", {"key": "Enter"})
    assert enter["text"].startswith("Not pressed: 'Enter in Message': Enter sends what is in the message box"), \
        enter["text"]
    assert hits["message"] == 0

    over = await _tool(http, control, sid, "browser_hand_over", {"reason": "Pick the delivery slot you want."})
    assert over["handover"]["kind"] == "asked" and over["handover"]["reason"] == "Pick the delivery slot you want."
    assert (await _state(http, control, sid))["wheel"] == "person"
    await _post(http, control, "/close", {"session_id": sid})
