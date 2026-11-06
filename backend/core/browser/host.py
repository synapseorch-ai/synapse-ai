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
import hashlib
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

from core import egress_proxy
from core.browser import gate
from core.browser import replay as replaying
from core.browser.recording import TARGET_JS, Recorder, line_of, sensitive_kind

#: Playwright MCP, pinned: its tool names and output are part of the gate's contract.
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"

DEFAULT_LIMITS = {"actions": 40, "loads_per_min": 30, "idle_s": 600}
#: How long an agent's held send waits for the person before it is dropped and stays their turn.
HOLD_S = 600
_HOST_GAP_S = 1.0          # at most one page load a second per host
_VIEWPORT = {"width": 1280, "height": 800}
#: Chromium's switches: the debugging port Playwright MCP connects to, and the egress proxy's own (WebRTC only on
#: the proxied path, QUIC off; F1122).
_CHROMIUM_ARGS = ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", *egress_proxy.CHROMIUM_ARGS]


def _browser_proxy_env() -> str | None:
    """The host's own proxy, when the environment names one (cloud's I1, F580): it is then the browser's outer layer.
    `NO_PROXY` is not read: it names the platform's hosts, never a page. As the scraper does (`tools/web_scraper`)."""
    import urllib.request

    found = urllib.request.getproxies_environment()
    return found.get("https") or found.get("http") or None
#: The element a press lands on, as `gate.classify_press` reads it. With `enter`, what Enter does there: it submits
#: a form from a one-line field (a message box's Enter is read by `editable`).
_PRESS_INFO_JS = """(el, enter) => {
  const pressable = 'button, [role=button], input[type=submit], input[type=button], input[type=image], input[type=reset], a, [role=link], [role=menuitem]';
  const tagOf = (x) => ((x && x.tagName) || '').toLowerCase();
  const typeOf = (x) => ((x && x.getAttribute && x.getAttribute('type')) || '').toLowerCase();
  const isField = (x) => !!x && (x.isContentEditable || ['textarea', 'select'].includes(tagOf(x)) || (tagOf(x) === 'input'
    && !['checkbox', 'radio', 'button', 'submit', 'image', 'reset', 'file'].includes(typeOf(x))));
  const button = (el && el.closest && el.closest(pressable)) || null;
  const b = button || el || document.body;
  const tag = tagOf(b), type = typeOf(b), field = !button && isField(b);
  const form = b.form || (b.closest && b.closest('form'));
  const own = (b.getAttribute && (b.getAttribute('aria-label') || b.getAttribute('title') || b.getAttribute('alt'))) || '';
  const label = (own || (button ? (b.innerText || b.value || '') : '')).trim().slice(0, 120);
  const oneLine = field && tag === 'input';
  const submits = !!form && (enter && !button ? oneLine : ((tag === 'button' && (type === '' || type === 'submit'))
                                                          || (tag === 'input' && (type === 'submit' || type === 'image'))));
  return JSON.stringify({label, tag, type, href: tag === 'a' ? (b.getAttribute('href') || '') : '',
    in_form: !!form, submits, form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : '',
    form_search: !!form && ((form.getAttribute('role') || '') === 'search' || !!form.querySelector('input[type=search]')),
    field, editable: field && tag !== 'select', nothing: !!enter && !button && !field,
    frame: location.href, title: document.title});
}"""
#: A worker's own WebSocket passes no socket route, so a read-only browser, which opens no socket, starts no worker
#: (F1122, SEC2's review). Run in every frame before its scripts; the page cannot define them again. A popup is a new
#: target that Playwright MCP, a second client of this Chromium, may let run before this script reaches it, so the
#: page gets no handle on one: `window.open` opens it `noopener` and returns null, as a blocked popup does.
_NO_WORKERS = """(() => { for (const name of ["Worker", "SharedWorker"]) {
  try { Object.defineProperty(globalThis, name, {value: undefined, writable: false, configurable: false}); }
  catch (e) {} }
  const open = window.open;
  try { Object.defineProperty(window, "open", {writable: false, configurable: false, value: function (url, target, features) {
    open.call(window, url, target, (features ? features + "," : "") + "noopener"); return null; }}); } catch (e) {}
})();"""
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


def _result_json(text: str):
    """The value a `browser_evaluate` returned, from Playwright MCP's answer (`### Result`, then it as JSON)."""
    found = re.search(r"### Result\s*\n(.*?)(?:\n###|\Z)", text or "", re.S)
    raw = "\n".join(line for line in (found.group(1) if found else text or "").strip().splitlines()
                    if not line.startswith("```")).strip()
    for _ in range(2):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
        if not isinstance(raw, str):
            return raw
    return None


#: What the agent passes to press a button that acts for the person (V2-CONTRACT "Final buttons and sends").
_CONFIRM = {"type": "string", "description": (
    "Only to press a button that acts for the person (submit, send, post, book…) after Synapse stopped it: the "
    "person's exact words that ask for it, copied from their message or your instructions. Leave it out otherwise.")}
_HAND_OVER_SCHEMA = {"name": "browser_hand_over", "description": (
    "Give the person the browser: they press a button themselves, choose, or log in. Say what they should do."),
    "inputSchema": {"type": "object", "properties": {"reason": {
        "type": "string", "description": "What the person should do, in one sentence."}}, "required": ["reason"]},
    "tags": {"read_only": True, "open_world": False}}


_RUN_RECORDING_SCHEMA = {"name": "browser_run_recording", "description": (
    "Run one of your recordings by code: a task the person did once, replayed step by step in this browser. Name "
    "it as your instructions list it, with its inputs. It returns what it read, or stops at a step the page no "
    "longer matches: find the way for that step with your browser tools, then call again with carry_on."),
    "inputSchema": {"type": "object", "properties": {
        "recording": {"type": "string", "description": "The recording, as your instructions name it."},
        "inputs": {"type": "object", "description": "Its inputs by name, e.g. {\"order number\": \"A-1001\"}."},
        "carry_on": {"type": "boolean", "description": (
            "True after it stopped: you found the way for its step (or the person did), so it goes on.")}},
        "required": ["recording"]},
    "tags": {"open_world": True}}


def _with_confirm_schema(name: str, input_schema: dict | None) -> dict:
    """A tool's schema as an agent is offered it: a press tool's with `confirm` beside Playwright MCP's own arguments
    (which never receive it), and none with an argument that writes a file the agent can't read back (F1340)."""
    schema = dict(input_schema or {})
    if gate.WRITES_A_FILE & set(schema.get("properties") or {}):
        schema["properties"] = {k: v for k, v in schema["properties"].items() if k not in gate.WRITES_A_FILE}
        if schema.get("required"):
            schema["required"] = [k for k in schema["required"] if k not in gate.WRITES_A_FILE]
    if name in gate.PRESSES:
        schema["properties"] = {**(schema.get("properties") or {}), "confirm": _CONFIRM}
    return schema


def _with_confirm(tool) -> dict:
    return _with_confirm_schema(tool.name, tool.inputSchema)


class Session:
    _nav_watch: "_NavWatch | None" = None
    #: The person took over unasked: the page they hand back is read before anything changes it (F1417).
    took_unasked = False

    def __init__(self, sid: str, owner: str, mode: str, limits: dict, root: Path, sites: list[str] | None = None,
                 wheel: str = "agent"):
        self.id = sid
        self.owner = owner
        self.mode = mode if mode in gate.MODES else gate.READ_ONLY
        self.sites = list(sites or [])
        self.typing_hosts: set[str] = set()            # allowed once by the person this session
        self.held: dict | None = None                  # an agent's send paused at the network
        self.banner: dict | None = None
        # The page that did not open, and why (F1425): the pane says it above the page, never a blank pane. Apart
        # from the banner, which says who drives.
        self.load_error: dict | None = None
        self._doc: tuple[str, int] | None = None       # the main frame's last document: its URL and status
        self._refused_top: str | None = None           # the guard's reason for the main frame's last refused page
        self.handover: dict | None = None              # what the agent's current call must hand over
        self.handback: str | None = None               # what the agent hears on its next call
        self.took_at: float | None = None
        self.last_frame: dict | None = None
        self.dialog = None          # a page's alert/confirm/prompt, while the person drives
        self.agent_dialog = ""      # the message of the dialog the agent faces (Playwright MCP handles it)
        self.idle = False           # the agent's turn ended: "nobody" until its next call, not a held send
        self.recorder: Recorder | None = None  # the person's task being recorded (F1141)
        self.replaying: dict | None = None     # the replay in hand: its steps, inputs and whether anyone is there
        self.replay_sends: set[str] | None = None  # unattended: the only sends it may make, as its recording did
        self.replay_blocked: tuple | None = None   # a send the list kept back during a replay, and at which step
        self.fixing: dict | None = None        # a missed step being found again (F1144): its capture and allowance
        # A replay run as a task, started and then polled (a long request may not survive a proxy, an idle cutoff
        # or a restart, BRW3): one at a time; its answer is kept until the next starts.
        self.replay_task: asyncio.Task | None = None
        self.replay_id: str | None = None
        self.replay_answer: dict | None = None
        self.replay_at: int | None = None
        self.replay_running = False  # a replay drives now: the pane says the recording, not the agent (F1363)
        self._pressing: dict | None = None  # a press the person's own words allowed, logged once it runs
        self.select: dict | None = None  # a dropdown the person clicked: the pane draws its options
        self.chooser = None         # a page's file picker, while the person drives
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.dir = root / sid
        self.log: list[dict] = []
        self.shots: dict[str, bytes] = {}
        self.actions = 0
        self.loads: deque[float] = deque()
        self.host_last: dict[str, float] = {}
        self.last_used = time.monotonic()
        self.wheel = wheel
        self.viewers: set[asyncio.Queue] = set()
        self.ctx = None
        self.page = None
        self.mcp = None
        self._cdp = None
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self, pw, *, chromium_sandbox: bool | None = None) -> None:
        await self.launch(pw, chromium_sandbox=chromium_sandbox)
        await self.apply_mode()

    def adopt(self, sid: str, owner: str, mode: str, limits: dict, sites: list[str] | None, wheel: str) -> None:
        """A prelaunched browser becomes this session (F1135): its Chromium and Playwright MCP are already up, and
        nothing has loaded in it yet."""
        self.id, self.owner = sid, owner
        self.mode = mode if mode in gate.MODES else gate.READ_ONLY
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.sites, self.wheel = list(sites or []), wheel
        self.last_used = time.monotonic()

    async def apply_mode(self) -> None:
        """What the session's mode adds to its browser, before any page of the session's loads."""
        if self.mode != gate.CO_DRIVE:
            await self.ctx.add_init_script(_NO_WORKERS)

    async def launch(self, pw, *, chromium_sandbox: bool | None = None) -> None:
        """Chromium behind the guards, and Playwright MCP connected to it; the mode's own parts come after."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # No service workers: a worker's own fetches would not pass the route below. Every connection the browser
        # makes goes through the egress proxy (SEC2, F1065/F1109), the outer layer under the route and socket
        # guards: it dials the address the guard checked, so a name that changes its answer reaches nothing new.
        proxy = _browser_proxy_env() or (await egress_proxy.shared()).url
        sandbox = {} if chromium_sandbox is None else {"chromium_sandbox": chromium_sandbox}
        self.ctx = await pw.chromium.launch_persistent_context(
            str(self.dir / "profile"), headless=True, viewport=_VIEWPORT, args=_CHROMIUM_ARGS,
            env=_clean_env(), service_workers="block", proxy={"server": proxy}, **sandbox,
        )
        await self.ctx.route("**/*", self._route)
        await self.ctx.route_web_socket(lambda _url: True, self._route_socket)
        self.ctx.on("page", self._on_page)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self._watch(self.page)
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

    async def load_login(self, state: dict) -> None:
        """A saved website login (cookies and localStorage), loaded before the first page."""
        cookies = [c for c in (state.get("cookies") or []) if isinstance(c, dict)]
        if cookies:
            await self.ctx.add_cookies(cookies)
        stored = {o.get("origin"): [[i.get("name"), i.get("value")] for i in (o.get("localStorage") or [])]
                  for o in (state.get("origins") or []) if isinstance(o, dict) and o.get("origin")}
        if stored:
            await self.ctx.add_init_script(
                "(() => { const want = " + json.dumps(stored) + "; const items = want[location.origin];"
                " if (!items) return; try { for (const [k, v] of items) {"
                " if (localStorage.getItem(k) === null) localStorage.setItem(k, v); } } catch (e) {} })()")

    async def login_state(self) -> dict:
        try:
            return await self.ctx.storage_state()
        except Exception:
            return {}

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
        if self.replay_task is not None and not self.replay_task.done():
            self.replay_task.cancel()
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
    def gates_as_agent(self) -> bool:
        """Whether what the page sends now is held to the agent's rules: its wheel, or its idle browser between its
        calls (F1376: a replay that ran to its end goes idle, and an unattended replay's send list, its sites and the
        money backstop hold for what the page sends late, as they did while it drove)."""
        return self.wheel == "agent" or (self.wheel == "nobody" and self.idle)

    async def _route(self, route) -> None:
        req = route.request
        # As the request starts, before any wait: the replay's send list, and whether a replay is running.
        agent, only, replaying = self.gates_as_agent(), self.replay_sends, self.replaying is not None
        top = False
        try:
            nav = req.is_navigation_request()
            top = nav and _is_main_frame(req)
            if top and self._nav_watch is not None:
                self._nav_watch.requested.set()  # the person's step leads somewhere: its picture waits for it
            reason = await gate.verdict(req.url, req.method, self.mode,
                                        sites=self.sites if (agent and top) else None)
            if (reason is None and agent and only is not None
                    and req.method.upper() not in ("GET", "HEAD", "OPTIONS")
                    and f"{req.method.upper()} {urlparse(req.url).hostname or ''}" not in only):
                reason = "an unattended replay sends only where its recording did"
                if replaying:  # the step that caused it becomes the person's turn (Naveen 2026-10-29)
                    self.replay_blocked = (f"{req.method.upper()} {urlparse(req.url).hostname or ''}", self.replay_at)
            if reason is None and nav:
                reason = await self._pace(urlparse(req.url).hostname or "")
            hold = (reason is None and agent and self.mode == gate.CO_DRIVE
                    and gate.is_money_send(req.method, req.url, page_url=self._page_url()))
            if reason is None and self.wheel == "person" and req.method.upper() not in ("GET", "HEAD", "OPTIONS"):
                # The record of who sent what (F1125): the method and the host, never the body or its fields.
                self._note("person", "sent", f"{req.method.upper()} {urlparse(req.url).hostname or ''}")
                if self.recorder is not None:
                    self.recorder.sent(req.method, urlparse(req.url).hostname or "")
        except Exception:
            reason, hold = "the browser's guard failed", False
        if hold:
            await self._hold(route, req)
            return
        try:
            if reason is None:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
                self._note("guard", "blocked", reason)
                if top:
                    self._refused_top = reason
        except Exception:
            pass  # the page or the request is already gone

    async def _route_socket(self, ws) -> None:
        """A page's WebSocket, checked before it opens (F1122): the route above never sees one. Unless the person
        drives, it opens only on the agent's sites."""
        scope = None if self.wheel == "person" else [*self.sites, *self.typing_hosts]
        try:
            reason = await gate.socket_verdict(ws.url, self.mode, scope=scope)
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

    async def _hold(self, route, req) -> None:
        """The backstop behind the click stop (F1125): a payment or checkout send the agent's press caused waits here
        for the person, who takes over and sends it themselves. Unanswered, it is dropped and stays their turn."""
        host = urlparse(req.url).hostname or "the site"
        event = asyncio.Event()
        self.held = {"method": req.method.upper(), "url": req.url, "event": event, "decision": None, "money": True}
        reason = f"The agent's press would send a payment or an order to {host}. Nothing has gone."
        self.banner = {"kind": "send", "reason": reason,
                       "held": {"method": req.method.upper(), "url": req.url, "money": True}}
        self.handover = {"kind": "send", "reason": reason}
        self.wheel, self.idle = "nobody", False  # a held send is the person's, never an idle browser to take back
        self._note("guard", "held", f"a {req.method.upper()} to {host}")
        self._push_state()
        try:
            await asyncio.wait_for(event.wait(), timeout=HOLD_S)
        except asyncio.TimeoutError:
            pass
        decision = self.held.get("decision") if self.held else None
        self.held = None
        try:
            if decision == "release":
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
        except Exception:
            pass
        if decision is None:  # nobody answered: it stays the person's turn, with nothing held
            self.wheel = "person"
            self.banner = {"kind": "send", "reason": f"Your turn: check the page on {host} and press its "
                                                     "Submit yourself."}
            self._push_state()

    def _page_url(self) -> str:
        try:
            return self.page.url if self.page else ""
        except Exception:
            return ""

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
    async def call(self, name: str, args: dict, press: dict | None = None) -> dict:
        """`press` is the engine's word on a quote the agent gave (`confirm`): `{because, confirmed}`, checked
        against the person's own text where that text is, never here."""
        self.last_used = time.monotonic()
        self.replay_sends = None  # a replay's send list was its own; the agent's presses go by the click stop
        args = {k: v for k, v in (args or {}).items() if k != "confirm"}
        refused = gate.refusal(name)
        if refused:
            return {"refused": refused}
        if self.wheel == "nobody" and self.idle:  # the agent's next turn takes its idle browser back
            self.wheel, self.idle = "agent", False
            self._push_state()
        if self.wheel != "agent":
            return {"waiting": "person_driving"}
        if self.actions >= self.limits["actions"]:
            return {"refused": f"This browser session has used its {self.limits['actions']} actions."}
        if name == gate.RUN_RECORDING:
            return {"refused": "Recordings run through Synapse, not the page."}
        if name == gate.HAND_OVER:
            reason = str(args.get("reason") or "").strip()[:300] or "The agent asked you to take it from here."
            turn = self._turn("asked", reason)
            self._note("agent", "handed over", reason)
            return {"text": "The person has the browser now. Stop here; you will continue when they hand it back.",
                    "is_error": False, "log": None, "handover": {**turn, "session_id": self.id}}
        async with self._lock:
            held = None
            if self.handback and self.took_unasked and name in gate.CHANGES_PAGE:
                # Written before the agent saw the page the person took over unasked and handed back: that page is
                # read instead, and the call is not made (F1417).
                held, name, args = name, "browser_snapshot", {}
            if name in gate.TYPES_TEXT:
                stop = await self._before_typing(name, args or {})
                if stop:
                    return stop
            if name in gate.PRESSES and self.mode == gate.CO_DRIVE:  # read-only sends nothing at all
                stop = await self._before_press(name, args, press)
                if stop:
                    return stop
            if held is None:
                self.actions += 1
            seen = len(self.log)
            self.handover = None
            fix_targets = await self._fix_targets(name, args) if self.fixing is not None else None
            fix_from = self._page_url()
            result = await self.mcp.call_tool(name, args)
            if fix_targets is not None and not result.isError:
                self._fix_capture(name, args, fix_targets, fix_from)
            text = self._inline_snapshots("\n".join(getattr(c, "text", "") for c in result.content
                                                    if getattr(c, "type", "") == "text"))
            blocked = [e["target"] for e in self.log[seen:] if e["actor"] == "guard"]
            if blocked:
                text += ("\n### Synapse\nNothing left the browser for: " + "; ".join(dict.fromkeys(blocked))
                         + ". The page may show an error because of it.")
            entry = None
            pressing, self._pressing = self._pressing, None
            if pressing:
                entry = await self._record("agent", "pressed", pressing["label"], because=pressing["because"])
            elif name in gate.CHANGES_PAGE:
                entry = await self._record("agent", name, _summary(name, args or {}))
                if self.handover is None and self._captcha_showing():
                    self._turn("captcha", "A CAPTCHA or a bot check is showing: it is yours to solve.")
            if self.handback:
                said = (f"### Synapse\nYour {held} was not made: it was written before the person handed the browser "
                        "back. This is the page as they left it; act on it next.\n" if held else "")
                text = f"### The person\n{self.handback}\n{said}" + text
                self.handback, self.took_unasked = None, False
        out ={"text": text, "is_error": bool(result.isError), "log": entry,
               "notes": [e for e in self.log[seen:] if e["actor"] != "agent"]}
        if self.handover:
            out["handover"] = {**self.handover, "session_id": self.id}
        return out

    def _turn(self, kind: str, reason: str, **extra) -> dict:
        """Hand the wheel to the person, and tell the agent's call it must stop here."""
        self.wheel = "nobody" if kind == "send" else "person"
        self.banner = {"kind": kind, "reason": reason, **extra}
        self.handover = {"kind": kind, "reason": reason, **extra}
        self._push_state()
        return self.handover

    async def _before_typing(self, name: str, args: dict) -> dict | None:
        """Typing goes only where it may: a listed site or one the person allowed, and never into a
        password, card or one-time-code field (DESIGN.md §2.4, §3.3)."""
        host = (urlparse(self._page_url()).hostname or "").lower()
        allowed = gate.site_allowed(host, self.sites) if self.sites else host in self.typing_hosts
        if not allowed:
            reason = f"The agent wants to type on {host or 'this page'}."
            turn = self._turn("allow_typing", reason, host=host)
            return {"text": f"Typing on {host} needs the person's OK first; they have been asked. Stop here.",
                    "is_error": True, "log": None, "handover": {**turn, "session_id": self.id}}
        targets = ([f.get("target") for f in (args.get("fields") or [])] if name == "browser_fill_form"
                   else [args.get("target")])
        for target in [t for t in targets if t]:
            if await self._sensitive(target):
                turn = self._turn("sensitive", "This field is yours: a password, a card or a one-time code.")
                return {"text": "That field is the person's to fill (a password, a card or a one-time code); "
                                "they have been asked. Stop here.",
                        "is_error": True, "log": None, "handover": {**turn, "session_id": self.id}}
        return None

    async def _before_press(self, name: str, args: dict, press: dict | None) -> dict | None:
        """A press that acts for the person runs only on their own words; money, permanent deletes and closing an
        account are always theirs; anything else runs (V2-CONTRACT "Final buttons and sends")."""
        if name == "browser_type" and not args.get("submit"):
            return None
        if name == "browser_press_key" and str(args.get("key") or "") not in ("Enter", "NumpadEnter"):
            return None
        if name == "browser_handle_dialog":
            if not args.get("accept"):
                return None
            info = {"label": self.agent_dialog, "dialog": True}
        else:
            info = await self._press_info(None if name == "browser_press_key" else args.get("target"),
                                          key=name != "browser_click")
            if info is None:
                return None  # no such element: Playwright MCP's own call says so
        page = self._page_url()
        tier, does = gate.classify_press(info, page)
        if tier == gate.FREE:
            return None
        label = (info.get("label") or "").strip()[:120]
        label = (f"Enter in {label}" if label else "Enter") if info.get("key") else (label or "this button")
        if tier == gate.ACTS and press and press.get("confirmed"):
            self._pressing = {"label": label, "because": str(press.get("because") or "")[:300]}
            return None
        redo = self._fix_redo(page) if tier == gate.ACTS else None
        if redo:  # finding a missed step again: what the person pressed there, once (F1144)
            self._pressing = {"label": label, "because": redo}
            return None
        detail = {"label": label, "does": does, "host": urlparse(page).hostname or "", "tier": tier}
        if tier == gate.MONEY or press:
            why = ("Payments, permanent deletes and closing an account are always the person's"
                   if tier == gate.MONEY else "the words quoted are not the person's own asking for it")
            turn = self._turn("press", f"Press '{label}' yourself: {does}.", press=detail)
            return {"text": f"Not pressed: '{label}' is the person's to press ({why}). They have been asked; "
                            "stop here.", "is_error": True, "log": None,
                    "handover": {**turn, "session_id": self.id}}
        return {"text": (f"Not pressed: '{label}': {does}. Press it only if the person asked for this in their "
                         f"own words: call {name} again with the same arguments and `confirm` set to their exact "
                         "words. If they did not, call browser_hand_over so they press it themselves."),
                "is_error": False, "log": None}

    async def _press_info(self, target: str | None, *, key: bool) -> dict | None:
        """What the page says about the element a press lands on, read through Playwright MCP's ref (or the
        focused element, for Enter). None when there is no such element; unreadable fails closed."""
        fn = _PRESS_INFO_JS if target else f"() => ({_PRESS_INFO_JS})(document.activeElement, true)"
        call = {"function": fn, **({"element": "the element pressed", "target": target} if target else {})}
        try:
            res = await self.mcp.call_tool("browser_evaluate", call)
            text = " ".join(getattr(c, "text", "") for c in res.content)
            if res.isError:
                if "not found" in text.lower() or "does not match" in text.lower():
                    return None
                return {"label": "", "submits": True, "form_method": "post", "key": key}
            info = _result_json(text)
        except Exception:
            info = None
        if not isinstance(info, dict):
            return {"label": "", "submits": True, "form_method": "post", "key": key}
        info["key"] = key
        return info

    async def _note_persons_press(self, page, x: float, y: float) -> None:
        """The record of who pressed what (F1125): the person's click on a button that acts or pays, by its label."""
        try:
            raw = await page.evaluate(
                "([x, y]) => { const el = document.elementFromPoint(x, y); if (!el) return null;"
                " if (el.tagName === 'IFRAME') return JSON.stringify({label: '', tag: 'iframe', frame: el.src});"
                f" return ({_PRESS_INFO_JS})(el, false); }}", [x, y])
            info = json.loads(raw) if raw else None
        except Exception:
            return
        if not isinstance(info, dict):
            return
        tier, _does = gate.classify_press(info, self._page_url())
        if tier != gate.FREE:
            self._note("person", "pressed", (info.get("label") or "").strip()[:120] or "a button", tier=tier)

    async def _sensitive(self, target: str) -> bool:
        """Read the field's own type and autocomplete through Playwright MCP's ref, never failing open."""
        try:
            res = await self.mcp.call_tool("browser_evaluate", {
                "element": "field", "target": target,
                "function": "(el) => JSON.stringify([el.type || '', el.getAttribute('autocomplete') || ''])"})
            text = " ".join(getattr(c, "text", "") for c in res.content)
            found = re.search(r'\[\\?"([^"\\]*)\\?",\s*\\?"([^"\\]*)\\?"\]', text)
            if res.isError or not found:
                return False if res.isError and "not found" in text.lower() else not found
            return gate.is_sensitive_field(found.group(1), found.group(2))
        except Exception:
            return True

    def _captcha_showing(self) -> bool:
        try:
            return any(gate.is_captcha_frame(f.url) for f in (self.page.frames if self.page else []))
        except Exception:
            return False

    # ── the person's turn ────────────────────────────────────────────────────
    def go_idle(self) -> dict:
        """The agent's turn or step ended (BRW2's live check): nobody drives until its next call or the person takes
        over. Only from the agent's wheel; a person's turn and a held send are left as they are."""
        if self.wheel == "agent":
            self.wheel, self.idle = "nobody", True
            self._push_state()
        return {"wheel": self.wheel}

    def take(self) -> dict:
        if self.held:
            self.held["decision"] = "take"
            self.held["event"].set()
        # Unasked: from the agent, or from an idle browser between its turns, never a turn it or Synapse gave (F1417).
        self.took_unasked = self.took_unasked or self.wheel == "agent" or (self.wheel == "nobody" and self.idle)
        self.wheel, self.idle = "person", False
        self.banner = None
        self.took_at = time.monotonic()
        self._note("person", "took over", "")
        self._push_state()
        return {"wheel": self.wheel}

    def hand_back(self, note: str = "") -> dict:
        drove = int(time.monotonic() - self.took_at) if self.took_at else 0
        self.took_at = None
        self.wheel = "agent"
        self.banner = None
        said = f' and said: "{note.strip()}"' if note and note.strip() else ""
        self.handback = (f"The person drove for {drove} s and handed the browser back{said}. "
                         f"The page is now {self._page_url()}. Read it before you act.")
        self._note("person", "handed back", f"after {drove} s")
        self._push_state()
        return {"wheel": self.wheel}

    def release(self) -> dict | None:
        """What the network still holds is a payment's or a checkout's (F1125), and those are the person's to send:
        Take over is the only way on."""
        if not self.held:
            return None
        if self.held.get("money"):
            return {"refused": "A payment is the person's: take over and press it yourself."}
        self.held["decision"] = "release"
        self.held["event"].set()
        self.wheel = "agent"
        self.banner = None
        self.handback = "The person let it go: it only loaded data. Carry on."
        self._note("person", "released", self.held["url"])
        self._push_state()
        return {"released": True}

    def allow_site(self, host: str) -> dict:
        host = (host or "").lower().strip()
        self.typing_hosts.add(host)
        if self.banner and self.banner.get("kind") == "allow_typing":
            self.banner = None
            self.wheel = "agent"
            self.handback = f"The person allowed typing on {host}. Carry on."
        self._push_state()
        return {"allowed": host}

    async def input(self, body: dict) -> dict | None:
        """The person's mouse, keys and address bar, while they hold the wheel. An action a page holds up
        (a click that opens an alert waits for the alert) answers `pending` after 3 s and finishes later."""
        if self.wheel != "person" or self.page is None:
            return None
        page = self.page
        kind = body.get("type")
        if kind == "mouse" and body.get("event", "click") in ("click", "down"):
            await self._note_persons_press(page, float(body.get("x", 0)), float(body.get("y", 0)))
        if kind == "mouse" and body.get("event", "click") == "click":
            x, y = float(body.get("x", 0)), float(body.get("y", 0))
            found = await page.evaluate(
                "([x, y]) => { const s = document.elementFromPoint(x, y)?.closest('select');"
                " if (!s || s.disabled) return null; return {options: [...s.options].map("
                "o => ({value: o.value, label: o.label || o.text})), selected: s.value}; }", [x, y])
            if found:  # the screencast draws no native dropdown: the pane draws this one
                self.select = {"target": f"{x:g},{y:g}", **found}
                self._push_state()
                return {"ok": True, "select": self.select}
        step = await self._record_input(kind, body) if self._rec() is not None else None
        watch = self._watch_navigation(page) if step is not None else None
        task = asyncio.ensure_future(self._act(page, kind, body))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except asyncio.TimeoutError:
            self._end_watch(watch)
            return {"ok": True, "pending": True}
        except Exception as exc:
            self._end_watch(watch)
            return {"ok": False, "error": str(exc)[:200]}
        self.last_used = time.monotonic()
        if step is not None:
            await self._picture(step, watch)
        return {"ok": True}

    def _watch_navigation(self, page) -> "_NavWatch":
        """Whether the step about to be acted leads to another page, and when that page is there (F1196)."""
        self._end_watch(self._nav_watch)
        self._nav_watch = _NavWatch(page)
        return self._nav_watch

    def _end_watch(self, watch: "_NavWatch | None") -> None:
        if watch is not None:
            watch.close()
            if self._nav_watch is watch:
                self._nav_watch = None

    # ── replaying a recording by code (F1143) ────────────────────────────────
    async def replay(self, steps: list[dict], inputs: dict, *, unattended: bool, recording_id: str = "",
                     from_n: int = 1) -> dict:
        """Run `steps` from step `from_n` (DESIGN §v5.3): `{done, outputs}`, or stopped at a step with `miss` (no
        target, or not the page it expects: for a fix) or `handover` (the person's step, or a press that is theirs).
        Refused while the person drives. It drives as the agent does, so the network gate and the money backstop
        hold as for any agent press."""
        if self.wheel == "person":
            return {"refused": "The person is driving this browser."}
        if self.recorder is not None:
            return {"refused": "This browser is recording."}
        if self.held is not None:
            return {"refused": "A send is waiting for the person."}
        self.wheel, self.idle, self.banner, self.handover = "agent", False, None, None
        self.replay_blocked = None
        self.replay_running = True
        self._push_state()
        try:
            answer = await self._replay(steps, inputs, unattended=unattended, recording_id=recording_id, from_n=from_n)
        finally:
            self.replay_running = False
            self._push_state()
        if "done" in answer and not any(k in answer for k in ("miss", "handover", "refused")):
            # Run to its end, it leaves nobody driving, as an agent's turn does (F1376): never "the agent". A stop
            # keeps the wheel where it put it: a miss the step's agent's to find the way, a turn the person's.
            self.go_idle()
        return answer

    async def _replay(self, steps: list[dict], inputs: dict, *, unattended: bool, recording_id: str,
                      from_n: int) -> dict:
        """`replay`'s own work, while the browser says a replay drives it (F1363)."""
        plan = self.replaying if (self.replaying and self.replaying["steps"] is steps) else None
        if plan is None:
            self.fixing = None
        self.replaying = plan or {"steps": steps, "inputs": inputs or {}, "unattended": unattended,
                                  "recording_id": recording_id, "outputs": {}}
        outputs = self.replaying["outputs"]
        # Unattended, the only sends are its recording's: kept past the replay's end (a page may send late) until the
        # agent's own next call, which has its own rules, or the next replay.
        self.replay_sends = ({x for st in steps for x in (st.get("sends") or [])} if unattended else None)
        for step in [st for st in steps if int(st.get("n") or 0) >= from_n]:
            self.replay_at = int(step.get("n") or 0)
            if self.wheel == "person":  # they took over during the step before: nothing more is done on their page
                return self._replay_taken(step)
            stopped = self._replay_held(step) or await self._replay_step(step, unattended)
            if stopped is not None:
                return stopped
        self.replaying, self.handover = None, None
        return {"done": len(steps), "outputs": outputs}

    async def _replay_step(self, step: dict, unattended: bool) -> dict | None:
        page, action, line = self.page, step.get("action"), step.get("line") or ""
        try:
            if action == "open":
                await page.goto(str(step.get("url") or ""), wait_until="domcontentloaded")
            elif action == "back":
                await page.go_back(wait_until="domcontentloaded")
            elif "person" in (step.get("value") or {}):
                return self._replay_turn(step, "sensitive", f"Your turn: {line}.")
            else:
                loc, _rung = await replaying.find(page, step.get("target"))
                if loc is None:
                    return await self._replay_miss(step, "its target is not on the page")
                if action in ("click", "press"):
                    info = json.loads(await loc.first.evaluate(f"(el) => ({_PRESS_INFO_JS})(el, {str(action == 'press').lower()})") or "{}")
                    info["key"] = action == "press"
                    tier, does = gate.classify_press(info, self._page_url())
                    why = replaying.press_rule(step, tier, unattended=unattended)
                    if why:
                        label = (info.get("label") or "").strip()[:120] or line
                        return self._replay_turn(step, "press", f"Press '{label}' yourself: {why}.",
                                                 press={"label": label, "does": does, "tier": tier,
                                                        "host": urlparse(self._page_url()).hostname or ""})
                    await (loc.first.click(no_wait_after=True) if action == "click"
                           else loc.first.press("Enter", no_wait_after=True))
                    await self._settle(page, "networkidle")  # what the press sent goes (or not) within its step
                elif action in ("type", "choose"):
                    value = replaying.fill_value(step, self.replaying["inputs"])
                    if value is None:
                        return await self._replay_miss(step, f"no value for {(step.get('value') or {}).get('input')}")
                    now = await loc.first.evaluate(f"(el) => ({TARGET_JS})(el)")
                    if sensitive_kind(now) or gate.looks_like_card(value):  # the page changed, or a card in an input
                        return self._replay_turn(step, "sensitive",
                                                 "This field is yours: a password, a card or a one-time code.")
                    await (loc.first.fill(value) if action == "type" else loc.first.select_option(label=value))
                    await self._settle(page)
                elif action == "read":
                    self.replaying["outputs"][str(step.get("output") or f"step_{step.get('n')}")] = \
                        await replaying.read(loc)
            want = (step.get("wait") or {}).get("url")
            if want:
                try:
                    await page.wait_for_url(lambda u: replaying.page_of(u) == want, timeout=replaying.WAIT_MS)
                except Exception:
                    return await self._replay_miss(step, f"it didn't reach {want}")
        except Exception as exc:
            return await self._replay_miss(step, str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__)
        held = self._replay_held(step)
        if held:
            return held
        await self._record("replay", action or "", line, recording_id=self.replaying.get("recording_id", ""),
                           step_id=step.get("id"))
        return None

    @staticmethod
    async def _settle(page, state: str = "domcontentloaded") -> None:
        try:
            await page.wait_for_load_state(state, timeout=replaying.SETTLE_MS)
        except Exception:
            pass

    def _replay_held(self, step: dict) -> dict | None:
        """The money backstop held a send the last step caused, or the send list kept one back: the person's turn,
        at that step."""
        blocked, self.replay_blocked = self.replay_blocked, None
        if blocked and self.held is None and not self.handover:
            said, at = blocked
            out = self._replay_turn(step, "send", f"The recording never sent a {said}, so Synapse kept it back. "
                                                  "If it is right, do that step yourself, then hand the browser back.")
            if at:  # the step that sent it, which may be the one before this check
                self.replaying["turn_at"] = at
                out["at"] = at
            return out
        if self.held is None and not self.handover:
            return None
        if self.replaying is not None:
            self.replaying["turn_at"] = step.get("n")
        return {"handover": {**(self.handover or {}), "session_id": self.id}, "at": step.get("n"),
                "step_id": step.get("id")}

    def _replay_taken(self, step: dict) -> dict:
        """The person took over (F1367): the step under way finished, as an action can't stop halfway, and the replay
        stops before this one, as their turn. Handed back, it carries on from this step (`turn_at`: the one before)."""
        n = int(step.get("n") or 1)
        if self.replaying is not None:
            self.replaying["turn_at"] = n - 1
        line = step.get("line") or "its next step"
        reason = (f"You took over, so the recording stopped before step {n}, \"{line}\". "
                  "Hand the browser back when you're done, and it carries on from that step.")
        return {"handover": {"kind": "took_over", "reason": reason, "session_id": self.id}, "at": n,
                "step_id": step.get("id")}

    def _replay_turn(self, step: dict, kind: str, reason: str, **extra) -> dict:
        turn = self._turn(kind, reason, **extra)
        if self.replaying is not None:
            self.replaying["turn_at"] = step.get("n")
        return {"handover": {**turn, "session_id": self.id}, "at": step.get("n"), "step_id": step.get("id")}

    async def _replay_miss(self, step: dict, why: str) -> dict:
        """A step that can't find its way: replay stops, with the page as it is, for the agent's fix or the person."""
        entry = await self._record("replay", "missed", f"Stopped at: {step.get('line') or 'a step'} ({why})",
                                   step_id=step.get("id"),
                                   recording_id=(self.replaying or {}).get("recording_id", ""))
        plan = self.replaying or {}
        # What follows, the agent's or the person's, is that step found again: kept as its fix (F1144). A press the
        # person made there may be redone once, on the same site, as recorded authority goes (attended, or allowed
        # to send on its own); never a payment or a delete, which stay theirs as everywhere.
        recorder = Recorder()
        recorder.at(self._page_url())
        self.fixing = {"step": step, "recorder": recorder, "site": urlparse(self._page_url()).hostname or "",
                       "redo": 1 if step.get("action") in ("click", "press") and (
                           not plan.get("unattended") or step.get("may_send")) else 0}
        snapshot = ""
        try:
            res = await self.mcp.call_tool("browser_snapshot", {})
            snapshot = self._inline_snapshots("\n".join(getattr(c, "text", "") for c in res.content))[:20000]
        except Exception:
            pass
        return {"miss": {"n": step.get("n"), "step_id": step.get("id"), "line": step.get("line"), "why": why,
                         "snapshot": snapshot, "shot_ref": entry.get("shot_ref")}, "done": int(step.get("n") or 1) - 1}

    # ── a missed step found again (F1144) ────────────────────────────────────
    def _rec(self) -> Recorder | None:
        """Where the person's input becomes steps: the recording, or a missed step's fix."""
        return self.recorder if self.recorder is not None else (self.fixing or {}).get("recorder")

    def _fix_redo(self, page_url: str) -> str | None:
        """The fix's one redo of what the person pressed at the missed step: why it may run, or None. Spent here."""
        fixing = self.fixing
        if not fixing or fixing.get("redo", 0) < 1:
            return None
        if not gate.same_site(urlparse(page_url).hostname or "", fixing["site"]):
            return None
        fixing["redo"] -= 1
        return f"the person pressed this when recording ({fixing['step'].get('line') or 'its step'})"

    async def _fix_targets(self, name: str, args: dict) -> list:
        """The elements an agent's action lands on, read before it acts (a press may take them away)."""
        refs = ([f.get("target") for f in (args.get("fields") or [])] if name == "browser_fill_form"
                else [args.get("target")] if name in ("browser_click", "browser_type", "browser_select_option")
                else [])
        out = []
        for ref in refs:
            target = None
            if ref:
                try:
                    res = await self.mcp.call_tool("browser_evaluate", {
                        "function": f"(el) => ({TARGET_JS})(el)", "element": "the element acted on", "target": ref})
                    target = None if res.isError else _result_json(
                        " ".join(getattr(c, "text", "") for c in res.content))
                except Exception:
                    target = None
            out.append(target if isinstance(target, dict) else None)
        return out

    def _fix_capture(self, name: str, args: dict, targets: list, url_before: str) -> None:
        """The agent's action as steps of the fix, worded and targeted as a person's would be."""
        rec = self.fixing["recorder"]
        rec.at(url_before)
        first = len(rec.steps)
        if name == "browser_navigate":
            rec.opened(str(args.get("url") or ""))
        elif name == "browser_navigate_back":
            rec.went_back()
        elif name == "browser_click":
            rec.clicked(targets[0])
        elif name == "browser_type":
            rec.typed(str(args.get("text") or ""), targets[0])
            if args.get("submit"):
                rec.pressed("Enter")
        elif name == "browser_select_option":
            rec.chose(targets[0], str((args.get("values") or [""])[0]))
        elif name == "browser_fill_form":
            for field, target in zip(args.get("fields") or [], targets):
                if field.get("type") in ("combobox", "select"):
                    rec.chose(target, str(field.get("value") or ""))
                else:
                    rec.flush()
                    rec.typed(str(field.get("value") or ""), target)
        elif name == "browser_press_key":
            rec.pressed(str(args.get("key") or ""))
        for step in rec.steps[first:]:
            step["actor"] = "agent"

    def finish_fix(self) -> dict | None:
        """The fix as an edit of the missed step: `{step_id, n, steps, shots}`; a typed value that was one of the
        run's inputs becomes that input again, so no run's data is kept in the recording."""
        fixing, self.fixing = self.fixing, None
        if fixing is None:
            return None
        done = fixing["recorder"].result(self._page_url())
        inputs = {str(v): k for k, v in ((self.replaying or {}).get("inputs") or {}).items() if v not in (None, "")}
        for step in done["steps"]:
            fixed = (step.get("value") or {}).get("fixed")
            if fixed is not None and fixed in inputs:
                step["value"] = {"input": inputs[fixed]}
                step["line"] = line_of(step)
        return {"step_id": fixing["step"].get("id"), "n": fixing["step"].get("n"), **done}

    # ── recording the person's task (F1141) ──────────────────────────────────
    def start_recording(self) -> dict:
        if self.recorder is None:
            self.recorder = Recorder()
        self._push_state()
        return {"recording": True}

    def stop_recording(self) -> dict | None:
        recorder, self.recorder = self.recorder, None
        self._push_state()
        return recorder.result(self._page_url()) if recorder is not None else None

    async def _target_at(self, x: float, y: float) -> dict | None:
        try:
            return await self.page.evaluate(
                f"([x, y]) => ({TARGET_JS})(document.elementFromPoint(x, y))", [x, y])
        except Exception:
            return None

    async def _target_focused(self) -> dict | None:
        try:
            return await self.page.evaluate(f"() => ({TARGET_JS})(document.activeElement)")
        except Exception:
            return None

    async def _record_input(self, kind: str, body: dict) -> dict | None:
        """The step a person's input makes, read before it acts (where it lands is the page as they saw it)."""
        rec = self._rec()
        rec.at(self._page_url())
        if kind == "mouse" and body.get("event", "click") in ("click", "down"):
            return rec.clicked(await self._target_at(float(body.get("x", 0)), float(body.get("y", 0))))
        if kind == "text":
            step = rec.typed(str(body.get("text") or ""), await self._target_focused())
            if step is not None and "person" in (step.get("value") or {}):
                rec.keep_quiet(self._page_url())
            self._push_state()
            return None  # typing is pictured with the step that ends it
        if kind == "key":
            return rec.pressed(str(body.get("key") or ""))
        if kind == "nav" and body.get("url"):
            return rec.opened(str(body["url"]))
        if kind == "nav" and body.get("action") == "back":
            return rec.went_back()
        return None

    async def _picture(self, step: dict | None, watch: "_NavWatch | None" = None) -> None:
        """The page after a step, about 1024 px wide: none on a page the person typed something of theirs on.

        A step that leads to another page is pictured there: its picture waits for that page's document (`watch`).
        All of it within `_SHOT_DEADLINE_S`, and the page is never scaled to picture it, only the picture here: a
        capture with CDP's `clip.scale` held the page laid out, drawn and clicked at that scale while it waited on
        the next page, a minute at worst, and the person's reply with it (F1196). A picture that is late is none,
        and the step is kept without one (`after_shot` stays None, as on a page the person typed something of
        theirs on)."""
        if step is None or self._rec() is None or self.page is None:
            self._end_watch(watch)
            self._push_state()
            return
        loop = asyncio.get_running_loop()
        until = loop.time() + _SHOT_DEADLINE_S
        jpeg = None
        try:
            if watch is not None:
                await watch.settled(until)
            raw = await asyncio.wait_for(self.page.screenshot(type="jpeg", quality=80, timeout=0),
                                         timeout=max(0.5, until - loop.time()))
            jpeg = await asyncio.to_thread(_thumbnail, raw)
        except Exception:  # late, or the page went away: the step stands without its picture
            jpeg = None
        finally:
            self._end_watch(watch)
        self._rec().shot(step, jpeg, self._page_url())
        self._push_state()

    async def _act(self, page, kind: str, body: dict) -> None:
        mods = [m for m in (body.get("modifiers") or []) if m in ("Shift", "Control", "Alt", "Meta")]
        for m in mods:
            await page.keyboard.down(m)
        try:
            if kind == "mouse":
                x, y = float(body.get("x", 0)), float(body.get("y", 0))
                button = body.get("button") if body.get("button") in ("left", "right", "middle") else "left"
                event = body.get("event", "click")
                if event == "click":
                    await page.mouse.click(x, y, button=button, click_count=2 if body.get("clicks") == 2 else 1)
                elif event == "move":
                    await page.mouse.move(x, y)
                elif event == "down":
                    await page.mouse.move(x, y)
                    await page.mouse.down(button=button)
                elif event == "up":
                    await page.mouse.move(x, y)
                    await page.mouse.up(button=button)
            elif kind == "scroll":
                await page.mouse.move(float(body.get("x", 0)), float(body.get("y", 0)))
                await page.mouse.wheel(float(body.get("dx", 0)), float(body.get("dy", 0)))
            elif kind == "key" and body.get("key"):
                await page.keyboard.press(str(body["key"]))
            elif kind == "text" and body.get("text"):
                await page.keyboard.insert_text(str(body["text"]))
            elif kind == "nav":
                if body.get("url"):
                    await page.goto(str(body["url"]), wait_until="commit")
                elif body.get("action") == "back":
                    await page.go_back(wait_until="commit")
                elif body.get("action") == "forward":
                    await page.go_forward(wait_until="commit")
                elif body.get("action") == "reload":
                    await page.reload(wait_until="commit")
        finally:
            for m in reversed(mods):
                try:
                    await page.keyboard.up(m)
                except Exception:
                    pass

    def state(self) -> dict:
        return {"wheel": self.wheel, "replaying": self.replay_running, "url": self._page_url(), "banner": self.banner,
                "load_error": self.load_error,
                **self.popups(),
                "recording": self.recorder.frame() if self.recorder is not None else None,
                "held": ({"method": self.held["method"], "url": self.held["url"], "money": bool(self.held.get("money"))}
                         if self.held else None)}

    def _push_state(self) -> None:
        """Viewers see the wheel and the banner change at once, on the last frame."""
        if self.last_frame and self.viewers:
            meta = {k: v for k, v in self.last_frame.items() if k not in ("dialog", "select", "file")}
            meta = {**meta, "wheel": self.wheel, "replaying": self.replay_running, "banner": self.banner,
                    "load_error": self.load_error, "url": self._page_url(), **self.popups(),
                    "recording": self.recorder.frame() if self.recorder is not None else None}
            for q in list(self.viewers):
                _offer(q, meta)

    def _inline_snapshots(self, text: str) -> str:
        """Playwright MCP links an action's page snapshot to a file in this session's
        folder; the agent only reads text, so the file's contents replace the link."""
        def put(match: re.Match) -> str:
            path = (self.dir / match.group(1)).resolve()
            if self.dir.resolve() not in path.parents or not path.is_file():
                return match.group(0)
            return "```yaml\n" + path.read_text(errors="replace") + "\n```"
        return _SNAPSHOT_LINK.sub(put, text)

    async def _record(self, actor: str, action: str, target: str, **extra) -> dict:
        ref = None
        try:
            shot = await self.page.screenshot(type="jpeg", quality=85)  # kept, and looked at large after (F1356)
            ref = f"{len(self.log) + 1}"
            self.shots[ref] = shot
        except Exception:
            pass
        return self._note(actor, action, target, ref, **extra)

    def _note(self, actor: str, action: str, target: str, shot_ref: str | None = None, **extra) -> dict:
        url = ""
        try:
            url = self.page.url if self.page else ""
        except Exception:
            pass
        entry = {"n": len(self.log) + 1, "at": time.time(), "session_id": self.id, "actor": actor, "action": action,
                 "target": target, "url": url, "shot_ref": shot_ref, **extra}
        self.log.append(entry)
        if actor != "agent":  # an agent's line comes on its own call's event; these have no call (F1125)
            for q in list(self.viewers):
                _offer(q, {"log": entry})
        return entry

    # ── the person's view ────────────────────────────────────────────────────
    def _watch(self, page) -> None:
        """The screencast draws no native popup: while the person drives, the pane draws them. A page that did not
        open is said the same way (F1425)."""
        page.on("dialog", self._on_dialog)
        page.on("filechooser", self._on_chooser)
        page.on("requestfailed", self._on_request_failed)
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_frame_navigated)
        page.on("load", lambda _page: asyncio.ensure_future(self._on_load()))

    def _set_load_error(self, error: dict | None) -> None:
        if error != self.load_error:
            self.load_error = error
            self._push_state()

    def _on_request_failed(self, request) -> None:
        """The main frame's page did not open: the headless shell draws its error page white, so it is said."""
        try:
            if not request.is_navigation_request() or not _is_main_frame(request):
                return
            code = str(request.failure or "").split()[0].removeprefix("net::") if request.failure else ""
        except Exception:
            return
        if code in ("", "ERR_ABORTED"):  # another navigation took its place, or a download: nothing failed
            return
        error = {"url": request.url, "host": urlparse(request.url).hostname or "", "kind": _failure_kind(code),
                 "code": code}
        if code == "ERR_HTTP_RESPONSE_CODE_FAILURE" and self._doc:  # full Chromium's word for an empty refusal
            error = {**error, "kind": "http", "status": self._doc[1]}
            error.pop("code")
        elif code == "ERR_BLOCKED_BY_CLIENT" and self._refused_top:
            error["why"] = self._refused_top
        self._set_load_error(error)

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and _is_main_frame(response.request):
                self._doc = (response.url, int(response.status))
        except Exception:
            pass

    def _on_frame_navigated(self, frame) -> None:
        """A page that opened clears it; Chromium's own error page is where it failed."""
        try:
            if frame.parent_frame is not None or str(frame.url).startswith("chrome-error://"):
                return
        except Exception:
            return
        self._refused_top = None
        self._set_load_error(None)

    async def _on_load(self) -> None:
        """A document that answered >= 400 and draws nothing (x.com's 403 to this browser): said. One that draws
        something (a site's challenge) is the person's to see, as it is."""
        doc, page = self._doc, self.page
        if not doc or doc[1] < 400 or page is None or page.url != doc[0]:
            return
        try:
            empty = await page.evaluate(_DRAWS_NOTHING_JS)
        except Exception:
            return
        if empty and page.url == doc[0]:
            self._set_load_error({"url": doc[0], "host": urlparse(doc[0]).hostname or "", "kind": "http",
                                  "status": doc[1]})

    def _on_dialog(self, dialog) -> None:
        if self.wheel == "person":  # the agent's are Playwright MCP's (browser_handle_dialog)
            self.dialog = dialog
            self._push_state()
        else:
            self.agent_dialog = dialog.message or ""

    def _on_chooser(self, chooser) -> None:
        if self.wheel == "person":
            self.chooser = chooser
            asyncio.ensure_future(self._chooser_accepts(chooser))

    async def _chooser_accepts(self, chooser) -> None:
        try:
            self._accept = await chooser.element.get_attribute("accept") or ""
        except Exception:
            self._accept = ""
        self._push_state()

    def popups(self) -> dict:
        out: dict = {}
        if self.dialog is not None:
            out["dialog"] = {"type": self.dialog.type, "message": self.dialog.message,
                             "default": self.dialog.default_value or None}
        if self.select:
            out["select"] = self.select
        if self.chooser is not None:
            out["file"] = {"multiple": self.chooser.is_multiple(), "accept": getattr(self, "_accept", "") or ""}
        return out

    async def answer_dialog(self, accept: bool, text: str | None) -> dict | None:
        dialog, self.dialog = self.dialog, None
        if dialog is None:
            return None
        try:
            await (dialog.accept(text) if accept and text is not None else dialog.accept() if accept
                   else dialog.dismiss())
        except Exception:
            pass
        self._push_state()
        return {"ok": True}

    async def choose(self, target: str, value: str) -> dict | None:
        """The person's pick from a dropdown the pane drew: set on the element at that point."""
        if not self.select or self.select.get("target") != target:
            return None
        options, self.select = self.select.get("options") or [], None
        try:
            x, y = (float(v) for v in target.split(","))
            if self._rec() is not None:
                self._rec().at(self._page_url())
                label = next((o.get("label") for o in options if o.get("value") == value), value)
                step = self._rec().chose(await self._target_at(x, y), str(label))
                await self._picture(step)
            await self.page.evaluate(
                "([x, y, v]) => { const s = document.elementFromPoint(x, y)?.closest('select'); if (!s) return;"
                " s.value = v; s.dispatchEvent(new Event('input', {bubbles: true}));"
                " s.dispatchEvent(new Event('change', {bubbles: true})); }", [x, y, value])
        except Exception:
            pass
        self._push_state()
        return {"ok": True}

    async def give_file(self, name: str, data: bytes) -> dict | None:
        chooser, self.chooser = self.chooser, None
        if chooser is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")[:120] or "file"
        folder = self.dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe
        path.write_bytes(data)
        try:
            await chooser.set_files(str(path))
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        finally:
            self._push_state()
        return {"ok": True}

    def _on_page(self, page) -> None:
        self.page = page
        self._doc = self._refused_top = None
        self._set_load_error(None)  # it was the page before's
        self._watch(page)
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
                    "wheel": self.wheel, "replaying": self.replay_running, "banner": self.banner,
                    "load_error": self.load_error, **self.popups()}
            self.last_frame = meta
            for q in list(self.viewers):
                _offer(q, meta)
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))

        cdp.on("Page.screencastFrame", on_frame)
        # JPEG 75, the run page's large view worth watching (F1356; it was 60); skip-not-lag holds (`_offer`).
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 75,
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
            shot = await self.page.screenshot(type="jpeg", quality=75)  # as the screencast (F1356)
        except Exception:
            return None
        self.last_frame = {"jpeg_b64": base64.b64encode(shot).decode(), "w": _VIEWPORT["width"],
                           "h": _VIEWPORT["height"], "url": self.page.url, "wheel": self.wheel,
                           "replaying": self.replay_running, "banner": self.banner, "load_error": self.load_error}
        return self.last_frame


#: A recording's pictures: this wide, as JPEG at this quality (a stop carries every one inline, F1142).
_THUMB_WIDTH, _THUMB_QUALITY = 1024, 55
#: The longest a step's picture may hold up the person's reply (F1196).
_SHOT_DEADLINE_S = 5.0
#: How long after a step its page has to start going somewhere else for the picture to wait for it.
_NAV_GRACE_S = 0.25


class _NavWatch:
    """A step's way to another page, as the browser takes it: `requested` when the main frame's navigation goes out
    (`Session._route`), `committed` when the new document is the page's (F1196)."""

    def __init__(self, page) -> None:
        self.page, self.requested, self.committed = page, asyncio.Event(), asyncio.Event()

        def navigated(frame) -> None:
            if frame == page.main_frame:
                self.committed.set()

        self._navigated = navigated
        page.on("framenavigated", navigated)

    async def settled(self, until: float) -> None:
        """Back when the step went nowhere (no navigation in `_NAV_GRACE_S`), or its next page has its document;
        never past `until`."""
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(self.requested.wait(), timeout=_NAV_GRACE_S)
        except asyncio.TimeoutError:
            if not self.committed.is_set():
                return
        try:
            await asyncio.wait_for(self.committed.wait(), timeout=max(0.0, until - loop.time() - 0.5))
            await self.page.wait_for_load_state("domcontentloaded",
                                                timeout=max(1.0, (until - loop.time() - 0.5) * 1000))
        except Exception:
            pass  # still on its way: pictured as it is

    def close(self) -> None:
        try:
            self.page.remove_listener("framenavigated", self._navigated)
        except Exception:
            pass


def _thumbnail(jpeg: bytes) -> bytes:
    """A picture of the page made `_THUMB_WIDTH` wide, off the page (F1196)."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        if image.width > _THUMB_WIDTH:
            image = image.resize((_THUMB_WIDTH, round(image.height * _THUMB_WIDTH / image.width)), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=_THUMB_QUALITY)
        return out.getvalue()


#: A page that draws nothing a person could read or press: no text, no picture, no control.
_DRAWS_NOTHING_JS = """() => { const b = document.body; if (!b) return true; if ((b.innerText || '').trim()) return false;
  return !document.querySelector('img, svg, canvas, video, iframe, object, embed, input, button, select, textarea'); }"""


def _failure_kind(code: str) -> str:
    """A failed navigation's Chromium code, as the pane words it."""
    if code == "ERR_BLOCKED_BY_CLIENT":
        return "blocked"
    if code == "ERR_NAME_NOT_RESOLVED":
        return "not_found"
    if code.startswith(("ERR_CERT_", "ERR_SSL_")) or code == "ERR_BAD_SSL_CLIENT_AUTH_CERT":
        return "certificate"
    if code in ("ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_TIMED_OUT",
                "ERR_CONNECTION_FAILED", "ERR_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE", "ERR_INTERNET_DISCONNECTED",
                "ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED", "ERR_EMPTY_RESPONSE",
                "ERR_NETWORK_CHANGED"):
        return "unreachable"
    return "failed"


def _is_main_frame(req) -> bool:
    try:
        return req.frame.parent_frame is None
    except Exception:
        return True  # a request with no frame (a service worker) is treated as top-level: scoped


def _offer(q: asyncio.Queue, item: dict) -> None:
    """Newest frame wins: a slow viewer skips frames rather than lagging. A log line or the close is never dropped
    (F1125): it waits, in order, ahead of the frame that follows it."""
    if "jpeg_b64" in item:
        waiting = []
        while not q.empty():
            older = q.get_nowait()
            if "jpeg_b64" not in older:
                waiting.append(older)
        for older in waiting:
            q.put_nowait(older)
    q.put_nowait(item)


#: The longest a short-lived token may run ahead of now (F1135, BRW3 §8).
TOKEN_MAX_S = 15 * 60


class Host:
    """The component's two ports and its sessions. Either a fixed token (OSS: the backend that started it) or,
    once `set_secret` is called, short-lived tokens made from a per-VM secret (cloud, BRW3 §8):
    `<exp>.<hex HMAC-SHA256(secret, "<exp>")>`, accepted while `now < exp <= now + 15 min`. Until one of the two
    is set, every request is refused."""

    def __init__(self, token: str | None, root: Path, *, chromium_sandbox: bool | None = None):
        self.token = token or ""
        self.secret: bytes | None = None
        self.root = root
        self.chromium_sandbox = chromium_sandbox
        self.sessions: dict[str, Session] = {}
        self.warm: Session | None = None   # a prelaunched browser, adopted by the next session
        self.pw = None

    def set_secret(self, secret: bytes | str) -> None:
        """Once only: the per-VM secret, from the VM's run payload. A second call is refused."""
        if self.secret is not None:
            raise RuntimeError("the browser's secret is already set")
        value = secret.encode() if isinstance(secret, str) else bytes(secret)
        if len(value) < 16:
            raise ValueError("the browser's secret is too short")
        self.secret = value

    def _authorised(self, request: web.Request) -> bool:
        given = request.headers.get(_TOKEN_HEADER, "")
        if not given:
            return False
        if self.secret is not None:
            return _token_valid(self.secret, given, time.time())
        return bool(self.token) and hmac.compare_digest(given, self.token)

    async def prelaunch(self) -> None:
        """Start Playwright, the egress proxy and one browser ahead of any session (cloud's snapshot holds it warm);
        the next `POST /session` adopts it instead of launching."""
        if self.pw is None:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
        if self.warm is None:
            warm = Session(f"warm-{uuid.uuid4().hex[:8]}", "", gate.READ_ONLY, {}, self.root)
            # In a task of its own that ends here, as a request's does: Playwright MCP's connection is opened in it,
            # and a task still running (the server's own) could never let another close it.
            await asyncio.ensure_future(warm.launch(self.pw, chromium_sandbox=self.chromium_sandbox))
            self.warm = warm

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
        return web.json_response({"ok": True, "sessions": len(self.sessions), "warm": self.warm is not None})

    async def tools(self, _request):
        return web.json_response({"tools": await self._tool_schemas()})

    async def _tool_schemas(self) -> list[dict]:
        if getattr(self, "_schemas", None) is not None:
            return self._schemas
        self._schemas = [{"name": t["name"], "description": t["description"],
                          "inputSchema": _with_confirm_schema(t["name"], t["inputSchema"]),
                          "tags": gate.ALLOWED[t["name"]]} for t in await pinned_tool_schemas()]
        self._schemas += [_HAND_OVER_SCHEMA, _RUN_RECORDING_SCHEMA]
        return self._schemas

    async def open_session(self, request):
        body = await request.json()
        sid = uuid.uuid4().hex
        owner, mode, limits = str(body.get("owner", "")), body.get("mode", gate.READ_ONLY), body.get("limits") or {}
        sites, wheel = body.get("sites") or [], "person" if body.get("person_only") else "agent"
        warm, self.warm = self.warm, None
        if warm is not None:
            s = warm
            s.adopt(sid, owner, mode, limits, sites, wheel)
        else:
            s = Session(sid, owner, mode, limits, self.root, sites=sites, wheel=wheel)
        try:
            if warm is not None:
                await s.apply_mode()
            else:
                await s.start(self.pw, chromium_sandbox=self.chromium_sandbox)
            if isinstance(body.get("login_state"), dict):
                await s.load_login(body["login_state"])
        except Exception as exc:
            await s.close()
            return web.json_response({"error": f"the browser did not start: {exc}"}, status=503)
        if isinstance(body.get("turn"), dict):  # it opens on the person's turn (a kept session that expired)
            turn = body["turn"]
            s._turn(str(turn.get("kind") or "asked"), str(turn.get("reason") or ""),
                    **{k: v for k, v in turn.items() if k in ("site", "site_session_id", "mine")})
            s.handover = None
        self.sessions[sid] = s
        return web.json_response({"session_id": sid})

    async def replay(self, request):
        body = await request.json()
        s = self._session(body.get("session_id", ""))
        return await self._replaying(s, body, s.replay(
            list(body.get("steps") or []), dict(body.get("inputs") or {}), unattended=bool(body.get("unattended")),
            recording_id=str(body.get("recording_id") or "")))

    async def replay_continue(self, request):
        body = await request.json()
        s = self._session(body.get("session_id", ""))
        if s.replay_task is not None and not s.replay_task.done():
            return web.json_response({"error": "A replay is running.", "replay_id": s.replay_id}, status=409)
        plan = s.replaying
        if not plan:
            return web.json_response({"error": "Nothing is being replayed."}, status=409)

        async def carry_on() -> dict:
            # From the step after the one found again (a fix, or the person's turn), unless told where.
            fix = s.finish_fix()
            after = (fix or {}).get("n") or plan.get("turn_at")
            from_n = int(body.get("from_n") or (int(after) + 1 if after else 1))
            plan.pop("turn_at", None)
            done = await s.replay(plan["steps"], plan["inputs"], unattended=plan["unattended"],
                                  recording_id=plan["recording_id"], from_n=from_n)
            return {**done, **({"fix": fix} if fix else {})}
        return await self._replaying(s, body, carry_on())

    async def _replaying(self, s: Session, body: dict, run) -> web.Response:
        """Answer a replay when it ends, or (`wait: false`) start it as the session's one replay task: 202
        `{replay_id}` (the caller's own id when it sends one), polled at /replay/status. A start while one runs is 409
        with the running one's id, so a start sent again never runs it twice."""
        if s.replay_task is not None and not s.replay_task.done():
            run.close()
            return web.json_response({"error": "A replay is running.", "replay_id": s.replay_id}, status=409)
        if body.get("wait", True) is not False:
            return web.json_response(await run)
        given = str(body.get("replay_id") or "")
        s.replay_id = given[:40] if given.startswith("rp_") else f"rp_{uuid.uuid4().hex[:12]}"
        s.replay_answer, s.replay_at = None, None

        async def task() -> None:
            try:
                s.replay_answer = await run
            except asyncio.CancelledError:
                s.replay_answer = {"refused": "The browser closed during the replay."}
                raise
            except Exception as exc:
                said = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                s.replay_answer = {"refused": f"The replay failed: {said}"}
        s.replay_task = asyncio.ensure_future(task())
        return web.json_response({"replay_id": s.replay_id}, status=202)

    async def replay_status(self, request):
        """A started replay: `{replay_id, running, at}` while it runs, then its answer with `replay_id`. Without a
        `replay_id`, the session's latest (a start whose 202 was lost is found this way)."""
        s = self._session(request.query.get("session_id", ""))
        want = request.query.get("replay_id") or s.replay_id
        if not want or want != s.replay_id:
            return web.json_response({"error": "No such replay."}, status=404)
        if s.replay_task is not None and not s.replay_task.done():
            return web.json_response({"replay_id": s.replay_id, "running": True, "at": s.replay_at})
        return web.json_response({"replay_id": s.replay_id, **(s.replay_answer or {"refused": "The replay ended."})})

    async def idle(self, request):
        body = await request.json()
        return web.json_response(self._session(body.get("session_id", "")).go_idle())

    async def call_tool(self, request):
        body = await request.json()
        s = self._session(body.get("session_id", ""))
        return web.json_response(await s.call(str(body.get("name", "")), body.get("args") or {},
                                              body.get("press") if isinstance(body.get("press"), dict) else None))

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
        state = None
        if s is not None:
            if body.get("keep_login"):
                state = await s.login_state()
            await s.close()
        return web.json_response({"closed": s is not None, "login_state": state})

    async def get_login_state(self, request):
        s = self._session(request.query.get("session_id", ""))
        return web.json_response({"login_state": await s.login_state()})

    # viewer
    async def frames(self, request):
        s = self._session(request.query.get("session_id", ""))
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()  # `_offer` keeps one frame in it; lines all wait
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

    async def get_state(self, request):
        return web.json_response(self._session(request.query.get("session_id", "")).state())

    async def _viewer_body(self, request) -> tuple[Session, dict]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return self._session(request.query.get("session_id", "") or body.get("session_id", "")), body

    async def input(self, request):
        s, body = await self._viewer_body(request)
        done = await s.input(body)
        if done is None:
            return web.json_response({"error": "You don't hold the wheel."}, status=409)
        return web.json_response(done)

    async def wheel_take(self, request):
        s, _ = await self._viewer_body(request)
        return web.json_response(s.take())

    async def wheel_hand_back(self, request):
        s, body = await self._viewer_body(request)
        return web.json_response(s.hand_back(str(body.get("note") or "")))

    async def release(self, request):
        s, body = await self._viewer_body(request)
        done = s.release()
        if done is None:
            return web.json_response({"error": "Nothing is held."}, status=409)
        if done.get("refused"):
            return web.json_response({"error": done["refused"]}, status=409)
        return web.json_response(done)

    async def allow_site(self, request):
        s, body = await self._viewer_body(request)
        return web.json_response(s.allow_site(str(body.get("host") or "")))

    async def dialog(self, request):
        s, body = await self._viewer_body(request)
        done = await s.answer_dialog(bool(body.get("accept", True)), body.get("text"))
        return web.json_response(done) if done else web.json_response({"error": "No dialog is open."}, status=409)

    async def select(self, request):
        s, body = await self._viewer_body(request)
        done = await s.choose(str(body.get("target") or ""), str(body.get("value") or ""))
        return web.json_response(done) if done else web.json_response({"error": "No dropdown is open."}, status=409)

    async def file(self, request):
        s, body = await self._viewer_body(request)
        try:
            data = base64.b64decode(body.get("b64") or "", validate=True)
        except Exception:
            return web.json_response({"error": "That file did not arrive whole."}, status=422)
        if len(data) > 25 * 1024 * 1024:
            return web.json_response({"error": "Files go up to 25 MB."}, status=413)
        done = await s.give_file(str(body.get("name") or "file"), data)
        return web.json_response(done) if done else web.json_response({"error": "No file picker is open."}, status=409)

    async def record_start(self, request):
        s, body = await self._viewer_body(request)
        if body.get("replay_first") or body.get("carry_on"):
            # Re-recording from a step: the steps before it run first, the person watching; after the person's own
            # step stopped them (a sign-in), `carry_on` goes on from the step after it.
            if s.held is not None or s.recorder is not None:
                return web.json_response({"error": "Not now: this browser is busy."}, status=409)
            plan = s.replaying
            if body.get("carry_on") and not (plan and plan.get("turn_at")):
                return web.json_response({"error": "Nothing to carry on."}, status=409)
            s.wheel = "nobody"
            if body.get("carry_on"):
                done = await s.replay(plan["steps"], {}, unattended=False, from_n=int(plan["turn_at"]) + 1)
            else:
                done = await s.replay(list(body["replay_first"]), {}, unattended=False)
            s.wheel, s.fixing = "person", None  # re-recording keeps its own steps, not a fix
            if "handover" in done:  # a turn keeps its banner and its place: the person's step, then carry on
                s.replaying["turn_at"] = done["at"]
            else:
                s.replaying, s.banner = None, None
            s._push_state()
            if "outputs" not in done:
                return web.json_response({"replayed": done}, status=409)
        return web.json_response(s.start_recording())

    async def record_stop(self, request):
        s, _body = await self._viewer_body(request)
        done = s.stop_recording()
        return web.json_response(done) if done is not None else web.json_response(
            {"error": "Nothing is being recorded."}, status=409)

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
    app.router.add_get("/state", host.get_state)
    app.router.add_get("/login-state", host.get_login_state)
    app.router.add_post("/idle", host.idle)
    app.router.add_post("/replay", host.replay)
    app.router.add_post("/replay/continue", host.replay_continue)
    app.router.add_get("/replay/status", host.replay_status)
    return app


def viewer_app(host: Host) -> web.Application:
    app = web.Application(middlewares=[host.auth], client_max_size=36 * 1024 * 1024)
    app.router.add_get("/frames", host.frames)
    app.router.add_post("/input", host.input)
    app.router.add_post("/wheel/take", host.wheel_take)
    app.router.add_post("/wheel/hand-back", host.wheel_hand_back)
    app.router.add_post("/release", host.release)
    app.router.add_post("/allow-site", host.allow_site)
    app.router.add_post("/dialog", host.dialog)
    app.router.add_post("/select", host.select)
    app.router.add_post("/file", host.file)
    app.router.add_post("/record/start", host.record_start)
    app.router.add_post("/record/stop", host.record_stop)
    return app


async def pinned_tool_schemas() -> list[dict]:
    """The gate's tools as the pinned Playwright MCP lists them, raw: what `core/browser/tool_schemas.json` holds
    (F1135), so the engine lists tools with no Node and no browser. Needs npx."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command=shutil.which("npx") or "npx",
        args=["-y", PLAYWRIGHT_MCP, "--cdp-endpoint", "http://127.0.0.1:9", "--no-webmcp"],
        env=_clean_env())
    async with stdio_client(params) as (read, write), ClientSession(read, write) as mcp:
        await mcp.initialize()
        listed = (await mcp.list_tools()).tools
    return sorted(({"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
                   for t in listed if t.name in gate.ALLOWED), key=lambda t: t["name"])


def write_tool_schemas(path: Path) -> None:
    path.write_text(json.dumps(asyncio.run(pinned_tool_schemas()), indent=1, sort_keys=True) + "\n", "utf-8")


def _token_valid(secret: bytes, given: str, now: float) -> bool:
    """A short-lived token made from `secret`: unexpired, at most `TOKEN_MAX_S` ahead, and its HMAC matching."""
    exp_text, _, mac = given.partition(".")
    if not exp_text.isdigit() or not mac:
        return False
    exp = int(exp_text)
    if not now < exp <= now + TOKEN_MAX_S:
        return False
    want = hmac.new(secret, exp_text.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, want)


async def start_host(*, bind: str = "127.0.0.1", control_port: int = 0, viewer_port: int = 0,
                     token: str | None = None, chromium_sandbox: bool | None = None,
                     prelaunch: bool = False) -> tuple[Host, list, list[int]]:
    """The component's ports, listening; `(host, runners, [control, viewer])`. Cloud's wrapper runs it in its own
    event loop and calls `host.set_secret` from the VM's run hook (F1135, BRW3 §4)."""
    from playwright.async_api import async_playwright
    root = Path(tempfile.mkdtemp(prefix="synapse-browser-"))
    host = Host(token, root, chromium_sandbox=chromium_sandbox)
    host.pw = await async_playwright().start()
    if prelaunch:
        await host.prelaunch()
    runners, ports = [], []
    for app, port in ((control_app(host), control_port), (viewer_app(host), viewer_port)):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, bind, port)
        await site.start()
        runners.append(runner)
        ports.append(site._server.sockets[0].getsockname()[1])
    return host, runners, ports


async def stop_host(host: Host) -> None:
    for s in list(host.sessions.values()):
        await s.close()
    if host.warm is not None:
        await host.warm.close()
    if host.pw is not None:
        await host.pw.stop()
    shutil.rmtree(host.root, ignore_errors=True)


async def serve(control_port: int, viewer_port: int, *, bind: str = "127.0.0.1",
                chromium_sandbox: bool | None = None, prelaunch: bool = False) -> None:
    token = os.environ.get("SYNAPSE_BROWSER_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("SYNAPSE_BROWSER_TOKEN is missing or too short")
    host, _runners, ports = await start_host(bind=bind, control_port=control_port, viewer_port=viewer_port,
                                             token=token, chromium_sandbox=chromium_sandbox, prelaunch=prelaunch)
    print(f"BROWSER_HOST_READY {ports[0]} {ports[1]}", flush=True)
    try:
        await host.reap()
    finally:
        await stop_host(host)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse's browser component")
    parser.add_argument("--control-port", type=int, default=0)
    parser.add_argument("--viewer-port", type=int, default=0)
    parser.add_argument("--bind", default="127.0.0.1", help="the address both ports listen on (a VM's own: 0.0.0.0)")
    parser.add_argument("--chromium-sandbox", choices=("on", "off"), default=None,
                        help="Chromium's own sandbox; left out, Playwright's default")
    parser.add_argument("--prelaunch", action="store_true", help="start one browser before any session asks")
    parser.add_argument("--write-tool-schemas", metavar="PATH", default=None,
                        help="write the pinned Playwright MCP's tool schemas there and exit (F1135)")
    args = parser.parse_args()
    if args.write_tool_schemas:
        write_tool_schemas(Path(args.write_tool_schemas))
        return
    sandbox = None if args.chromium_sandbox is None else args.chromium_sandbox == "on"
    asyncio.run(serve(args.control_port, args.viewer_port, bind=args.bind, chromium_sandbox=sandbox,
                      prelaunch=args.prelaunch))


if __name__ == "__main__":
    main()
