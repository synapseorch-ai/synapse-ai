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
import json
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

#: Whose kept sessions this call may open (F1138): the chat's person, the run's starter, the schedule's or the API
#: key's owner. Unset (None), the install's one person: OSS has one. Cloud sets it around each job.
for_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("browser_for_user", default=None)

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


#: How long a replay may take, and how often the client asks how it is going.
REPLAY_S, REPLAY_POLL_S = 600, 1.0


async def _replay_polled(host, sid: str, path: str, body: dict) -> tuple[int, dict]:
    """A replay started (`wait: false`) and then polled until it ends (BRW3: a long request may not survive the
    MicroVM endpoint, an idle cutoff or a restart). Its id is ours, sent with the start, which is sent once: if the
    answer is lost, that id is polled (404 a few times means it never arrived); a 409 is another replay running."""
    replay_id = f"rp_{secrets.token_hex(8)}"
    try:
        status, started = await host.call("POST", path, json={**body, "wait": False, "replay_id": replay_id},
                                          timeout=30)
        if status == 409 and started.get("replay_id") and started["replay_id"] != replay_id:
            return 409, {"error": "Another recording is still running in this browser; wait for it to finish."}
        if 400 <= status < 500 and status != 409:
            return status, started
    except Exception:
        pass  # lost on the way back, or never arrived: its status says which
    loop = asyncio.get_running_loop()
    deadline, misses = loop.time() + REPLAY_S, 0
    while loop.time() < deadline:
        try:
            status, now = await host.call("GET", "/replay/status", timeout=30,
                                          params={"session_id": sid, "replay_id": replay_id})
        except Exception:
            status, now = 599, {}
        if status == 200 and not now.get("running"):
            return 200, now
        if status == 404:
            misses += 1
            if misses >= 5:  # nothing started: the start never arrived
                return 503, {"error": "The browser did not start the recording; try again."}
        await asyncio.sleep(REPLAY_POLL_S)
    return 504, {"error": f"The recording was still running after {REPLAY_S // 60} minutes."}


class RecordingOpen(RuntimeError):
    """A recording already has its browser open (someone is recording or re-recording it)."""


class ReplayStopped(RuntimeError):
    """Re-recording from a step: the steps before it stopped (a miss, or the person's own step). The browser is open
    at `session_id`; `replayed` says where and why."""

    def __init__(self, session_id: str, replayed: dict):
        super().__init__("the steps before it stopped")
        self.session_id, self.replayed = session_id, replayed


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
        if name == gate.RUN_RECORDING:
            return await self._run_recording(key, arguments)
        quote = arguments.pop("confirm", None)
        press = None
        if name in gate.PRESSES and isinstance(quote, str) and quote.strip():
            press = {"because": quote.strip()[:300], "confirmed": gate.quote_found(quote, await _own_words())}
        try:
            host, sid = await self._session_for(key)
            record = await self.store.get_owner(key) or {}
            if record.get("expired"):  # a kept session ran out: the person signs in again first
                site = record["expired"].get("site") or "the site"
                self._handovers[key] = {"kind": "session_expired", "reason": _expired_words(site), "session_id": sid,
                                        **record["expired"]}
                await self.store.set_owner(key, {**record, "expired": None})
                return _result(f"The kept session for {site} has expired. It is the person's turn in the browser to "
                               "sign in again; stop here, and you will continue when they hand it back.", error=True)
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

    async def _run_recording(self, key: str, args: dict) -> types.CallToolResult:
        """`browser_run_recording` (F1145): one of the agent's picked recordings, replayed by code in its browser.
        Only a picked one runs, whatever the model asks (the picks are the person's, `settings`); a miss leaves the
        agent to find the step again, and its way comes back on `carry_on` as a pending edit, never applied (F1144)."""
        from core.browser import recordings as recs
        want = str(args.get("recording") or "").strip()
        docs = await recs.picked(list((settings.get() or {}).get("recordings") or []))
        doc = next((d for d in docs if want and want in (d["id"], recs.slug(d))), None)
        if doc is None:
            yours = ", ".join(recs.slug(d) for d in docs) or "none"
            return _result(f"'{want}' is not one of the recordings picked for you (yours: {yours}).", error=True)
        name, inputs = recs.slug(doc), dict(args.get("inputs") or {})
        if not args.get("carry_on"):
            missing = [i for i in recs.input_names(doc) if inputs.get(i) in (None, "")]
            if missing:
                return _result(f"'{name}' needs its inputs: {', '.join(missing)}.", error=True)
        try:
            host, sid = await self._session_for(key)
        except Exception as exc:
            return _result(f"Synapse's browser is not available: {exc}", error=True)
        if args.get("carry_on"):
            status, body = await _replay_polled(host, sid, "/replay/continue", {"session_id": sid})
        else:
            status, body = await _replay_polled(host, sid, "/replay", {
                "session_id": sid, "steps": doc["steps"], "inputs": inputs, "recording_id": doc["id"],
                "unattended": key.startswith("run:")})
        if status >= 400 or body.get("refused") or body.get("error"):
            return _result(body.get("refused") or body.get("error") or "The recording could not run.", error=True)
        said: list[str] = []
        fix = body.get("fix")
        if fix and fix.get("steps"):
            run_id = key.split(":", 1)[1] if key.startswith("run:") else None
            try:
                await recs.add_edit(doc["id"], step_id=fix["step_id"], steps=fix["steps"], by="agent", run_id=run_id,
                                    shots=fix.get("shots") or None)
                said.append(f"Your way through step {fix['n']} is kept as a proposed change to '{name}' for the "
                            "person to approve; the recording itself is unchanged until they do.")
            except recs.NotFound:
                said.append(f"'{name}' was deleted meanwhile: your way through step {fix['n']} was not kept.")
        if body.get("handover"):
            self._handovers[key] = body["handover"]
            said.append(f"'{name}' stopped at step {body.get('at')}: it is the person's turn in the browser: "
                        f"{body['handover'].get('reason', '')} Stop here; when they hand it back, call "
                        "browser_run_recording again with carry_on: true.")
        elif body.get("miss"):
            miss = body["miss"]
            said.append(
                f"'{name}' stopped at step {miss.get('n')}, \"{miss.get('line')}\": {miss.get('why')}. Find the way "
                "for that step with your browser tools, for this run only: do what that step did, on the same site "
                "(a press the person made there may be redone once; payments and deletes stay theirs). Then call "
                "browser_run_recording again with carry_on: true. If you can't find it, call browser_hand_over.\n"
                "### The page now\n" + _scrub(miss.get("snapshot") or ""))
        else:
            said.append(f"Ran '{name}' to the end ({body.get('done')} steps). What it read:\n"
                        + json.dumps(body.get("outputs") or {}, ensure_ascii=False, indent=1)[:20000])
        return _result("\n".join(said), error=bool(body.get("miss")))

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

    async def park_turn(self, key: str, handover: dict, **where) -> None:
        """Park a turn that no agent call ended on (a Browser step's, F1146), as `take_handover` parks one."""
        self._handovers[key] = handover
        await self.take_handover(key, **where)

    # ── the Browser step's replay (F1146) ────────────────────────────────────
    async def replay_for_step(self, key: str, doc: dict, steps: list[dict], inputs: dict, *,
                              carry_on: bool = False) -> dict:
        """A Browser step's recording, replayed in the run's browser (opened with `settings`): never watched, so
        only the steps allowed to send on their own send (Naveen 2026-10-29). Its answer, with `session_id`."""
        host, sid = await self._session_for(key)
        if carry_on:
            status, body = await _replay_polled(host, sid, "/replay/continue", {"session_id": sid})
        else:
            status, body = await _replay_polled(host, sid, "/replay", {
                "session_id": sid, "steps": steps, "inputs": inputs, "recording_id": doc["id"], "unattended": True})
        if status >= 400:
            body = {"refused": body.get("error") or "The recording could not run."}
        return {**body, "session_id": sid}

    async def latest_replay(self, key: str) -> dict | None:
        """The run's browser's last replay, as it ended (after an agent carried it on in its own calls)."""
        record = await self.store.get_owner(key)
        host = await self.hosts.host_for(key, opening=False) if record and record.get("sid") else None
        if host is None:
            return None
        status, body = await host.call("GET", "/replay/status", params={"session_id": record["sid"]}, timeout=30)
        return {**body, "session_id": record["sid"]} if status == 200 and not body.get("running") else None

    async def hand_to_person(self, key: str, reason: str) -> dict:
        """The person's turn in the run's browser, a missed step to find (on_miss "pause"): what they do is kept as
        that step's fix when the replay carries on."""
        host, sid = await self._session_for(key)
        status, body = await host.call("POST", "/tool", json={"session_id": sid, "name": gate.HAND_OVER,
                                                              "args": {"reason": reason}, "press": None})
        if status != 200 or not body.get("handover"):
            raise RuntimeError(body.get("refused") or body.get("error") or "the person could not be given the browser")
        return body["handover"]

    async def take_parked(self, key: str | None) -> dict | None:
        """The parked turn the person just answered, once."""
        record = await self.store.get_owner(key) if key else None
        parked = (record or {}).get("parked")
        if parked:
            await self.store.set_owner(key, {**record, "parked": None})
            await self.hosts.resume(key)
        return parked

    async def keep_session(self, session_id: str, *, site_session_id: str | None, site: str,
                           owner: str) -> dict | None:
        """Keep what the person just signed in to on `site`, under `site_session_id` (signing in again) or a new one;
        its row (`{id, site, expires_at}`), or None when nothing on that site was signed in. Only that site's cookies
        and storage are kept: a cookie only ever goes to its own site."""
        key = await self.store.owner_of(session_id)
        host = await self.hosts.host_for(key, opening=False) if key else None
        if host is None:
            return None
        status, body = await host.call("GET", "/login-state", params={"session_id": session_id})
        state = _for_site(body.get("login_state") if status == 200 else None, site)
        if not state:
            return None
        store = _logins()
        kept = await store.keep(site_session_id, state=state, site=site, owner=owner)
        row = await store.get(kept) if hasattr(store, "get") else None
        return {"id": kept, "site": (row or {}).get("site") or site, "expires_at": (row or {}).get("expires_at")}

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

    async def open_record_session(self, recording_id: str, *, sessions: list[str], start_url: str = "",
                                  replay_first: list[dict] | None = None) -> str:
        """"Record a task" (F1142): a browser that is the person's alone, signed in with the picked sessions, recording
        from the start; `start_url`, when given, is its first step. Re-recording an existing recording from a step
        (F1143), `replay_first` is the steps before it: they replay, then recording starts; `ReplayStopped` (the
        browser stays open) when they can't all run."""
        key = f"record:{recording_id}"
        if await self.store.get_owner(key) is not None:
            raise RecordingOpen(recording_id)
        state, _expired, loaded, skipped = await _sessions_state(list(sessions or []), {s: "record" for s in sessions or []})
        opening = {"owner": key, "mode": gate.CO_DRIVE, "person_only": True, "limits": {"actions": 1}}
        if state:
            opening["login_state"] = state
        host = await self.hosts.host_for(key, opening=True)
        status, body = await host.call("POST", "/session", json=opening)
        if status != 200 or not body.get("session_id"):
            raise RuntimeError(body.get("error") or "the browser did not open")
        sid = body["session_id"]
        await self.store.set_owner(key, {"sid": sid, "parked": None, "expired": None, "sessions": None})
        self._events.setdefault(key, []).append({"type": "browser_session", "session_id": sid, "owner": key,
                                                 "sessions": loaded, "skipped": skipped})
        status, started = await host.viewer("/record/start", params={"session_id": sid},
                                            json={"replay_first": replay_first} if replay_first else {})
        if status == 409 and started.get("replayed"):
            raise ReplayStopped(sid, started["replayed"])
        if start_url:
            await host.viewer("/input", params={"session_id": sid}, json={"type": "nav", "url": start_url})
        return sid

    async def finish_session(self, session_id: str, *, keep: bool, owner: str) -> tuple[str, dict | None] | None:
        """End a "Keep me signed in" browser: `(site, row)`, the row None when nothing on the site was signed in (the
        browser then stays open, so the person can try again); None when this is no such browser."""
        key = await self.store.owner_of(session_id)
        record = await self.store.get_owner(key) if key else None
        if not record or not record.get("sessions"):
            return None
        site = record["sessions"][0]
        if not keep:
            await self.close_owner(key)
            return site, None
        row = await self.keep_session(session_id, site_session_id=None, site=site, owner=owner)
        if row is not None:
            await self.close_owner(key)
        return site, row

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
        picked = list(chosen.get("sessions") or ([chosen["login_ref"]] if chosen.get("login_ref") else []))
        state, expired, loaded, skipped = await _sessions_state(picked, chosen.get("levels") or {})
        opening = {"owner": key, "mode": chosen.get("mode") or gate.READ_ONLY, "sites": chosen.get("sites") or [],
                   "limits": {"actions": int(chosen.get("action_budget") or 40)}}
        if state:
            opening["login_state"] = state
        if expired:
            opening["turn"] = {"kind": "session_expired", "reason": _expired_words(expired["site"]),
                               "site": expired["site"], "site_session_id": expired["site_session_id"], "mine": True}
        host = await self.hosts.host_for(key, opening=True)
        status, body = await host.call("POST", "/session", json=opening)
        if status != 200 or not body.get("session_id"):
            raise RuntimeError(body.get("error") or "the browser did not open")
        sid = body["session_id"]
        await self.store.set_owner(key, {
            "sid": sid, "parked": None, "sessions": None,
            "expired": expired})
        self._events.setdefault(key, []).append({"type": "browser_session", "session_id": sid, "owner": key,
                                                 "sessions": loaded, "skipped": skipped})
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

    async def idle(self, key: str) -> None:
        """The agent's turn or step ended: its browser shows nobody driving until the agent's next call. Best
        effort; a person's turn is left as it is."""
        try:
            record = await self.store.get_owner(key)
            host = await self.hosts.host_for(key, opening=False) if record and record.get("sid") else None
            if host is not None:
                await host.call("POST", "/idle", json={"session_id": record["sid"]}, timeout=15)
        except Exception:
            pass

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
    """Where kept sessions live: async `state_for(id, *, for_user)` and `keep(id_or_None, *, state, site, owner)`
    (optional `get(id)` for its row: site, expires_at, expired)."""
    global _login_store
    _login_store = store


def _logins():
    if _login_store is not None:
        return _login_store
    from core import site_sessions
    return site_sessions


_SCHEMAS = Path(__file__).with_name("tool_schemas.json")


def _committed_tool_schemas() -> list[dict] | None:
    """The gate's tools as pinned Playwright MCP lists them, committed beside this module (F1135): listing needs no
    Node and no browser. `confirm` and `browser_hand_over` are the component's own, added on top."""
    import json
    try:
        listed = json.loads(_SCHEMAS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    from core.browser.host import _HAND_OVER_SCHEMA, _RUN_RECORDING_SCHEMA, _with_confirm_schema
    return [{"name": t["name"], "description": t["description"], "inputSchema": _with_confirm_schema(t["name"],
             t["inputSchema"]), "tags": gate.ALLOWED[t["name"]]} for t in listed if t["name"] in gate.ALLOWED] \
        + [_HAND_OVER_SCHEMA, _RUN_RECORDING_SCHEMA]


def _npx_hint() -> str:
    import shutil
    return "npx is not on this backend's PATH (Node is needed)" if shutil.which("npx") is None else "see the log"


def _expired_words(site: str) -> str:
    return f"The kept session for {site} has expired: sign in again, then hand the browser back."


async def _sessions_state(picked: list[str], levels: dict | None = None):
    """`(state, expired, loaded, skipped)` for the picked sessions: their state merged into one (each cookie already
    scoped to its own site); the first that expired and is the person's (`{site_session_id, site}`), for a "sign in
    again" turn; `[{id, site, level}]` of those loaded; `[{site, reason: expired|not_shared, owner_name}]` of those
    left out, so the pane can say who's signed in and why one wasn't (F1140)."""
    store, who, levels = _logins(), for_user.get(), levels or {}
    cookies, origins, expired, loaded, skipped = [], [], None, [], []
    for sid in picked:
        try:
            state = await store.state_for(sid, for_user=who)
        except Exception:
            state = None
        try:
            row = await store.get(sid) if hasattr(store, "get") else None
        except Exception:
            row = None
        site = (row or {}).get("site") or ""
        if state:
            cookies += [c for c in state.get("cookies") or [] if isinstance(c, dict)]
            origins += [o for o in state.get("origins") or [] if isinstance(o, dict)]
            loaded.append({"id": sid, "site": site, "level": levels.get(sid, "agent")})
            continue
        if not row:
            continue  # removed since it was picked
        mine = who is None or row.get("owner") == who
        if row.get("expired") and mine and expired is None:
            expired = {"site_session_id": sid, "site": site}
        skipped.append({"site": site, "reason": "expired" if row.get("expired") else "not_shared",
                        "owner_name": row.get("owner_name") or row.get("owner") or ""})
    state = {"cookies": cookies, "origins": origins} if cookies or origins else None
    return state, expired, loaded, skipped


def _for_site(state: dict | None, site: str) -> dict | None:
    """The part of a browser's storage state that belongs to `site`: its cookies and its origins' storage."""
    if not state or not site:
        return None
    from urllib.parse import urlparse

    from core.sites import same_site
    cookies = [c for c in state.get("cookies") or [] if isinstance(c, dict)
               and same_site(str(c.get("domain") or "").lstrip("."), site)]
    origins = [o for o in state.get("origins") or [] if isinstance(o, dict)
               and same_site(urlparse(str(o.get("origin") or "")).hostname or "", site)]
    return {"cookies": cookies, "origins": origins} if cookies or origins else None


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
