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

from core import egress_proxy
from core.browser import gate

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


class Session:
    def __init__(self, sid: str, owner: str, mode: str, limits: dict, root: Path, sites: list[str] | None = None,
                 wheel: str = "agent"):
        self.id = sid
        self.owner = owner
        self.mode = mode if mode in gate.MODES else gate.READ_ONLY
        self.sites = list(sites or [])
        self.typing_hosts: set[str] = set()            # allowed once by the person this session
        self.allowed_sends: set[tuple[str, str]] = set()  # "it only loaded data: don't ask again"
        self.held: dict | None = None                  # an agent's send paused at the network
        self.banner: dict | None = None
        self.handover: dict | None = None              # what the agent's current call must hand over
        self.handback: str | None = None               # what the agent hears on its next call
        self.took_at: float | None = None
        self.last_frame: dict | None = None
        self.dialog = None          # a page's alert/confirm/prompt, while the person drives
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
    async def start(self, pw) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # No service workers: a worker's own fetches would not pass the route below. Every connection the browser
        # makes goes through the egress proxy (SEC2, F1065/F1109), the outer layer under the route and socket
        # guards: it dials the address the guard checked, so a name that changes its answer reaches nothing new.
        proxy = _browser_proxy_env() or (await egress_proxy.shared()).url
        self.ctx = await pw.chromium.launch_persistent_context(
            str(self.dir / "profile"), headless=True, viewport=_VIEWPORT, args=_CHROMIUM_ARGS,
            env=_clean_env(), service_workers="block", proxy={"server": proxy},
        )
        await self.ctx.route("**/*", self._route)
        await self.ctx.route_web_socket(lambda _url: True, self._route_socket)
        if self.mode != gate.CO_DRIVE:
            await self.ctx.add_init_script(_NO_WORKERS)
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
        agent = self.wheel == "agent"
        try:
            nav = req.is_navigation_request()
            top = nav and _is_main_frame(req)
            reason = await gate.verdict(req.url, req.method, self.mode,
                                        sites=self.sites if (agent and top) else None)
            if reason is None and nav:
                reason = await self._pace(urlparse(req.url).hostname or "")
            hold = (reason is None and agent and self.mode == gate.CO_DRIVE
                    and gate.is_send(req.method, req.url, navigation=nav, page_url=self._page_url())
                    and gate.send_key(req.method, req.url) not in self.allowed_sends)
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

    async def _hold(self, route, req) -> None:
        """An agent's send waits here for the person: let it go (it only loaded data), or take over
        and send it themselves. Unanswered, it is dropped and stays their turn."""
        host = urlparse(req.url).hostname or "the site"
        event = asyncio.Event()
        self.held = {"method": req.method.upper(), "url": req.url, "event": event, "decision": None}
        reason = f"The agent tried to send this to {host}. Nothing has gone."
        self.banner = {"kind": "send", "reason": reason, "held": {"method": req.method.upper(), "url": req.url}}
        self.handover = {"kind": "send", "reason": reason}
        self.wheel = "nobody"
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
            if name in gate.TYPES_TEXT:
                stop = await self._before_typing(name, args or {})
                if stop:
                    return stop
            self.actions += 1
            seen = len(self.log)
            self.handover = None
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
                if self.handover is None and self._captcha_showing():
                    self._turn("captcha", "A CAPTCHA or a bot check is showing: it is yours to solve.")
            if self.handback:
                text = f"### The person\n{self.handback}\n" + text
                self.handback = None
        out = {"text": text, "is_error": bool(result.isError), "log": entry}
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
    def take(self) -> dict:
        if self.held:
            self.held["decision"] = "take"
            self.held["event"].set()
        self.wheel = "person"
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

    def release(self, allow_address: bool = True) -> dict | None:
        if not self.held:
            return None
        if allow_address:
            self.allowed_sends.add(gate.send_key(self.held["method"], self.held["url"]))
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
        task = asyncio.ensure_future(self._act(page, kind, body))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except asyncio.TimeoutError:
            return {"ok": True, "pending": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        self.last_used = time.monotonic()
        return {"ok": True}

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
        return {"wheel": self.wheel, "url": self._page_url(), "banner": self.banner, **self.popups(),
                "held": ({"method": self.held["method"], "url": self.held["url"]} if self.held else None)}

    def _push_state(self) -> None:
        """Viewers see the wheel and the banner change at once, on the last frame."""
        if self.last_frame and self.viewers:
            meta = {k: v for k, v in self.last_frame.items() if k not in ("dialog", "select", "file")}
            meta = {**meta, "wheel": self.wheel, "banner": self.banner, "url": self._page_url(), **self.popups()}
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
    def _watch(self, page) -> None:
        """The screencast draws no native popup: while the person drives, the pane draws them."""
        page.on("dialog", self._on_dialog)
        page.on("filechooser", self._on_chooser)

    def _on_dialog(self, dialog) -> None:
        if self.wheel == "person":  # the agent's are Playwright MCP's (browser_handle_dialog)
            self.dialog = dialog
            self._push_state()

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
        self.select = None
        try:
            x, y = (float(v) for v in target.split(","))
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
                    "wheel": self.wheel, "banner": self.banner, **self.popups()}
            self.last_frame = meta
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
        self.last_frame = {"jpeg_b64": base64.b64encode(shot).decode(), "w": _VIEWPORT["width"],
                           "h": _VIEWPORT["height"], "url": self.page.url, "wheel": self.wheel, "banner": self.banner}
        return self.last_frame


def _is_main_frame(req) -> bool:
    try:
        return req.frame.parent_frame is None
    except Exception:
        return True  # a request with no frame (a service worker) is treated as top-level: scoped


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
                    self.root, sites=body.get("sites") or [],
                    wheel="person" if body.get("person_only") else "agent")
        try:
            await s.start(self.pw)
            if isinstance(body.get("login_state"), dict):
                await s.load_login(body["login_state"])
        except Exception as exc:
            await s.close()
            return web.json_response({"error": f"the browser did not start: {exc}"}, status=503)
        if isinstance(body.get("turn"), dict):  # it opens on the person's turn (a saved login that expired)
            s._turn(str(body["turn"].get("kind") or "asked"), str(body["turn"].get("reason") or ""))
            s.handover = None
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
        done = s.release(bool(body.get("allow_address", True)))
        if done is None:
            return web.json_response({"error": "Nothing is held."}, status=409)
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
