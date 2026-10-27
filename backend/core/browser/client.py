"""The engine's side of the browser component (BRW1, D328).

`tools` stands where the global Playwright MCP session used to stand in
`agent_sessions["Browser Automation"]`: the same server name and tool names, so
saved agents keep working, but each chat or run gets its own browser. The owner is
`owner` (a ContextVar set where an agent step starts); the first browser call
under an owner opens a session in the component, which this module starts lazily
as its own process.

What the agent reads comes back scrubbed of the workspace's secrets. The pane's
events (`browser_session`, then one `browser_action` per action) wait in
`take_events` for the agent loop to send on.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import secrets
import sys
from pathlib import Path

import aiohttp
from mcp import types

from core.browser import gate

_log = logging.getLogger(__name__)

SERVER_NAME = "Browser Automation"

#: Whose browser a call belongs to: "chat:<session>" or "run:<run id>".
owner: contextvars.ContextVar[str | None] = contextvars.ContextVar("browser_owner", default=None)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_READY = "BROWSER_HOST_READY"


def owner_for(session_id: str | None, run_id: str | None) -> str:
    return f"run:{run_id}" if run_id else f"chat:{session_id or 'unscoped'}"


class _Host:
    """The component process: started on first use, restarted if it died."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.control = self.viewer = 0
        self.token = ""
        self._lock = asyncio.Lock()

    def _env(self) -> dict[str, str]:
        keep = ("PATH", "HOME", "LANG", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME",
                "EGRESS_ALLOW_HOSTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        try:
            from core.config import load_settings
            path = load_settings().get("playwright_browsers_path")
            if path:
                env["PLAYWRIGHT_BROWSERS_PATH"] = path
        except Exception:
            pass
        env["SYNAPSE_BROWSER_TOKEN"] = self.token
        env["PYTHONPATH"] = str(_BACKEND_DIR)
        return env

    async def ensure(self) -> None:
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                return
            self.token = secrets.token_urlsafe(32)
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "core.browser.host", cwd=str(_BACKEND_DIR), env=self._env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            line = b""
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=60)
            except asyncio.TimeoutError:
                pass
            parts = line.decode(errors="replace").split()
            if len(parts) != 3 or parts[0] != _READY:
                self.proc.kill()
                self.proc = None
                raise RuntimeError("Synapse's browser did not start. Is Chromium installed (playwright install chromium)?")
            self.control, self.viewer = int(parts[1]), int(parts[2])

    def _headers(self) -> dict[str, str]:
        return {"X-Browser-Token": self.token}

    async def call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                   timeout: float = 120) -> tuple[int, dict]:
        await self.ensure()
        async with aiohttp.ClientSession(headers=self._headers(), timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.request(method, f"http://127.0.0.1:{self.control}{path}", json=json,
                                    params=params) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, body

    def viewer_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.viewer}{path}"

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


class BrowserTools:
    """The "Browser Automation" session the engine calls, one browser per owner."""

    def __init__(self, host: _Host | None = None) -> None:
        self.host = host or _Host()
        self._sessions: dict[str, str] = {}          # owner -> session id
        self._owners: dict[str, str] = {}            # session id -> owner
        self._events: dict[str, list[dict]] = {}
        self._tools: list[types.Tool] | None = None

    # ── what the engine's MCP paths call ─────────────────────────────────────
    async def send_ping(self) -> None:
        await self.host.ensure()

    async def list_tools(self) -> types.ListToolsResult:
        if self._tools is None:
            status, body = await self.host.call("GET", "/tools")
            if status != 200:
                raise RuntimeError("the browser's tools could not be listed")
            self._tools = [
                types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"],
                           annotations=types.ToolAnnotations(
                               readOnlyHint=t["tags"].get("read_only", False),
                               destructiveHint=False,
                               idempotentHint=t["tags"].get("idempotent", False),
                               openWorldHint=t["tags"].get("open_world", True)))
                for t in body.get("tools", [])]
        return types.ListToolsResult(tools=self._tools)

    async def call_tool(self, name: str, arguments: dict | None = None, read_timeout_seconds=None,
                        **_ignored) -> types.CallToolResult:
        key = owner.get() or owner_for(None, None)
        refused = gate.refusal(name)
        if refused:
            return _result(refused, error=True)
        try:
            sid = await self._session_for(key)
            status, body = await self.host.call("POST", "/tool", json={
                "session_id": sid, "name": name, "args": arguments or {}})
            if status == 404:  # the session was closed (idle) or the component restarted
                self._forget(key)
                sid = await self._session_for(key)
                status, body = await self.host.call("POST", "/tool", json={
                    "session_id": sid, "name": name, "args": arguments or {}})
        except Exception as exc:
            return _result(f"Synapse's browser is not available: {exc}", error=True)
        if body.get("refused"):
            return _result(body["refused"], error=True)
        if body.get("waiting"):
            return _result("The person is driving the browser; wait for them to hand it back.", error=True)
        if body.get("log"):
            self._events.setdefault(key, []).append({"type": "browser_action", "session_id": sid, **body["log"]})
        return _result(_scrub(body.get("text", "")), error=bool(body.get("is_error")))

    # ── sessions ─────────────────────────────────────────────────────────────
    async def _session_for(self, key: str) -> str:
        sid = self._sessions.get(key)
        if sid:
            return sid
        status, body = await self.host.call("POST", "/session", json={"owner": key, "mode": gate.READ_ONLY})
        if status != 200 or not body.get("session_id"):
            raise RuntimeError(body.get("error") or "the browser did not open")
        sid = body["session_id"]
        self._sessions[key] = sid
        self._owners[sid] = key
        self._events.setdefault(key, []).append({"type": "browser_session", "session_id": sid, "owner": key})
        return sid

    def _forget(self, key: str) -> None:
        sid = self._sessions.pop(key, None)
        if sid:
            self._owners.pop(sid, None)

    def owner_of(self, session_id: str) -> str | None:
        return self._owners.get(session_id)

    def take_events(self, key: str | None) -> list[dict]:
        return self._events.pop(key, []) if key else []

    async def close_owner(self, key: str) -> None:
        sid = self._sessions.get(key)
        self._forget(key)
        self._events.pop(key, None)
        if sid and self.host.proc is not None:
            try:
                await self.host.call("POST", "/close", json={"session_id": sid}, timeout=30)
            except Exception:
                pass


def _scrub(text: str) -> str:
    try:
        from core.secret_scrub import scrub_secrets
        return scrub_secrets(text)
    except Exception:
        return text


def _result(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)


#: The one instance the backend registers.
tools = BrowserTools()
