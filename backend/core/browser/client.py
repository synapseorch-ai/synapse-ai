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
#: The calling agent's browser settings (`core.models.AgentBrowser` as a dict), read when a session opens.
settings: contextvars.ContextVar[dict | None] = contextvars.ContextVar("browser_settings", default=None)

#: The person's own words for this call, read only when the agent quotes them to press a button that acts for
#: them (`confirm`): an async callable returning their messages, the agent's instructions and the step's own text,
#: never page text or a model's (V2-CONTRACT "Final buttons and sends").
own_words: contextvars.ContextVar = contextvars.ContextVar("browser_own_words", default=None)

#: How long an agent's call waits while the person drives before it gives up and says so: inside the
#: engine's 60 s for one MCP call (`MCP_TOOL_CALL_TIMEOUT`). A longer wait is a park, not a call.
WAIT_FOR_PERSON_S = 45

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_READY = "BROWSER_HOST_READY"


async def _own_words() -> list[str]:
    source = own_words.get()
    if source is None:
        return []
    try:
        return [w for w in await source() if isinstance(w, str) and w.strip()]
    except Exception:
        return []  # unreadable: no quote is found, and the person presses it


def owner_for(session_id: str | None, run_id: str | None) -> str:
    return f"run:{run_id}" if run_id else f"chat:{session_id or 'unscoped'}"


class _Host:
    """The component process: started on first use, restarted if it died."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.control = self.viewer_port = 0
        self.token = ""
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

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
            if self.running:
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
            self.control, self.viewer_port = int(parts[1]), int(parts[2])

    def _headers(self) -> dict[str, str]:
        return {"X-Browser-Token": self.token}

    async def _request(self, method: str, url: str, *, json: dict | None, params: dict | None, timeout: float,
                       raw: bool = False):
        await self.ensure()
        async with aiohttp.ClientSession(headers=self._headers(), timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.request(method, url, json=json, params=params) as resp:
                if raw:
                    return resp.status, await resp.read()
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, body

    async def call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                   timeout: float = 120) -> tuple[int, dict]:
        return await self._request(method, f"http://127.0.0.1:{self.control}{path}", json=json, params=params,
                                   timeout=timeout)

    async def get_bytes(self, path: str, *, params: dict | None = None, timeout: float = 30) -> tuple[int, bytes]:
        return await self._request("GET", f"http://127.0.0.1:{self.control}{path}", json=None, params=params,
                                   timeout=timeout, raw=True)

    async def viewer(self, path: str, *, params: dict | None = None, json: dict | None = None,
                     timeout: float = 60) -> tuple[int, dict]:
        return await self._request("POST", self.viewer_url(path), json=json or {}, params=params, timeout=timeout)

    def viewer_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.viewer_port}{path}"

    async def stop(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


class LocalHosts:
    """Where each owner's browser is (F1135): here, every owner's is in the one local component process. Cloud
    gives each its own MicroVM (BRW3), through `set_host_provider`. A host has `call`, `get_bytes` and `viewer`."""

    def __init__(self, host: _Host) -> None:
        self.host = host

    async def host_for(self, owner_key: str, *, opening: bool):
        """The owner's host; with nothing open and `opening` false, None (nothing is started for a lookup)."""
        if opening or self.host.running:
            await self.host.ensure()
            return self.host
        return None

    async def ping(self) -> None:
        await self.host.ensure()

    async def close(self, owner_key: str) -> None:
        pass

    async def park(self, owner_key: str) -> None:
        """A turn waits for the person; cloud suspends the browser's VM here."""

    async def resume(self, owner_key: str) -> None:
        """The person answered; cloud resumes the VM here."""


class MemorySessionStore:
    """Which browser an owner has, and what it waits on (F1136): one record per owner, `{sid, parked, expired,
    sessions}`. In this process's memory here; cloud keeps it on the browser's own row, because a chat's next turn
    or a run's resume may land on another worker (BRW3)."""

    def __init__(self) -> None:
        self._by_owner: dict[str, dict] = {}
        self._by_sid: dict[str, str] = {}

    async def get_owner(self, owner_key: str) -> dict | None:
        record = self._by_owner.get(owner_key)
        return dict(record) if record else None

    async def set_owner(self, owner_key: str, record: dict) -> None:
        old = self._by_owner.get(owner_key)
        if old and old.get("sid") and old.get("sid") != record.get("sid"):
            self._by_sid.pop(old["sid"], None)
        self._by_owner[owner_key] = dict(record)
        if record.get("sid"):
            self._by_sid[record["sid"]] = owner_key

    async def pop_owner(self, owner_key: str) -> dict | None:
        record = self._by_owner.pop(owner_key, None)
        if record and record.get("sid"):
            self._by_sid.pop(record["sid"], None)
        return record

    async def owner_of(self, sid: str) -> str | None:
        return self._by_sid.get(sid)


class BrowserTools:
    """The "Browser Automation" session the engine calls, one browser per owner."""

    def __init__(self, host: _Host | None = None, *, hosts=None, store=None) -> None:
        self.host = host or _Host()               # this process's component (the app's own frames and shot routes)
        self.hosts = hosts or LocalHosts(self.host)
        self.store = store or MemorySessionStore()
        self.action_gate = None                   # async (owner, tool name) -> a refusal, or None (F1135)
        self._events: dict[str, list[dict]] = {}
        self._handovers: dict[str, dict] = {}       # owner -> the person's turn this call ended on
        self._tools: list[types.Tool] | None = None

    # ── what the engine's MCP paths call ─────────────────────────────────────
    async def send_ping(self) -> None:
        ping = getattr(self.hosts, "ping", None)
        if ping is not None:
            await ping()

    async def list_tools(self) -> types.ListToolsResult:
        if self._tools is None:
            listed = _committed_tool_schemas()
            if listed is None:  # no committed list (a development tree): the component lists them
                host = await self.hosts.host_for("tools", opening=True)
                status, body = await host.call("GET", "/tools")
                if status != 200:
                    raise RuntimeError("the browser's tools could not be listed: " + _npx_hint())
                listed = body.get("tools", [])
            self._tools = [
                types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"],
                           annotations=types.ToolAnnotations(
                               readOnlyHint=t["tags"].get("read_only", False),
                               destructiveHint=False,
                               idempotentHint=t["tags"].get("idempotent", False),
                               openWorldHint=t["tags"].get("open_world", True)))
                for t in listed]
        return types.ListToolsResult(tools=self._tools)

    async def call_tool(self, name: str, arguments: dict | None = None, read_timeout_seconds=None,
                        **_ignored) -> types.CallToolResult:
        key = owner.get() or owner_for(None, None)
        refused = gate.refusal(name)
        if refused:
            return _result(refused, error=True)
        if self.action_gate is not None and name != gate.HAND_OVER:  # a hand-over is never refused for money
            try:
                refused = await self.action_gate(key, name)
            except Exception as exc:
                refused = f"Synapse could not check this browser action: {exc}"
            if refused:
                return _result(refused, error=True)
        arguments = dict(arguments or {})
        quote = arguments.pop("confirm", None)
        press = None
        if name in gate.PRESSES and isinstance(quote, str) and quote.strip():
            press = {"because": quote.strip()[:300], "confirmed": gate.quote_found(quote, await _own_words())}
        try:
            host, sid = await self._session_for(key)
            record = await self.store.get_owner(key) or {}
            if record.get("expired"):  # the kept session ran out: the person signs in again first
                self._handovers[key] = {"kind": "login_expired", "reason": _EXPIRED, "session_id": sid,
                                        **record["expired"]}
                await self.store.set_owner(key, {**record, "expired": None})
                return _result("The kept session has expired. It is the person's turn in the browser to sign in "
                               "again; stop here, and you will continue when they hand it back.", error=True)
            status, body = await host.call("POST", "/tool", json={
                "session_id": sid, "name": name, "args": arguments, "press": press})
            if status == 404:  # the session was closed (idle) or the component restarted
                await self.store.pop_owner(key)
                host, sid = await self._session_for(key)
                status, body = await host.call("POST", "/tool", json={
                    "session_id": sid, "name": name, "args": arguments, "press": press})
        except Exception as exc:
            return _result(f"Synapse's browser is not available: {exc}", error=True)
        if body.get("waiting"):
            if not await self._wait_for_wheel(host, sid):
                return _result("The person is still driving the browser. Stop here and say so; carry on when "
                               "they hand it back.", error=True)
            status, body = await host.call("POST", "/tool", json={
                "session_id": sid, "name": name, "args": arguments, "press": press})
        if body.get("refused"):
            return _result(body["refused"], error=True)
        if body.get("waiting"):
            return _result("The person is driving the browser; wait for them to hand it back.", error=True)
        if body.get("log"):
            self._events.setdefault(key, []).append({"type": "browser_action", "session_id": sid, **body["log"]})
            await self._keep_shot(host, key, sid, body["log"])
        await self.keep_lines(key, [*([body["log"]] if body.get("log") else []), *(body.get("notes") or [])])
        text = _scrub(body.get("text", ""))
        if body.get("handover"):
            self._handovers[key] = body["handover"]
            text += ("\n### Synapse\nIt is the person's turn in the browser: " + body["handover"]["reason"]
                     + " Stop here; you will continue when they hand it back.")
        return _result(text, error=bool(body.get("is_error")))

    async def _keep_shot(self, host, key: str, sid: str, entry: dict) -> None:
        """The filmstrip's copy (F1103): best effort, never in the agent's way."""
        ref = entry.get("shot_ref")
        if not ref or not key.startswith(("run:", "chat:")):
            return
        try:
            status, jpeg = await host.get_bytes("/shot", params={"session_id": sid, "ref": ref}, timeout=15)
            if status != 200:
                return
            from core.browser import kept
            await asyncio.to_thread(kept.keep, key, ref, jpeg)
        except Exception:
            pass

    async def keep_lines(self, key: str, entries: list[dict]) -> None:
        """The record of who pressed and sent what (F1125): best effort, never in the agent's way."""
        if not entries or not key.startswith(("run:", "chat:")):
            return
        try:
            from core.browser import kept
            await asyncio.to_thread(kept.keep_lines, key, entries)
        except Exception:
            pass

    async def _wait_for_wheel(self, host, sid: str) -> bool:
        """While the person drives, the agent's call waits for the wheel to come back."""
        deadline = asyncio.get_running_loop().time() + WAIT_FOR_PERSON_S
        while asyncio.get_running_loop().time() < deadline:
            status, state = await host.call("GET", "/state", params={"session_id": sid}, timeout=30)
            if status != 200:
                return False
            if state.get("wheel") == "agent":
                return True
            await asyncio.sleep(1)
        return False

    async def take_handover(self, key: str | None, **where) -> dict | None:
        """The person's turn this call ended on, now parked: the engine stops the agent here."""
        handover = self._handovers.pop(key, None) if key else None
        if handover:
            record = await self.store.get_owner(key)
            if record is not None:
                await self.store.set_owner(key, {**record, "parked": {**handover, **where}})
            await self.hosts.park(key)
        return handover

    async def take_parked(self, key: str | None) -> dict | None:
        """The parked turn the person just answered, once."""
        record = await self.store.get_owner(key) if key else None
        parked = (record or {}).get("parked")
        if parked:
            await self.store.set_owner(key, {**record, "parked": None})
            await self.hosts.resume(key)
        return parked

    async def keep_login(self, session_id: str, *, login_ref: str | None, sites: list[str], owner: str) -> str | None:
        """Keep the session's current sign-in (the person just signed in), under `login_ref` or a new one."""
        key = await self.store.owner_of(session_id)
        host = await self.hosts.host_for(key, opening=False) if key else None
        if host is None:
            return None
        status, body = await host.call("GET", "/login-state", params={"session_id": session_id})
        state = body.get("login_state") if status == 200 else None
        if not state or not (state.get("cookies") or state.get("origins")):
            return None
        return await _logins().keep(login_ref, state=state, sites=sites, owner=owner)

    async def open_login_session(self, sites: list[str]) -> str:
        """"Log in to a site": the person's alone, no agent, the wheel theirs from the start."""
        key = f"login:{secrets.token_hex(8)}"
        host = await self.hosts.host_for(key, opening=True)
        status, body = await host.call("POST", "/session", json={
            "owner": key, "mode": gate.CO_DRIVE, "person_only": True, "limits": {"actions": 1}})
        if status != 200 or not body.get("session_id"):
            raise RuntimeError(body.get("error") or "the browser did not open")
        sid = body["session_id"]
        await self.store.set_owner(key, {"sid": sid, "parked": None, "expired": None, "sessions": list(sites)})
        self._events.setdefault(key, []).append({"type": "browser_session", "session_id": sid, "owner": key})
        return sid

    async def finish_login(self, session_id: str, *, keep: bool, owner: str) -> str | None:
        """End a "Log in to a site" session, keeping its sign-in when the person asked to."""
        key = await self.store.owner_of(session_id)
        record = await self.store.get_owner(key) if key else None
        if not record or record.get("sessions") is None:
            return None
        ref = await self.keep_login(session_id, login_ref=None, sites=record["sessions"], owner=owner) if keep else None
        await self.close_owner(key)
        return ref

    # ── sessions ─────────────────────────────────────────────────────────────
    async def _session_for(self, key: str):
        """The owner's host and browser session, opening one on first use. Its record is written only once the
        component has said yes, so a store never holds a session that isn't there."""
        record = await self.store.get_owner(key)
        if record and record.get("sid"):
            host = await self.hosts.host_for(key, opening=False)
            if host is not None:
                return host, record["sid"]
            await self.store.pop_owner(key)  # its browser is gone
        chosen = settings.get() or {}
        login_ref = chosen.get("login_ref")
        state = await _login_state(login_ref) if login_ref else None
        expired = bool(login_ref) and state is None
        opening = {"owner": key, "mode": chosen.get("mode") or gate.READ_ONLY, "sites": chosen.get("sites") or [],
                   "limits": {"actions": int(chosen.get("action_budget") or 40)}}
        if state:
            opening["login_state"] = state
        if expired:
            opening["turn"] = {"kind": "login_expired", "reason": _EXPIRED}
        host = await self.hosts.host_for(key, opening=True)
        status, body = await host.call("POST", "/session", json=opening)
        if status != 200 or not body.get("session_id"):
            raise RuntimeError(body.get("error") or "the browser did not open")
        sid = body["session_id"]
        await self.store.set_owner(key, {
            "sid": sid, "parked": None, "sessions": None,
            "expired": {"login_ref": login_ref, "sites": chosen.get("sites") or []} if expired else None})
        self._events.setdefault(key, []).append({"type": "browser_session", "session_id": sid, "owner": key})
        return host, sid

    async def owner_of(self, session_id: str) -> str | None:
        return await self.store.owner_of(session_id)

    async def viewer(self, session_id: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """The person's action on their view: the wheel, input, release, allow a site."""
        key = await self.store.owner_of(session_id)
        host = await self.hosts.host_for(key, opening=False) if key else None
        if host is None:
            return 404, {"error": "That browser is closed."}
        return await host.viewer(path, params={"session_id": session_id}, json=body or {})

    def take_events(self, key: str | None) -> list[dict]:
        return self._events.pop(key, []) if key else []

    async def close_owner(self, key: str) -> None:
        record = await self.store.pop_owner(key)
        self._events.pop(key, None)
        sid = (record or {}).get("sid")
        host = await self.hosts.host_for(key, opening=False) if sid else None
        if host is not None:
            try:  # the whole log is kept before the browser goes: lines nobody's call carried are in it too
                status, body = await host.call("GET", "/log", params={"session_id": sid}, timeout=30)
                if status == 200:
                    await self.keep_lines(key, body.get("entries") or [])
            except Exception:
                pass
            try:
                await host.call("POST", "/close", json={"session_id": sid}, timeout=30)
            except Exception:
                pass
        await self.hosts.close(key)


# ── the seams cloud fills (F1135, F1136; BRW3) ───────────────────────────────

def set_host_provider(provider) -> None:
    """Where each owner's browser runs: `host_for(owner, *, opening)`, `close`, `park`, `resume` (optional `ping`)."""
    tools.hosts = provider


def set_session_store(store) -> None:
    """Which browser each owner has: async `get_owner`, `set_owner`, `pop_owner`, `owner_of`."""
    tools.store = store


def set_action_gate(gate_fn) -> None:
    """Asked before every agent action but a hand-over: async (owner, tool name) -> a refusal for the agent, or
    None. Cloud checks the wallet's floor and the run's cap here (DESIGN §4.2)."""
    tools.action_gate = gate_fn


_login_store = None


def set_login_store(store) -> None:
    """Where kept sign-ins live: async `state_for(ref)` and `keep(ref, *, state, sites, owner)`."""
    global _login_store
    _login_store = store


def _logins():
    if _login_store is not None:
        return _login_store
    from core import website_logins
    return website_logins


_SCHEMAS = Path(__file__).with_name("tool_schemas.json")


def _committed_tool_schemas() -> list[dict] | None:
    """The gate's tools as pinned Playwright MCP lists them, committed beside this module (F1135): listing needs no
    Node and no browser. `confirm` and `browser_hand_over` are the component's own, added on top."""
    import json
    try:
        listed = json.loads(_SCHEMAS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    from core.browser.host import _HAND_OVER_SCHEMA, _with_confirm_schema
    return [{"name": t["name"], "description": t["description"], "inputSchema": _with_confirm_schema(t["name"],
             t["inputSchema"]), "tags": gate.ALLOWED[t["name"]]} for t in listed if t["name"] in gate.ALLOWED] \
        + [_HAND_OVER_SCHEMA]


def _npx_hint() -> str:
    import shutil
    return "npx is not on this backend's PATH (Node is needed)" if shutil.which("npx") is None else "see the log"


_EXPIRED = "The saved login has expired: log in again, then hand the browser back."


async def _login_state(login_ref: str) -> dict | None:
    """The kept sign-in, decrypted, or None once expired, forgotten or never kept."""
    try:
        return await _logins().state_for(login_ref)
    except Exception:
        return None


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
