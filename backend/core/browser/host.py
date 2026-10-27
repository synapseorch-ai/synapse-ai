"""The browser component (BRW1, D328): run as `python -m core.browser.host`.

One process holds every session. A session is one headless Chromium with one
context, Playwright MCP connected to it over CDP for the agent's actions, and this
process's own Playwright connection to the same context for everything the agent
must not control: the network gate (`gate.verdict` on every request), the
screencast the person watches, a screenshot after each agent action, and the log.

Nothing outside this process reaches Chromium or CDP. The control listener serves
the engine; the viewer listener serves the person's pane. Both bind 127.0.0.1 and
require `SYNAPSE_BROWSER_TOKEN` (in cloud the MicroVM's port-scoped tokens sit in
front as well). The routes are DESIGN.md §6.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import deque
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from core.browser import gate

#: Playwright MCP, pinned: its tool names and output are part of the gate's contract.
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"

DEFAULT_LIMITS = {"actions": 40, "loads_per_min": 30, "idle_s": 600}
_HOST_GAP_S = 1.0          # at most one page load a second per host
_VIEWPORT = {"width": 1280, "height": 800}
#: Chromium's switches. WebRTC may reach any address a page names over UDP, which no route sees: only proxied UDP is
#: allowed, and there is no proxy for it (F1122). QUIC is off for the same reason, and WebTransport rides it.
_CHROMIUM_ARGS = ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]
_TOKEN_HEADER = "X-Browser-Token"
_SNAPSHOT_LINK = re.compile(r"- \[Snapshot\]\(([^)\s]+\.yml)\)")


def _clean_env() -> dict[str, str]:
    """What Chromium and Playwright MCP may see: no keys, no tokens."""
    keep = ("PATH", "HOME", "LANG", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _summary(name: str, args: dict) -> str:
    """One line for the log. Typed values stay out of it."""
    element = args.get("element") or args.get("target") or ""
    if name == "browser_navigate":
        return args.get("url", "")
    if name in ("browser_type", "browser_fill_form"):
        if name == "browser_fill_form":
            fields = args.get("fields") or []
            return f"filled {len(fields)} field(s)"
        return f"typed into {element}".strip()
    if name == "browser_select_option":
        return f"chose an option in {element}".strip()
    if name == "browser_press_key":
        return f"pressed {args.get('key', '')}"
    return str(element)


class Session:
    def __init__(self, sid: str, owner: str, mode: str, limits: dict, root: Path):
        self.id = sid
        self.owner = owner
        self.mode = mode if mode in gate.MODES else gate.READ_ONLY
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.dir = root / sid
        self.log: list[dict] = []
        self.shots: dict[str, bytes] = {}
        self.actions = 0
        self.loads: deque[float] = deque()
        self.host_last: dict[str, float] = {}
        self.last_used = time.monotonic()
        self.wheel = "agent"
        self.viewers: set[asyncio.Queue] = set()
        self.ctx = None
        self.page = None
        self.mcp = None
        self._cdp = None
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self, pw) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # No service workers: a worker's own fetches would not pass the route below.
        self.ctx = await pw.chromium.launch_persistent_context(
            str(self.dir / "profile"), headless=True, viewport=_VIEWPORT, args=_CHROMIUM_ARGS,
            env=_clean_env(), service_workers="block",
        )
        await self.ctx.route("**/*", self._route)
        await self.ctx.route_web_socket(lambda _url: True, self._route_socket)
        self.ctx.on("page", self._on_page)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        port = await self._devtools_port()
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(
            command=shutil.which("npx") or "npx",
            args=["-y", PLAYWRIGHT_MCP, "--cdp-endpoint", f"http://127.0.0.1:{port}",
                  "--no-webmcp", "--image-responses", "omit", "--codegen", "none",
                  "--output-dir", str(self.dir / "mcp")],
            env=_clean_env(), cwd=str(self.dir),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.mcp = await self._stack.enter_async_context(ClientSession(read, write))
        await self.mcp.initialize()

    async def _devtools_port(self) -> int:
        marker = self.dir / "profile" / "DevToolsActivePort"
        for _ in range(100):
            if marker.exists():
                first = marker.read_text().splitlines()[:1]
                if first and first[0].strip().isdigit():
                    return int(first[0])
            await asyncio.sleep(0.1)
        raise RuntimeError("Chromium did not open its debugging port")

    async def close(self) -> None:
        await self._stop_screencast()
        try:
            await self._stack.aclose()
        except Exception:
            pass
        try:
            if self.ctx is not None:
                await self.ctx.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)
        for q in list(self.viewers):
            _offer(q, {"closed": True})

    # ── the network gate ─────────────────────────────────────────────────────
    async def _route(self, route) -> None:
        req = route.request
        try:
            reason = await gate.verdict(req.url, req.method, self.mode)
            if reason is None and req.is_navigation_request():
                reason = await self._pace(urlparse(req.url).hostname or "")
        except Exception:
            reason = "the browser's guard failed"
        try:
            if reason is None:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
                self._note("guard", "blocked", reason)
        except Exception:
            pass  # the page or the request is already gone

    async def _route_socket(self, ws) -> None:
        """A page's WebSocket, checked before it opens (F1122): the route above never sees one."""
        try:
            reason = await gate.socket_verdict(ws.url, self.mode)
        except Exception:
            reason = "the browser's guard failed"
        if reason is None:
            ws.connect_to_server()
            return
        self._note("guard", "blocked", reason)
        try:
            await ws.close(code=1008, reason="blocked by Synapse")
        except Exception:
            pass  # the page is already gone

    async def _pace(self, host: str) -> str | None:
        now = time.monotonic()
        while self.loads and now - self.loads[0] > 60:
            self.loads.popleft()
        if len(self.loads) >= self.limits["loads_per_min"]:
            return f"more than {self.limits['loads_per_min']} page loads in a minute"
        gap = _HOST_GAP_S - (now - self.host_last.get(host, -1e9))
        if gap > 0:
            await asyncio.sleep(gap)
        self.host_last[host] = time.monotonic()
        self.loads.append(time.monotonic())
        return None

    # ── the agent's tools ────────────────────────────────────────────────────
    async def call(self, name: str, args: dict) -> dict:
        self.last_used = time.monotonic()
        refused = gate.refusal(name)
        if refused:
            return {"refused": refused}
        if self.wheel != "agent":
            return {"waiting": "person_driving"}
        if self.actions >= self.limits["actions"]:
            return {"refused": f"This browser session has used its {self.limits['actions']} actions."}
        async with self._lock:
            self.actions += 1
            seen = len(self.log)
            result = await self.mcp.call_tool(name, args)
            text = self._inline_snapshots("\n".join(getattr(c, "text", "") for c in result.content
                                                    if getattr(c, "type", "") == "text"))
            blocked = [e["target"] for e in self.log[seen:] if e["actor"] == "guard"]
            if blocked:
                text += ("\n### Synapse\nNothing left the browser for: " + "; ".join(dict.fromkeys(blocked))
                         + ". The page may show an error because of it.")
            entry = None
            if name in gate.CHANGES_PAGE:
                entry = await self._record("agent", name, _summary(name, args or {}))
        return {"text": text, "is_error": bool(result.isError), "log": entry}

    def _inline_snapshots(self, text: str) -> str:
        """Playwright MCP links an action's page snapshot to a file in this session's
        folder; the agent only reads text, so the file's contents replace the link."""
        def put(match: re.Match) -> str:
            path = (self.dir / match.group(1)).resolve()
            if self.dir.resolve() not in path.parents or not path.is_file():
                return match.group(0)
            return "```yaml\n" + path.read_text(errors="replace") + "\n```"
        return _SNAPSHOT_LINK.sub(put, text)

    async def _record(self, actor: str, action: str, target: str) -> dict:
        ref = None
        try:
            shot = await self.page.screenshot(type="jpeg", quality=60)
            ref = f"{len(self.log) + 1}"
            self.shots[ref] = shot
        except Exception:
            pass
        return self._note(actor, action, target, ref)

    def _note(self, actor: str, action: str, target: str, shot_ref: str | None = None) -> dict:
        url = ""
        try:
            url = self.page.url if self.page else ""
        except Exception:
            pass
        entry = {"n": len(self.log) + 1, "at": time.time(), "actor": actor, "action": action,
                 "target": target, "url": url, "shot_ref": shot_ref}
        self.log.append(entry)
        return entry

    # ── the person's view ────────────────────────────────────────────────────
    def _on_page(self, page) -> None:
        self.page = page
        page.on("close", lambda _p: self._page_closed())
        if self.viewers:
            asyncio.ensure_future(self._restart_screencast())

    def _page_closed(self) -> None:
        pages = self.ctx.pages if self.ctx else []
        self.page = pages[-1] if pages else None
        if self.viewers and self.page is not None:
            asyncio.ensure_future(self._restart_screencast())

    async def _restart_screencast(self) -> None:
        await self._stop_screencast()
        await self._start_screencast()

    async def _start_screencast(self) -> None:
        if self._cdp is not None or self.page is None:
            return
        cdp = await self.ctx.new_cdp_session(self.page)
        self._cdp = cdp

        def on_frame(ev):
            meta = {"jpeg_b64": ev["data"], "w": ev.get("metadata", {}).get("deviceWidth"),
                    "h": ev.get("metadata", {}).get("deviceHeight"), "url": self.page.url if self.page else "",
                    "wheel": self.wheel}
            for q in list(self.viewers):
                _offer(q, meta)
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60,
                                                "maxWidth": _VIEWPORT["width"], "maxHeight": _VIEWPORT["height"]})

    async def _stop_screencast(self) -> None:
        cdp, self._cdp = self._cdp, None
        if cdp is None:
            return
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except Exception:
            pass

    async def first_frame(self) -> dict | None:
        try:
            shot = await self.page.screenshot(type="jpeg", quality=60)
        except Exception:
            return None
        return {"jpeg_b64": base64.b64encode(shot).decode(), "w": _VIEWPORT["width"], "h": _VIEWPORT["height"],
                "url": self.page.url, "wheel": self.wheel}


def _offer(q: asyncio.Queue, item: dict) -> None:
    """Newest frame wins: a slow viewer skips frames rather than lagging."""
    while q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
    q.put_nowait(item)


class Host:
    def __init__(self, token: str, root: Path):
        self.token = token
        self.root = root
        self.sessions: dict[str, Session] = {}
        self.pw = None

    def _authorised(self, request: web.Request) -> bool:
        given = request.headers.get(_TOKEN_HEADER, "")
        return bool(given) and hmac.compare_digest(given, self.token)

    @web.middleware
    async def auth(self, request: web.Request, handler):
        if not self._authorised(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        return await handler(request)

    def _session(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such session"}), content_type="application/json")
        return s

    # control
    async def health(self, _request):
        return web.json_response({"ok": True, "sessions": len(self.sessions)})

    async def tools(self, _request):
        return web.json_response({"tools": await self._tool_schemas()})

    async def _tool_schemas(self) -> list[dict]:
        if getattr(self, "_schemas", None) is not None:
            return self._schemas
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(
            command=shutil.which("npx") or "npx",
            args=["-y", PLAYWRIGHT_MCP, "--cdp-endpoint", "http://127.0.0.1:9", "--no-webmcp"],
            env=_clean_env())
        async with stdio_client(params) as (read, write), ClientSession(read, write) as mcp:
            await mcp.initialize()
            listed = (await mcp.list_tools()).tools
        self._schemas = [{"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema,
                          "tags": gate.ALLOWED[t.name]} for t in listed if t.name in gate.ALLOWED]
        return self._schemas

    async def open_session(self, request):
        body = await request.json()
        sid = uuid.uuid4().hex
        s = Session(sid, str(body.get("owner", "")), body.get("mode", gate.READ_ONLY), body.get("limits") or {},
                    self.root)
        try:
            await s.start(self.pw)
        except Exception as exc:
            await s.close()
            return web.json_response({"error": f"the browser did not start: {exc}"}, status=503)
        self.sessions[sid] = s
        return web.json_response({"session_id": sid})

    async def call_tool(self, request):
        body = await request.json()
        s = self._session(body.get("session_id", ""))
        return web.json_response(await s.call(str(body.get("name", "")), body.get("args") or {}))

    async def get_log(self, request):
        s = self._session(request.query.get("session_id", ""))
        after = int(request.query.get("after", "0") or 0)
        return web.json_response({"entries": [e for e in s.log if e["n"] > after]})

    async def get_shot(self, request):
        s = self._session(request.query.get("session_id", ""))
        shot = s.shots.get(request.query.get("ref", ""))
        if shot is None:
            raise web.HTTPNotFound()
        return web.Response(body=shot, content_type="image/jpeg")

    async def close_session(self, request):
        body = await request.json()
        s = self.sessions.pop(body.get("session_id", ""), None)
        if s is not None:
            await s.close()
        return web.json_response({"closed": s is not None, "login_state": None})

    # viewer
    async def frames(self, request):
        s = self._session(request.query.get("session_id", ""))
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        first = await s.first_frame()
        if first:
            _offer(q, first)
        s.viewers.add(q)
        await s._start_screencast()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                await resp.write(f"data: {json.dumps(item)}\n\n".encode())
                if item.get("closed"):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            s.viewers.discard(q)
            if not s.viewers:
                await s._stop_screencast()
        return resp

    async def input(self, _request):
        # v1: the person watches. Taking the wheel arrives in v2 (DESIGN.md §2.4).
        return web.json_response({"refused": "In this version you watch; taking over comes next."}, status=409)

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for sid, s in list(self.sessions.items()):
                if now - s.last_used > s.limits["idle_s"] and not s.viewers:
                    self.sessions.pop(sid, None)
                    await s.close()


def control_app(host: Host) -> web.Application:
    app = web.Application(middlewares=[host.auth])
    app.router.add_get("/health", host.health)
    app.router.add_get("/tools", host.tools)
    app.router.add_post("/session", host.open_session)
    app.router.add_post("/tool", host.call_tool)
    app.router.add_get("/log", host.get_log)
    app.router.add_get("/shot", host.get_shot)
    app.router.add_post("/close", host.close_session)
    return app


def viewer_app(host: Host) -> web.Application:
    app = web.Application(middlewares=[host.auth])
    app.router.add_get("/frames", host.frames)
    app.router.add_post("/input", host.input)
    return app


async def serve(control_port: int, viewer_port: int) -> None:
    token = os.environ.get("SYNAPSE_BROWSER_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("SYNAPSE_BROWSER_TOKEN is missing or too short")
    from playwright.async_api import async_playwright
    root = Path(tempfile.mkdtemp(prefix="synapse-browser-"))
    host = Host(token, root)
    host.pw = await async_playwright().start()
    runners = []
    ports = []
    for app, port in ((control_app(host), control_port), (viewer_app(host), viewer_port)):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        runners.append(runner)
        ports.append(site._server.sockets[0].getsockname()[1])
    print(f"BROWSER_HOST_READY {ports[0]} {ports[1]}", flush=True)
    try:
        await host.reap()
    finally:
        for s in list(host.sessions.values()):
            await s.close()
        await host.pw.stop()
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse's browser component")
    parser.add_argument("--control-port", type=int, default=0)
    parser.add_argument("--viewer-port", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(serve(args.control_port, args.viewer_port))


if __name__ == "__main__":
    main()
