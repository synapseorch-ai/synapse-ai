#!/usr/bin/env python3
"""Browser checks for the single-process product.

Each check drives a real instance in a real browser and asserts something a
test in isolation cannot: where things actually land on the screen, what a
control's accessible name really is, what the network actually carried. It is
not a unit-test suite and does not pretend to be one — it exists for the class
of defect that only a rendered page can show.

    SYNAPSE_INTERNAL_TOKEN=... ./scripts/ui-smoke.py
    ./scripts/ui-smoke.py --only runbar --app http://localhost:3100

Rules that are not style:

* `--app` must be `localhost`, never `127.0.0.1` — a Next dev server at the
  numeric host never hydrates.
* Never wait for `networkidle`: `/api/notifications/stream` is held open for
  the life of the page, so it never arrives. Every check settles on content.
* Both processes need the same `SYNAPSE_INTERNAL_TOKEN` or every `/api/*` call
  is refused; the `token` check says so once instead of failing everything.
* Exit status is the number of failed checks, so a broken check cannot hide
  the rest.
* Every check's browser has already seen every guided tour (`ToursSeen`): a
  page's first-visit tour covers it until put away, so a fresh browser would
  meet one on every page. Only `tour` starts fresh, with `fresh_context`.
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
from urllib.parse import quote
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP = os.environ.get("SMOKE_APP", "http://localhost:3100")
API = os.environ.get("SMOKE_API", "http://127.0.0.1:8865")
TOKEN = os.environ.get("SYNAPSE_INTERNAL_TOKEN", "")
CHROME = os.environ.get(
    "SMOKE_CHROME",
    os.path.expanduser("~/.cache/ms-playwright/chromium-1222/chrome-linux64/chrome"),
)
WIDE = (1600, 900)
PHONE = (400, 850)
SETTLE = 2.0

CHECKS: dict[str, "object"] = {}


def check(fn):
    CHECKS[fn.__name__.removeprefix("check_")] = fn
    return fn


# Every guided tour (GUIDE1, F919/F1028) reads as seen, whatever its page or version: `tourSeen` reads
# `synapse.tour.<id>.v<n>` from localStorage, and here that read is always '1'.
TOURS_SEEN = """try{const get=Storage.prototype.getItem;Storage.prototype.getItem=function(k){
  return this===window.localStorage&&typeof k==='string'&&k.startsWith('synapse.tour.')
    ?'1':get.call(this,k)}}catch(e){}"""


class ToursSeen:
    """The browser every check gets: each context it opens has seen every tour already.

    Naveen (2026-10-24): a page's guided tour opens by itself once, on the first visit, and its overlay takes
    the clicks until it is put away; that replaced "help only on click". A check about anything else must not
    meet one by accident, so this is where no check can forget it. `fresh_context` is the exception, for `tour`.
    """

    def __init__(self, browser):
        self._browser = browser

    def __getattr__(self, name):
        return getattr(self._browser, name)

    async def new_context(self, **kwargs):
        ctx = await self._browser.new_context(**kwargs)
        await ctx.add_init_script(TOURS_SEEN)
        return ctx

    async def fresh_context(self, **kwargs):
        return await self._browser.new_context(**kwargs)


# ── the API, as the app's own server calls it ────────────────────────────────

def api(path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Synapse-Internal": TOKEN},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def human_gate_orchestration(oid: str) -> dict:
    """An orchestration that fills the run log and then parks on a person.

    Deliberately model-free: `print` steps cost nothing and need no provider
    key, and there are enough of them to push the run log past the height of
    the pane. That matters — the sticky "waiting for input" strip only reaches
    the bottom of the viewport once the log is long, which is exactly when it
    meets whatever else is floating there.
    """
    steps = []
    for i in range(8):
        steps.append({
            "id": f"{oid}_p{i}",
            "name": f"Line {i + 1}",
            "type": "print",
            "print_content": f"smoke line {i + 1} — filling the run log so the strip reaches the bottom",
            "next_step_id": f"{oid}_p{i + 1}" if i < 7 else f"{oid}_human",
            "output_key": f"line_{i}",
        })
    steps.append({
        "id": f"{oid}_human",
        "name": "Approve",
        "type": "human",
        "human_prompt": "Approve this smoke run?",
        "next_step_id": None,
        "output_key": "approval",
    })
    return {
        "id": oid,
        "name": "ui-smoke human gate",
        "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "steps": steps,
        "entry_step_id": steps[0]["id"],
        "state_schema": {},
        "trigger": "manual",
    }


def drop_fixture(oid: str) -> None:
    try:
        api(f"/api/orchestrations/{oid}", "DELETE")
    except Exception:
        pass


#: Roles a person types into or picks from — the controls a field's label names.
FIELD_ROLES = {"textbox", "combobox", "spinbutton", "searchbox"}


async def ax_fields(page) -> list[dict]:
    """What the browser's accessibility tree says each field is called and described as.

    Playwright's `page.accessibility` is gone; the tree comes from CDP. This is
    the name a screen reader announces — computed by the browser, not guessed
    from the DOM, which is the only way to see what a wrapping `<label>` adds.
    """
    cdp = await page.context.new_cdp_session(page)
    try:
        tree = await cdp.send("Accessibility.getFullAXTree")
    finally:
        await cdp.detach()
    out = []
    for node in tree["nodes"]:
        role = (node.get("role") or {}).get("value")
        if role not in FIELD_ROLES or node.get("ignored"):
            continue
        out.append({
            "role": role,
            "name": (node.get("name") or {}).get("value", ""),
            "desc": (node.get("description") or {}).get("value", ""),
        })
    return out


async def open_step(page, oid: str, step_name: str) -> None:
    """Open a fixture orchestration and the config panel of one of its steps."""
    await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE + 1.0)
    node = page.locator(".react-flow__node", has_text=step_name).first
    box = await node.bounding_box()
    if box is None:
        raise AssertionError(f"no canvas node named {step_name!r}")
    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await asyncio.sleep(1.0)


# ── checks ───────────────────────────────────────────────────────────────────

@check
async def check_token(browser) -> str:
    """Both processes agree on the internal token, so `/api/*` is reachable."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    r = await page.goto(f"{APP}/api/settings", wait_until="domcontentloaded")
    await ctx.close()
    if r is None or r.status != 200:
        raise AssertionError(
            f"GET /api/settings through the app answered {r.status if r else 'nothing'} — "
            "start the backend and the frontend with the same SYNAPSE_INTERNAL_TOKEN"
        )
    return "the app reaches the backend"


@check
async def check_bodylimit(browser) -> str:
    """A body over 10 MB reaches the backend whole through the app, and one over the limit is refused at once (F364).

    Next cut `proxy.ts`'s copy of a body at 10 MiB and sent it on under the full Content-Length: the backend waited
    for the rest until the request timed out, and a chat message with 8 MB of images answered 500. A 20 MiB import
    that is not a bundle must come back as the backend's 422; 33 MiB is over `MAX_REQUEST_BODY_BYTES` (32 MiB) and
    gets the 413. No browser: this is what the app's server does with a body.
    """
    def post(mib: int) -> tuple[int, str, float]:
        body = json.dumps({"pad": "a" * (mib * 1024 * 1024 - 11)}).encode()
        req = urllib.request.Request(f"{APP}/api/import", method="POST", data=body,
                                     headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()[:200], time.monotonic() - started
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:200], time.monotonic() - started
        except (TimeoutError, urllib.error.URLError):
            raise AssertionError(f"a {mib} MiB POST /api/import never came back: the body was cut short (F364)")

    status, text, took = post(20)
    if status != 422:
        raise AssertionError(f"a 20 MiB non-bundle through the app answered {status}, not the backend's 422: {text}")
    over, said, over_took = post(33)
    if over != 413 or "the most one request can carry is 32 MB" not in said:
        raise AssertionError(f"33 MiB answered {over}, not the 413 that says the limit: {said}")
    if over_took > 5:
        raise AssertionError(f"the 413 took {over_took:.1f}s")
    return f"20 MiB reached the backend (422 in {took:.1f}s); 33 MiB refused (413 in {over_took:.1f}s)"


@check
async def check_runbar(browser) -> str:
    """A run waiting for a person has an Answer control nothing else covers.

    Driven the way a person drives it — open the orchestration, press Run, wait
    for it to park — because that is what fills the run log and pushes the
    sticky strip down to the bottom of the viewport, where the floating
    AI-builder launcher owns a 44px corner at a 20px inset. Deep-linking to the
    run leaves the log short, the strip high, and the collision invisible.

    The fixture parks on a person before any model call, so this costs nothing
    and needs no provider key.
    """
    oid = f"orch_uismoke_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    gaps: list[int | None] = []
    try:
        for width in (WIDE, PHONE):
            ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
            page = await ctx.new_page()
            await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE + 2.0)
            await page.get_by_role("button", name="Run now", exact=True).click()
            # Settle on content, never networkidle: the notifications stream is
            # held open for the life of the page.
            for _ in range(25):
                await asyncio.sleep(1.0)
                if await page.evaluate(
                    "() => [...document.querySelectorAll('button')]"
                    ".some(b => /Waiting for your input/.test(b.innerText || ''))"
                ):
                    break
            # The question opens in its step's setup card by itself (M3a-2): on a
            # phone that is the bottom sheet, over the strip. The strip's Answer is
            # what brings it back once it is put away, so it is measured with the
            # sheet closed.
            await asyncio.sleep(1.0)
            grip = page.get_by_role("button", name="Drag down to close, or press to close")
            if await grip.count():
                await grip.click()
                await asyncio.sleep(0.8)
            verdict = await page.evaluate(
                """() => {
                  const bar = [...document.querySelectorAll('button')]
                      .find(b => /Waiting for your input/.test(b.innerText || ''));
                  if (!bar) return {found: false};
                  const pill = [...bar.querySelectorAll('*')]
                      .reverse().find(e => (e.innerText || '').trim() === 'Answer');
                  const r = (pill || bar).getBoundingClientRect();
                  const pts = [[r.x + r.width / 2, r.y + r.height / 2],
                               [r.right - 4, r.y + r.height / 2]];
                  const hits = pts.map(([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return 'nothing';
                    if (el.closest('button[aria-label^="Open the AI builder"]')) return 'the AI-builder launcher';
                    return bar.contains(el) ? 'the strip' : el.tagName.toLowerCase();
                  });
                  // The toast host owns the other end of the same corner. It is
                  // parked high enough to clear both the launcher and this strip,
                  // and it stacks upward, so its lowest edge is the whole story.
                  const host = document.querySelector('.fixed.bottom-20.right-6');
                  const h = host && host.getBoundingClientRect();
                  const toastGap = h && h.height ? Math.round(r.top - h.bottom) : null;
                  return {found: true, hits, toastGap, box: {x: Math.round(r.x), y: Math.round(r.y),
                          w: Math.round(r.width), h: Math.round(r.height)}};
                }"""
            )
            await ctx.close()
            if not verdict["found"]:
                raise AssertionError("the run never showed a 'Waiting for your input' strip")
            covered = [h for h in verdict["hits"] if h != "the strip"]
            if covered:
                raise AssertionError(
                    f"at {width[0]}px the Answer control at {verdict['box']} is covered by "
                    f"{covered[0]} — a person pressing Answer presses that instead"
                )
            gap = verdict["toastGap"]
            if gap is not None and gap < 0:
                raise AssertionError(
                    f"at {width[0]}px a toast overlaps the Answer strip by {-gap}px (F46)"
                )
            gaps.append(gap)
        seen = [g for g in gaps if g is not None]
        return (
            "Answer is clickable at 1600 and 400"
            + (f", and a toast clears the strip by {min(seen)}px" if seen else "")
        )
    finally:
        drop_fixture(oid)


# ── the AI builder ───────────────────────────────────────────────────────────

#: A builder stream, driven from the test rather than from the engine.
#:
#: `page.route` cannot help here: it fulfils a response whole, and what has to
#: be held is a stream that stays OPEN — that is the entire state the panel
#: gets wrong. So `fetch` is wrapped in the page, `/api/builder/chat` is
#: answered with a `ReadableStream` this script feeds frame by frame, and every
#: other call is recorded and passed through. Model-free, instant, free, and it
#: exercises the real panel, the real reducer and the real network calls.
BUILDER_STUB = r"""
window.__calls = [];
window.__stub = {};
const _fetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
  // A fresh panel: a conversation left open in the store would be reopened (F214).
  if (url.includes('/api/builder/conversation')) {
    return Promise.resolve(new Response('{"conversation": null}', {
      status: 200, headers: {'Content-Type': 'application/json'},
    }));
  }
  if (url.includes('/api/builder/chat')) {
    const body = new ReadableStream({
      start(c) {
        window.__stub.push = (event) =>
          c.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(event) + '\n\n'));
        window.__stub.close = () => { try { c.close(); } catch (e) {} };
      },
    });
    return Promise.resolve(new Response(body, {
      status: 200, headers: {'Content-Type': 'text/event-stream'},
    }));
  }
  window.__calls.push({url: url, method: method});
  return _fetch(input, init);
};
"""

PANEL = """() => {
  const panel = document.querySelector('[data-testid="assistant-panel"]');
  if (!panel) return {panel: false};
  const ta = panel.querySelector('textarea');
  const named = [...panel.querySelectorAll('button')].map((b) => ({
    label: (b.getAttribute('aria-label') || b.innerText || '').trim().slice(0, 40),
    disabled: b.disabled,
  }));
  return {
    panel: true,
    textareaDisabled: ta ? ta.disabled : null,
    stop: named.filter((b) => /stop/i.test(b.label)).map((b) => b.label),
    // `innerText` applies `text-transform`, so the label arrives uppercased.
    parked: /needs your answer/i.test(panel.innerText),
    buttons: named.map((b) => b.label),
  };
}"""

RUN_ID = "builder_uismoke00"


async def _build_with_ai(page):
    """Open the AI builder as a person does since M2c retired the corner launcher: the menu's Synapse AI row (Build with
    AI before ASSIST1; below `md`, the phone bar's mark). Found by its test id, not its words. Returns the button pressed."""
    buttons = page.locator('[data-testid="assistant-open"]')
    for _ in range(40):
        for i in range(await buttons.count()):
            button = buttons.nth(i)
            if await button.is_visible():
                await button.click()
                return button
        await asyncio.sleep(0.25)
    raise AssertionError("no visible Synapse AI row or mark to open the builder with")


async def _open_builder(browser, width=WIDE):
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    await ctx.add_init_script(BUILDER_STUB)
    page = await ctx.new_page()
    await page.goto(APP, wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE + 1.0)
    await _build_with_ai(page)
    await asyncio.sleep(0.5)
    await page.locator('[data-testid="assistant-panel"] textarea').fill("build me something")
    # Send is disabled until React has seen the change event `fill` dispatches.
    # Inside the builder: chat's composer behind it has a Send of its own since M2c.
    send = page.locator('[data-testid="assistant-panel"]').get_by_role("button", name="Send", exact=True)
    await send.wait_for()
    for _ in range(20):
        if not await send.is_disabled():
            break
        await asyncio.sleep(0.1)
    await send.click()
    # The stream's controller exists only once the panel has actually called
    # fetch; pushing a frame before that is a race the panel always wins.
    for _ in range(40):
        if await page.evaluate("() => typeof (window.__stub || {}).push === 'function'"):
            return ctx, page
        await asyncio.sleep(0.1)
    raise AssertionError("the panel never sent the message")


async def _settle(page, predicate_js: str, tries: int = 40) -> dict:
    """Poll the panel until it satisfies `predicate_js`, then return its state."""
    state = {}
    for _ in range(tries):
        await asyncio.sleep(0.25)
        state = await page.evaluate(PANEL)
        if await page.evaluate(f"(s) => ({predicate_js})(s)", state):
            return state
    return state


@check
async def check_builder(browser) -> str:
    """A build in flight can be stopped; a parked one can be answered or stopped.

    Two states, one panel. While a run works the composer is closed, so the
    only way out has to be a control — there was none, and a build runs for
    the best part of a minute. While a run is parked the composer opens (the
    stream ends at the pause here, unlike the commercial edition), and the
    other way out belongs on the question itself.
    """
    # ── a run in flight ──────────────────────────────────────────────────────
    ctx, page = await _open_builder(browser)
    try:
        await page.evaluate(
            "(id) => window.__stub.push({type: 'orchestration_start', run_id: id,"
            " orchestration_id: 'orch_native_builder'})", RUN_ID)
        await page.evaluate(
            "() => window.__stub.push({type: 'step_start', orch_step_id: 's1',"
            " step_name: 'Understand the request'})")
        state = await _settle(page, "(s) => s.stop.length > 0")
        if not state.get("stop"):
            raise AssertionError(
                "a builder run is streaming and the panel offers no way to stop it "
                f"(buttons: {state.get('buttons')}); the composer is "
                f"disabled={state.get('textareaDisabled')}, so there is no way out at all"
            )
        await page.get_by_role("button", name=state["stop"][0]).click()
        cancelled = await _settle(
            page, f"() => window.__calls.some(c => c.method === 'POST' "
                  f"&& c.url.includes('/runs/{RUN_ID}/cancel'))")
        calls = await page.evaluate("() => window.__calls")
        if not [c for c in calls if c["method"] == "POST" and f"/runs/{RUN_ID}/cancel" in c["url"]]:
            raise AssertionError(
                f"Stop was pressed and nothing cancelled the run; calls: {calls}")
    finally:
        await ctx.close()

    # ── a run parked on its question ─────────────────────────────────────────
    ctx, page = await _open_builder(browser)
    try:
        await page.evaluate(
            "(id) => window.__stub.push({type: 'orchestration_start', run_id: id})", RUN_ID)
        await page.evaluate(
            "(id) => window.__stub.push({type: 'human_input_required', run_id: id,"
            " orch_step_id: 's9', prompt: 'Which orchestration did you mean?',"
            " fields: [{name: 'answer'}]})", RUN_ID)
        await page.evaluate("() => window.__stub.close()")
        state = await _settle(page, "(s) => s.parked && s.textareaDisabled === false")
        if not state.get("parked"):
            raise AssertionError("the question never rendered")
        if state.get("textareaDisabled") is not False:
            raise AssertionError(
                "the run is parked on its question and the composer is disabled — "
                "the answer it is asking for cannot be typed")
        if not state.get("stop"):
            raise AssertionError(
                "a run parked on a question offers no way to end it: answering is "
                f"the only exit and walking away leaves it paused forever "
                f"(buttons: {state.get('buttons')})")
        await page.get_by_role("button", name=state["stop"][0]).click()
        await _settle(page, f"() => window.__calls.some(c => c.method === 'POST' "
                            f"&& c.url.includes('/runs/{RUN_ID}/cancel'))")
        calls = await page.evaluate("() => window.__calls")
        if not [c for c in calls if c["method"] == "POST" and f"/runs/{RUN_ID}/cancel" in c["url"]]:
            raise AssertionError(
                f"the parked run was not cancelled when asked; calls: {calls}")
    finally:
        await ctx.close()

    return "a build can be stopped in flight and while it waits"


# ── money ────────────────────────────────────────────────────────────────────

#: A usage summary with real money in it, none of it a round number of cents.
#:
#: Served to the page instead of the backend's, so the check needs no model
#: call, no provider key and no spend in the store, and asserts the same thing
#: on any instance. The figures are the ones F41 was reported with
#: (`total_cost` 0.0041545) plus a per-model row measured at 0.0003255 — both
#: rendered `$0.00` by a `toFixed(2)`.
USAGE_STUB = {
    "total_cost": 0.0041545,
    "total_input_tokens": 12000, "total_output_tokens": 3000, "total_tokens": 15000,
    "total_requests": 7, "total_cache_read_tokens": 0, "total_cache_write_tokens": 0,
    "total_estimated_savings": 0.0009431, "total_response_cache_hits": 0,
    "by_model": [
        {"model": "smoke-costly", "provider": "smoke", "requests": 4,
         "input_tokens": 9000, "output_tokens": 2000, "total_tokens": 11000,
         "estimated_cost": 0.0038290, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "estimated_savings": 0.0009431, "response_cache_hits": 0},
        {"model": "smoke-tiny", "provider": "smoke", "requests": 3,
         "input_tokens": 3000, "output_tokens": 1000, "total_tokens": 4000,
         "estimated_cost": 0.0003255, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "estimated_savings": 0.0, "response_cache_hits": 0},
    ],
    "by_session": [], "by_schedule": [],
}

SERVE_USAGE = """
window.__usage = %s;
const _f = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (url.includes('/api/usage/summary')) {
    return Promise.resolve(new Response(JSON.stringify(window.__usage), {
      status: 200, headers: {'Content-Type': 'application/json'},
    }));
  }
  return _f(input, init);
};
"""


@check
async def check_money(browser) -> str:
    """Money that was spent is never rendered as nothing.

    Home is titled "Where the workspace stands right now", and it read `$0.00`
    against a model that had cost real money — `toFixed(2)` on anything under
    half a cent. The Usage screen, reading the same API in the same instant,
    showed the true figure. A reader had no way to tell "nothing was spent"
    from "less than half a cent was spent", on the one screen whose job is to
    say which.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(SERVE_USAGE % json.dumps(USAGE_STUB))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/dashboard", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        shown = await page.evaluate(
            r"""() => {
              const txt = document.body.innerText;
              const tile = (label) => {
                const i = txt.indexOf(label);
                if (i < 0) return null;
                const m = txt.slice(i, i + 80).match(/\$[0-9][^\s]*/);
                return m ? m[0] : null;
              };
              return {
                total: tile('Total spend'),
                saved: tile('Cache saved'),
                rows: [...document.querySelectorAll('li')]
                    .map(li => li.innerText.replace(/\n/g, ' ').trim())
                    .filter(t => /^smoke-/.test(t)),
              };
            }"""
        )
    finally:
        await ctx.close()

    if shown["total"] is None:
        raise AssertionError("no 'Total spend' tile on /dashboard — did the screen move?")
    if not shown["rows"]:
        raise AssertionError(f"the stubbed models never reached 'Spend by model': {shown}")

    zeroed = []
    if shown["total"] in ("$0.00", "$0"):
        zeroed.append(f"Total spend shows {shown['total']} for {USAGE_STUB['total_cost']}")
    if shown["saved"] in ("$0.00", "$0"):
        zeroed.append(f"Cache saved shows {shown['saved']} for {USAGE_STUB['total_estimated_savings']}")
    for row, stat in zip(shown["rows"], USAGE_STUB["by_model"]):
        if row.endswith("$0.00") or row.endswith("$0"):
            zeroed.append(f"{stat['model']} shows $0.00 for {stat['estimated_cost']}")
    if zeroed:
        raise AssertionError(
            "money that was spent is rendered as nothing on Home — "
            + "; ".join(zeroed)
        )
    return f"Home reports {shown['total']} and every model row truthfully"


# ── layout ───────────────────────────────────────────────────────────────────

#: Screens to check for a page that scrolls when it should not. Five, because
#: the defect this exists for was in the app shell and so was on every one.
LAYOUT_PAGES = ("/dashboard", "/workflows", "/agents", "/vault", "/usage", "/knowledge")


@check
async def check_layout(browser) -> str:
    """A full-height page does not scroll, and a tooltip opens by its trigger.

    `Hint` wraps its child in an inline span, deliberately — that is how a
    tooltip still fires on a disabled button. Around a `fixed` button the span
    stays behind in the page flow, and after an `h-screen` shell it lands as
    one 24px line of body text: every page in the app scrolled onto a blank
    strip. The tooltip anchors to that span too, so the launcher's label opened
    in the bottom-left corner, half below the window, describing a button in
    the bottom-right.

    Both halves are asserted, plus the rule underneath them — nothing in the
    static flow may wrap something fixed — so the next control to reach for
    `Hint` cannot reintroduce this quietly.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        overflowing, wrappers = [], []
        for path in LAYOUT_PAGES:
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            found = await page.evaluate(
                r"""() => {
                  const doc = document.documentElement;
                  const over = doc.scrollHeight - window.innerHeight;
                  // Anything fixed whose ancestor chain is ordinary flow: the
                  // wrapper keeps a line of the page to itself.
                  const bad = [];
                  for (const el of document.querySelectorAll('*')) {
                    if (getComputedStyle(el).position !== 'fixed') continue;
                    const p = el.parentElement;
                    if (!p || p === document.body) continue;
                    if (getComputedStyle(p).position !== 'static') continue;
                    const r = p.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0 && r.top > 0) {
                      bad.push((el.getAttribute('aria-label') || el.tagName) +
                               ' inside a static <' + p.tagName.toLowerCase() + '> at y=' + Math.round(r.top));
                    }
                  }
                  return {over, bad};
                }"""
            )
            if found["over"] > 0:
                overflowing.append(f"{path} by {found['over']}px")
            wrappers.extend(f"{path}: {b}" for b in found["bad"])

        if overflowing:
            raise AssertionError(
                "pages scroll the document onto a blank strip — "
                + "; ".join(overflowing)
                + (f" (the culprit: {wrappers[0]})" if wrappers else "")
            )
        if wrappers:
            raise AssertionError(
                "a zero-sized element in the page flow wraps something fixed, which "
                "costs a line of every page and anchors its tooltip there — " + wrappers[0]
            )

        # A `Hint`'s tooltip, where a person would look for it. Its subject was the corner launcher until M2c
        # retired it; the builder's "Start a new conversation" is the `Hint` left in the shell.
        await page.goto(f"{APP}/dashboard", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        await _build_with_ai(page)
        await asyncio.sleep(0.6)
        launcher = page.locator('[data-testid="assistant-panel"] button[aria-label="Start a new conversation"]')
        box = await launcher.bounding_box()
        if box is None:
            raise AssertionError("no Start a new conversation in the builder on /dashboard")
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await asyncio.sleep(1.5)
        tip = await page.evaluate(
            r"""() => {
              const t = document.querySelector('[role="tooltip"]');
              if (!t) return null;
              const r = t.getBoundingClientRect();
              return {x: r.x, y: r.y, right: r.right, bottom: r.bottom,
                      text: (t.textContent || '').trim()};
            }"""
        )
    finally:
        await ctx.close()

    if tip is None:
        raise AssertionError("Start a new conversation offers no tooltip at all")
    if tip["bottom"] > WIDE[1] or tip["right"] > WIDE[0] or tip["x"] < 0 or tip["y"] < 0:
        raise AssertionError(
            f"the launcher's tooltip {tip['text']!r} is outside the window "
            f"(bottom {round(tip['bottom'])} of {WIDE[1]})"
        )
    # The nearer side: a tooltip may open on either side of its trigger.
    gap = min(abs(box["x"] - tip["right"]), abs(tip["x"] - (box["x"] + box["width"])), abs(tip["y"] - (box["y"] + box["height"])), abs(box["y"] - tip["bottom"]))
    if gap > 200:
        raise AssertionError(
            f"the launcher's tooltip {tip['text']!r} opens {round(gap)}px from the button "
            f"it describes — it is anchored to something else"
        )
    return f"no page scrolls, and a Hint's tooltip opens {round(gap)}px from its button"


# ── contrast ─────────────────────────────────────────────────────────────────

#: Both themes, because the defect this exists for was reported as a light-mode
#: one and was in fact in both: the light palette is an inversion of the dark
#: one, so it inherits the dark one's mistakes exactly.
CONTRAST_PAGES = (
    "/", "/agents", "/workflows", "/schedules", "/tools", "/vault", "/runs", "/logs",
    "/evals", "/apps", "/skills", "/kitchen-sink", "/templates",
    "/settings/general", "/settings/models", "/settings/scale", "/widgets",
    "/channels", "/channels/phone", "/settings/memory", "/settings/api-keys",
    # The rest of the rail (F101): the uses the inversion layer lacks sit mostly
    # on screens the walk never opened.
    "/dashboard", "/usage", "/settings/personal_details", "/settings/variables",
    "/settings/workspace", "/settings/repos", "/settings/db_configs",
    "/settings/import_export", "/settings/support", "/settings/datalab",
    "/knowledge",
)

#: A run log with a line of every kind, and a chat turn with its panels open —
#: states no page load reaches, and where most raw colour lives (F101).
CONTRAST_RUN_EVENTS = [
    {"type": "orchestration_start", "run_id": "run_contrast"},
    {"type": "step_start", "orch_step_id": "a", "step_name": "Research", "step_type": "agent"},
    {"type": "thinking", "orch_step_id": "a", "message": "Looking it up"},
    {"type": "tool_execution", "orch_step_id": "a", "tool_name": "search_web", "args": {"q": "x"}},
    {"type": "tool_result", "orch_step_id": "a", "tool_name": "search_web", "preview": "3 results"},
    {"type": "step_warning", "orch_step_id": "a", "message": "a warning line"},
    {"type": "step_complete", "orch_step_id": "a", "step_name": "Research", "duration_seconds": 1.2},
    {"type": "routing_decision", "orch_step_id": "r", "step_type": "evaluator", "decision": "yes",
     "target_step_id": "b"},
    {"type": "loop_iteration", "orch_step_id": "l", "iteration": 1},
    {"type": "step_error", "orch_step_id": "b", "error": "an error line"},
    {"type": "transform_result", "orch_step_id": "t", "result": "ok"},
]
CONTRAST_CHAT_EVENTS = [
    {"type": "status", "message": "Working"},
    {"type": "llm_reasoning", "reasoning": "Thinking about it.", "turn": 1},
    {"type": "llm_thought", "thought": "I will search.", "turn": 1},
    {"type": "tool_execution", "tool_name": "search_web", "args": {"q": "x"}},
    {"type": "tool_result", "tool_name": "search_web", "preview": "3 results"},
    {"type": "response", "content": "The answer, with **bold** and `code`."},
    {"type": "done"},
]

#: Known, owned, and deliberately not fixed here. `--danger` is one token doing
#: two jobs — it is the colour of 60 pieces of danger *text* on a dark ground
#: and the ground of 18 danger *buttons* under white text — and no single value
#: serves both. Splitting it changes the kit, which is synced to the other
#: product, so it belongs to the synced-UI batch and not to this one. Anything
#: not named here is a regression.
#: Matched on the colours themselves, not on class names: a class attribute is
#: long, generated and truncated, while the pair of colours IS the defect.
#: Empty since F100 split `--danger`: the one waiver was white on the danger
#: ground, which now takes `--danger-fg`.
CONTRAST_WAIVED: tuple[tuple[str, str, str], ...] = ()

#: Contrast as the browser computes it, not as the stylesheet claims it.
#:
#: Colours are resolved by painting them, because Tailwind v4 emits lab()/oklch()
#: and a hand-written rgb() parser silently reads those as black — which invents
#: failures and hides real ones. Decorative and disabled elements are skipped:
#: an aria-hidden icon is not text, and WCAG exempts a disabled control.
CONTRAST = r"""
() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const g = cv.getContext('2d', {willReadFrequently: true});
  const cache = new Map();
  const rgba = (s) => {
    if (cache.has(s)) return cache.get(s);
    g.clearRect(0,0,1,1); g.fillStyle = '#000'; g.fillStyle = s;
    g.clearRect(0,0,1,1); g.fillRect(0,0,1,1);
    const d = g.getImageData(0,0,1,1).data;
    const v = {rgb:[d[0],d[1],d[2]], a: d[3]/255};
    cache.set(s, v); return v;
  };
  const lum = (c) => {
    const [r,gg,b] = c.map(v => { v/=255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
    return 0.2126*r + 0.7152*gg + 0.0722*b;
  };
  const over = (fg, bg) => fg.rgb.map((v,i) => v*fg.a + bg[i]*(1-fg.a));
  const bgOf = (el) => {
    for (let p = el; p; p = p.parentElement) {
      const c = rgba(getComputedStyle(p).backgroundColor);
      if (c.a >= 0.999) return c.rgb;
      if (c.a > 0) return over(c, p.parentElement ? bgOf(p.parentElement) : [255,255,255]);
    }
    return [255,255,255];
  };
  const hex = (c) => '#' + c.map(v => Math.round(v).toString(16).padStart(2,'0')).join('');
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    if (el.closest('[aria-hidden="true"]')) continue;          // decoration, not text
    if (el.closest(':disabled, [disabled], [aria-disabled="true"]')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    const bg = bgOf(el);
    const f = lum(over(rgba(cs.color), bg)), b = lum(bg);
    const ratio = (Math.max(f,b) + 0.05) / (Math.min(f,b) + 0.05);
    const px = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight, 10) >= 700;
    const need = (px >= 24 || (px >= 18.66 && bold)) ? 3.0 : 4.5;
    if (ratio + 0.005 < need) out.push({
      ratio: +ratio.toFixed(2), need, tag: el.tagName.toLowerCase(),
      fg: hex(over(rgba(cs.color), bg)), bg: hex(bg),
      cls: (el.className?.baseVal ?? el.className ?? '').toString().slice(0, 70),
      txt: (el.textContent || '').trim().replace(/\s+/g,' ').slice(0, 30)});
  }
  return out;
}
"""


@check
async def check_contrast(browser) -> str:
    """Text meets AA against what is actually painted behind it, in both themes.

    Light mode is built by inverting the dark ramp, so a colour the inversion
    layer forgot keeps its dark-mode value on a light ground: amber-200 body
    text on the inverted amber-950 tint read 1.2:1, which is not faint text but
    no text. The same reading also showed the reported "light mode" fault was
    in both themes — raw zinc-600 was 2.46:1 light and 2.58:1 dark — and that
    the --text-faint token itself missed AA in dark on its own page ground.
    """
    bad: list[str] = []
    for theme in ("light", "dark"):
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        await ctx.add_init_script(
            f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}"
        )
        page = await ctx.new_page()

        async def measure(where: str) -> None:
            for row in await page.evaluate(CONTRAST):
                if any(row["fg"] == fg and row["bg"] == bg for fg, bg, _ in CONTRAST_WAIVED):
                    continue
                bad.append(
                    f"{theme} {where}: {row['ratio']}:1 (needs {row['need']}) "
                    f"{row['fg']} on {row['bg']} — {row['txt'][:24]!r}"
                )
        try:
            for path in CONTRAST_PAGES:
                await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
                await asyncio.sleep(SETTLE)
                await measure(path)
        finally:
            await ctx.close()

        # A run log with a line of every kind, on a stubbed stream.
        oid = f"orch_uismoke_contrast_{int(time.time())}"
        api("/api/orchestrations", "POST", human_gate_orchestration(oid))
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
        await ctx.add_init_script(RUNLOG_STUB)
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE + 2.0)
            await page.get_by_role("button", name="Run now", exact=True).click()
            for _ in range(30):
                if await page.evaluate("() => !!window.__run.raw"):
                    break
                await asyncio.sleep(0.1)
            await page.evaluate("(evs) => evs.forEach(e => window.__run.raw(e))", CONTRAST_RUN_EVENTS)
            await asyncio.sleep(1.5)
            await measure("a run log")
        finally:
            await ctx.close()
            try:
                api(f"/api/orchestrations/{oid}", "DELETE")
            except Exception:
                pass

        # The "?" panel (H1) over a knowledge base: its steps, its guide link, and the clip with its pause control.
        ctx, _ = await _help_context(browser, routes=list(_SMOKE_KB_ROUTES))
        await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
            await page.get_by_role("button", name="Upload files").wait_for(timeout=30_000)
            await asyncio.sleep(SETTLE)
            _, dialog = await _open_help(page)
            await dialog.get_by_role("button", name="Pause the clip").wait_for(timeout=10_000)
            await measure("the help panel")
        finally:
            await ctx.close()

        # A chat turn with its panels open, on a stubbed stream.
        ctx, page = await _chat_page(browser, f"uismoke-contrast-{theme}")
        try:
            await page.evaluate(f"() => localStorage.setItem('synapseTheme', '{theme}')")
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            box = page.locator("main textarea").last
            await box.fill("contrast turn")
            await box.press("Enter")
            await _stream_open(page, "post")
            frames = [_frame(e, f"k-{i}") for i, e in enumerate(CONTRAST_CHAT_EVENTS)]
            await page.evaluate("(fs) => fs.forEach(f => window.__streams.post.push(f))", frames)
            await asyncio.sleep(1.5)
            for toggle in await page.get_by_role("button", name=re.compile("reasoning|thinking", re.I)).all():
                await toggle.click()
            await asyncio.sleep(0.5)
            await measure("a chat turn")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError(f"{len(bad)} under AA — " + "; ".join(bad[:5]))
    return (
        f"{len(CONTRAST_PAGES)} screens, a run log, the help panel and a chat turn in both themes meet AA"
        + (f" ({len(CONTRAST_WAIVED)} waived)" if CONTRAST_WAIVED else "")
    )


# ── phone ────────────────────────────────────────────────────────────────────

#: The five shell screens the `layout` check uses, plus the one page that is not
#: built on `Screen` — which is where both phone-width defects actually were.
PHONE_PAGES = LAYOUT_PAGES + ("/kitchen-sink", "/settings/models", "/settings/scale")

#: Two questions the rendered page answers and a stylesheet cannot.
#:
#: Sideways: not "is anything wider than the viewport" — content inside a scroll
#: container is, legitimately — but "does the page itself move when pushed".
#:
#: Stranded: the shell's <main> is `overflow-hidden` by design, so a screen that
#: does not bring its own scroller has its overflow silently cut off instead of
#: scrolled. That is content a person cannot reach by any gesture.
PHONE_OVERFLOW = r"""
() => {
  window.scrollTo(2000, 0);
  const sideways = Math.round(window.scrollX);
  window.scrollTo(0, 0);
  const main = document.querySelector('main');
  let stranded = 0;
  if (main && getComputedStyle(main).overflowY === 'hidden') {
    stranded = Math.max(0, main.scrollHeight - main.clientHeight);
  }
  let blame = null;
  if (sideways) {
    const vw = document.documentElement.clientWidth;
    // An in-flow box is clipped by any overflow ancestor; an absolutely
    // positioned one only by an ancestor that is also its containing block.
    const clipped = (el) => {
      const absolute = getComputedStyle(el).position === 'absolute';
      for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
        const o = getComputedStyle(p);
        if (!/(auto|scroll|hidden|clip)/.test(o.overflowX)) continue;
        if (!absolute || o.position !== 'static') return true;
      }
      return false;
    };
    for (const el of document.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.right <= vw + 1 || clipped(el)) continue;
      blame = el.tagName.toLowerCase() + '.' +
              (el.className?.baseVal ?? el.className ?? '').toString().split(/\s+/).slice(0, 3).join('.') +
              ' reaching ' + Math.round(r.right) + 'px';
      break;
    }
  }
  return {sideways, stranded, blame};
}
"""


@check
async def check_phone(browser) -> str:
    """At phone width a page does not slide sideways, and none of it is stranded.

    Both halves were real on /kitchen-sink. It slid 242px, and not because
    anything visible was too wide: an `sr-only` table header is absolutely
    positioned, and the scroll container it sits in was not a positioned
    ancestor, so 1px of invisible text was placed against the document. And
    3,498px of the page could not be reached at all, because the shell's main
    is `overflow-hidden` and that page brought no scroller of its own.

    Asked by pushing the page rather than by measuring boxes: content inside a
    scroll container is wider than the viewport on purpose.
    """
    ctx = await browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]})
    page = await ctx.new_page()
    try:
        bad: list[str] = []
        for path in PHONE_PAGES:
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            r = await page.evaluate(PHONE_OVERFLOW)
            if r["sideways"]:
                bad.append(f"{path} slides {r['sideways']}px sideways ({r['blame'] or 'cause not isolated'})")
            if r["stranded"]:
                bad.append(f"{path} strands {r['stranded']}px inside an overflow-hidden main")
        if bad:
            raise AssertionError("; ".join(bad))
        return f"{len(PHONE_PAGES)} screens at {PHONE[0]}px: none slides, none stranded"
    finally:
        await ctx.close()


#: Each Section header with actions on a page: the title's width against the header's, and whether
#: the actions sit below the title (their top under the title's bottom).
SECTION_HEADERS = r"""
() => [...document.querySelectorAll('section > header')]
  .filter((h) => h.children.length === 2)
  .map((h) => {
    const [title, actions] = [...h.children].map((c) => c.getBoundingClientRect());
    const head = h.getBoundingClientRect();
    return {
      text: (h.querySelector('h2')?.textContent || '').trim().slice(0, 40),
      share: title.width / head.width,
      below: actions.top >= title.bottom - 1,
    };
  })
"""


@check
async def check_sectionphone(browser) -> str:
    """At phone width a section's actions go under its title (F839): kept beside it, a wide control
    squeezed the title and description into a ~90 px column (cloud's Settings → Plan at 390 px). Above
    `sm` the header stays one line (M2g), which `layout` and the contrast checks see at desktop width."""
    ctx = await browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]})
    page = await ctx.new_page()
    try:
        bad: list[str] = []
        seen = 0
        for path in ("/settings/models", "/settings/api-keys", "/settings/scale"):
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            for h in await page.evaluate(SECTION_HEADERS):
                seen += 1
                if not h["below"] or h["share"] < 0.6:
                    bad.append(f"{path} '{h['text']}': title {h['share']:.0%} of the header, "
                               f"actions {'below' if h['below'] else 'beside'} it")
        if not seen:
            raise AssertionError("no section header with actions was found to measure")
        if bad:
            raise AssertionError("; ".join(bad))
        return f"{seen} section headers with actions at {PHONE[0]}px: the actions under the title"
    finally:
        await ctx.close()


# ── builder context ──────────────────────────────────────────────────────────

#: Enough of a stream to let the panel finish a turn without a model call.
#: Named apart from BUILDER_STUB, which is an init script: one clobbered the
#: other and Playwright injected this as JavaScript.
BUILDER_TURN = (
    'data: {"type":"assistant_message","content":"ok"}\n\n'
    'data: {"type":"done"}\n\n'
)


@check
async def check_builderctx(browser) -> str:
    """A builder message names the orchestration the person is actually on.

    "Change with AI" on an orchestration screen tells the builder which one, so
    that "add a step to this" has a referent. That answer was kept in a ref
    that only ever got written when a caller passed one — and the app-wide
    launcher passes nothing, while a new conversation cleared everything else.
    So after one build on an orchestration, every later message named it: from
    another screen, and in a conversation started to talk about something else.

    Driven through the app with the builder POST stubbed, so it costs no model
    call, and read off the request body rather than off the screen — the id is
    never rendered anywhere (F17).
    """
    oid = f"orch_uismoke_ctx_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    sent: list[str | None] = []
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        async def stub(route):
            try:
                body = json.loads(route.request.post_data or "{}")
            except Exception:
                body = {}
            sent.append(body.get("current_orchestration_id"))
            await route.fulfill(status=200, content_type="text/event-stream", body=BUILDER_TURN)
        await page.route("**/api/builder/chat", stub)
        # A fresh panel: a conversation left open in the store would be reopened (F214).
        await page.route("**/api/builder/conversation", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"conversation": null}'))

        async def say(text: str):
            box = page.locator("textarea").last
            await box.fill(text)
            await box.press("Enter")
            await asyncio.sleep(1.5)

        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        # The open canvas's own way in, in its bar: "Change with AI" (M3a; it was the
        # toolbar's "Build with AI"). The rail's row is Synapse AI (ASSIST1).
        await page.locator("main").get_by_role("button", name="Change with AI").first.click()
        await asyncio.sleep(1.2)
        await say("one")                       # on the orchestration: names it
        # Client-side navigation, the way a person moves. A hard load would
        # remount the provider and the ref would look innocent.
        await page.click('a[href="/agents"]')
        await asyncio.sleep(SETTLE)
        await say("two")                       # a different screen: names nothing
        await page.get_by_role("button", name="Start a new conversation").first.click()
        await asyncio.sleep(1.2)
        await say("three")                     # a new conversation: names nothing
    finally:
        await ctx.close()
        drop_fixture(oid)
    if len(sent) != 3:
        raise AssertionError(f"expected 3 builder turns, saw {len(sent)}: {sent}")
    if sent[0] != oid:
        raise AssertionError(f"Build with AI on {oid} sent {sent[0]!r} — it should name that one")
    stale = [w for w, got in zip(("from another screen", "in a new conversation"), sent[1:]) if got]
    if stale:
        raise AssertionError(
            f"the builder still named {sent[1] or sent[2]!r} " + " and ".join(stale)
        )
    return "a build names its orchestration, and only while it is the one open"


# ── delegation ───────────────────────────────────────────────────────────────

#: The chat stream, held open from the test. A fulfilled response body ends
#: the moment it is written, and the page — correctly — calls that a turn that
#: died early and clears the line being measured. The activity line only exists
#: while a turn is in flight, so the turn has to stay in flight.
DELEGATE_STUB = r"""
const _fetch = window.fetch;
window.__chat = {};
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (url.includes('/api/chat/stream')) {
    const body = new ReadableStream({
      start(c) {
        const send = (e) =>
          c.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(e) + '\n\n'));
        window.__chat.ready = true;
        send({type: 'status', message: 'Working'});
        send({type: 'thinking', message: 'I will ask the researcher'});
        send({type: 'tool_execution', tool_name: 'search_web', args: {q: 'tides'},
              delegate_from: 'agent_main', delegate_agent_name: 'Researcher'});
        // deliberately never closed
      },
    });
    return Promise.resolve(new Response(body, {
      status: 200, headers: {'Content-Type': 'text/event-stream'},
    }));
  }
  return _fetch(input, init);
};
"""


#: The run's own stream, answered in the page: the test pushes step events into
#: it one batch at a time, so the log grows while the reader is on it — with no
#: engine, no model and nothing slow.
RUNLOG_STUB = r"""
const _fetch = window.fetch;
window.__run = {};
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (/\/api\/orchestrations\/[^/]+\/run$/.test(url)) {
    const body = new ReadableStream({
      start(c) {
        let n = 0;
        window.__run.raw = (e) =>
          c.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(e) + '\n\n'));
        window.__run.push = (count) => {
          for (let i = 0; i < count; i++, n++) {
            c.enqueue(new TextEncoder().encode('data: ' + JSON.stringify({
              type: 'step_start', orch_step_id: 's' + n, step_name: 'Step number ' + n,
              step_type: 'print'}) + '\n\n'));
          }
        };
        // deliberately never closed: the run is still going
      },
    });
    return Promise.resolve(new Response(body, {
      status: 200, headers: {'Content-Type': 'text/event-stream'},
    }));
  }
  return _fetch(input, init);
};
"""

#: Where the run log's scroller stands: the nearest scrolling ancestor of a line.
RUNLOG_POSITION = r"""() => {
  const line = [...document.querySelectorAll('div.font-mono *')]
      .find(e => /Step number/.test(e.textContent || ''));
  let el = line;
  while (el && !(el.scrollHeight > el.clientHeight + 2 &&
                 /(auto|scroll)/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
  if (!el) return null;
  window.__scroller = el;
  const chip = [...document.querySelectorAll('button')]
      .some(b => /Jump to latest/.test(b.innerText || ''));
  return {gap: Math.round(el.scrollHeight - el.scrollTop - el.clientHeight),
          top: Math.round(el.scrollTop), chip};
}"""


@check
async def check_runfollow(browser) -> str:
    """The run log follows the newest line, and leaves a reader who scrolled up alone.

    Its effect set `scrollTop` on the log's own div, which does not scroll — the
    panel does — so the log never followed at all: measured, the scroller stayed
    at the top while fourteen lines arrived below it (F127). Now it follows while
    the reader is at its foot, stops when they scroll up, and "Jump to latest"
    starts it again.
    """
    oid = f"orch_uismoke_follow_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(RUNLOG_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run now", exact=True).click()
        for _ in range(30):
            if await page.evaluate("() => !!window.__run.push"):
                break
            await asyncio.sleep(0.1)
        await page.evaluate("() => window.__run.push(60)")
        await asyncio.sleep(0.8)
        at = await page.evaluate(RUNLOG_POSITION)
        if at is None:
            raise AssertionError("sixty lines never made the run log scroll")
        if at["gap"] > 24:
            raise AssertionError(f"the log did not follow its newest line: {at['gap']}px below the view")

        await page.evaluate("() => { window.__scroller.scrollTop = 0; }")
        await asyncio.sleep(0.3)
        await page.evaluate("() => window.__run.push(10)")
        await asyncio.sleep(0.8)
        held = await page.evaluate(RUNLOG_POSITION)
        if held["top"] > 0:
            raise AssertionError(f"a reader who scrolled up was moved to {held['top']}px")
        if not held["chip"]:
            raise AssertionError("scrolled up, the log offers no way back to its newest line")

        await page.get_by_role("button", name="Jump to latest").click()
        await asyncio.sleep(0.5)
        await page.evaluate("() => window.__run.push(5)")
        await asyncio.sleep(0.8)
        back = await page.evaluate(RUNLOG_POSITION)
        if back["gap"] > 24 or back["chip"]:
            raise AssertionError(f"'Jump to latest' did not start following again: {back}")
        return "the run log follows its newest line, and not a reader who scrolled up"
    finally:
        await ctx.close()
        try:
            api(f"/api/orchestrations/{oid}", "DELETE")
        except Exception:
            pass


@check
async def check_delegate(browser) -> str:
    """A delegate's work is shown as the delegate's, not as the agent's own.

    When an agent hands a task to another agent, the sub-agent keeps streaming
    on the same channel: its thinking, its tool calls, its results. The engine
    tags each of those with the agent that produced it; the route rebuilt every
    event as a fixed dict and dropped the tag, so chat announced that the agent
    you asked was searching the web when a different agent was (F7).

    Stubbed at the browser, so it needs no second agent, no delegation and no
    model call — what is under test is whether the name survives the journey.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(DELEGATE_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(APP, wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        box = page.locator("main textarea").last
        await box.fill("when is high tide?")
        await box.press("Enter")
        activity = ""
        for _ in range(30):
            await asyncio.sleep(0.4)
            activity = await page.evaluate(
                r"""() => document.body.innerText.split('\n')
                       .filter(l => /search web/i.test(l)).join(' | ')"""
            )
            if activity:
                break
        if not activity:
            raise AssertionError("the delegated tool call never reached the screen")
        if not re.search(r"researcher", activity, re.I):
            raise AssertionError(
                f"a delegate's tool call is shown as the agent's own: {activity!r}"
            )
        return "a delegate's tool call is shown under the delegate's name"
    finally:
        await ctx.close()


# ── tool globals ─────────────────────────────────────────────────────────────

#: A Python tool that reads one workspace variable. Model-written arguments are
#: never rendered with globals any more; a tool receives exactly the ones it
#: declares, as `_globals` (F28's engine half, batch 1A).
GLOBALS_TOOL = {
    "name": "uismoke_globals_tool", "generalName": "UI smoke globals tool",
    "description": "reads one declared global", "tool_type": "python",
    "code": "import json\nprint(json.dumps(sorted(_globals)))\n",
    "inputSchema": {"type": "object", "properties": {}}, "schemaParams": [],
    "globals": ["UISMOKE_KEY"],
}


@check
async def check_toolglobals(browser) -> str:
    """A Python tool's declared globals can be seen, kept and tested.

    Since the engine stopped rendering model-written arguments, the only way a
    Python tool receives a workspace variable is to declare it — and the editor
    had no field for that, so no tool built in the app could receive one. Worse,
    the save path rebuilt the tool from a list of fields that did not include
    `globals`, so a tool given its declaration anywhere else (the API, the AI
    builder) lost it the next time anyone opened it and pressed Save (F28).

    The Test run is read off its request body rather than executed: it runs in
    a Docker sandbox this check has no business needing.
    """
    api("/api/tools/custom", "POST", GLOBALS_TOOL)
    saved: list[dict] = []
    tested: list[dict] = []
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        async def test_stub(route):
            try:
                tested.append(json.loads(route.request.post_data or "{}"))
            except Exception:
                tested.append({})
            await route.fulfill(status=200, content_type="application/json",
                                body='{"stdout": "[]", "stderr": "", "exit_code": 0}')
        await page.route("**/api/tools/python/test", test_stub)
        page.on("request", lambda r: saved.append(json.loads(r.post_data or "{}"))
                if r.method == "POST" and r.url.endswith("/api/tools/custom") else None)

        await page.goto(f"{APP}/tools", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        # The list beside the editor (M2f): a row opens its tool.
        await page.locator(f'[data-tool-row="{GLOBALS_TOOL["name"]}"] > button:first-child').first.click()
        await asyncio.sleep(1.5)
        # "(not set)" is appended on an instance that has no such variable.
        shown = await page.get_by_role("button", name=re.compile(r"^Remove UISMOKE_KEY")).count()
        # Declare one more through the picker, when the workspace has one to offer.
        # The check never writes variables itself: that route replaces them all.
        added = None
        # The kit's Combobox opens from a Radix popover trigger, which is a button.
        picker = page.get_by_role("button", name="Add a variable this tool can read")
        if await picker.count():
            await picker.click()
            await asyncio.sleep(0.5)
            option = page.get_by_role("option").first
            added = (await option.inner_text()).strip()
            await option.click()
            await asyncio.sleep(0.5)
        # The editor's test button is called "Run".
        await page.get_by_role("button", name="Run", exact=True).first.click()
        await asyncio.sleep(1.2)
        await page.get_by_role("button", name=re.compile(r"^save", re.I)).first.click()
        await asyncio.sleep(2.0)
    finally:
        await ctx.close()
        stored = [t for t in api("/api/tools/custom") if t.get("name") == GLOBALS_TOOL["name"]]
        try:
            api(f"/api/tools/custom/{GLOBALS_TOOL['name']}", "DELETE")
        except Exception:
            pass
    problems = []
    if not shown:
        problems.append("the editor does not show the global the tool declares")
    want = ["UISMOKE_KEY"] + ([added] if added else [])
    if not saved or saved[-1].get("globals") != want:
        problems.append(f"Save sent globals={saved[-1].get('globals', '<absent>') if saved else '<no save>'},"
                        f" wanted {want}")
    if not stored or stored[0].get("globals") != want:
        problems.append("opening the tool and pressing Save erased its declaration")
    if not tested or tested[-1].get("globals") != want:
        problems.append(f"Test ran with globals={tested[-1].get('globals', '<absent>') if tested else '<no test>'}, not what a run gets")
    if problems:
        raise AssertionError("; ".join(problems))
    return ("a declared global is shown, survives Save, and reaches Test"
            + (f"; {added} declared through the picker" if added else "; no variable to pick here"))


# ── scale ────────────────────────────────────────────────────────────────────

#: Settings the scale layer reads from the environment only. The screen may
#: show them; saving one never changed anything (F58).
SCALE_ENV_ONLY = {
    "Worker Concurrency": "WORKER_CONCURRENCY",
    "Max Global Queue Depth": "MAX_GLOBAL_QUEUE_DEPTH",
    "Rate Limit": "RATE_LIMIT_PER_TENANT_RPS",
    "Metrics Token": "METRICS_TOKEN",
    "PgBouncer": "PGBOUNCER_MODE",
}

#: One dead letter whose run never reached a step — the retry route's 409.
DLQ_ENTRY = [{"id": "dlq_uismoke", "run_id": "run_uismoke_dlq", "job_id": "j1",
              "error_message": "Redis went away", "attempt_count": 1,
              "created_at": "2026-09-18T00:00:00Z", "resolved": False}]
DLQ_409 = "This run never reached a step, so there is nothing to resume — start it again."


@check
async def check_scale(browser) -> str:
    """The Scale screen offers only what saving can change, and says why not.

    Five fields are read by the scale layer from the environment alone. The
    screen rendered each as an ordinary input, and a Save reported success
    while nothing changed: 37 typed into Worker Concurrency, 10 still in
    effect. Queue Shards named a setting that no longer exists; Register posted
    to a route that never existed (405); the setup guide said WORKER_MAX_RETRIES
    counts retries before the dead-letter queue, which stopped being true when
    a dead job began to be dead-lettered on its first try; and the dead-letter
    retry button threw away the route's 409 — "this run never reached a step"
    — and redrew the list as though it had worked (F58).
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": 1400})
    page = await ctx.new_page()
    problems: list[str] = []
    try:
        await page.route("**/api/scale/dlq", lambda r: asyncio.ensure_future(
            r.fulfill(status=200, content_type="application/json", body=json.dumps(DLQ_ENTRY))))
        await page.route("**/api/scale/dlq/*/retry", lambda r: asyncio.ensure_future(
            r.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": DLQ_409}))))
        await page.goto(f"{APP}/settings/scale", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        sections = ("Redis connection", "Postgres", "Workers", "Observability")
        for title in sections:
            await page.get_by_role("button", name=re.compile(f"^{title}")).first.click()
            await asyncio.sleep(0.3)
        editable = await page.evaluate("""() => [...document.querySelectorAll('input')]
            .filter(i => !i.disabled && !i.readOnly && i.getBoundingClientRect().width > 0)
            .map(i => (i.labels && i.labels[0] && i.labels[0].innerText.split('\\n')[0])
                      || i.getAttribute('aria-label') || '')""")
        # The configuration sections only: the deployment guide sits in the DOM
        # too, and its text would answer for a field that says nothing itself.
        # innerText applies text-transform, so everything compares lowercased.
        text = (await page.evaluate("""(titles) => [...document.querySelectorAll('button')]
            .filter(b => titles.some(t => (b.textContent || '').trim().startsWith(t)))
            .map(b => b.parentElement.innerText).join('\\n')""", list(sections))).lower()
        for label, env in SCALE_ENV_ONLY.items():
            if any(label.lower() in e.lower() for e in editable):
                problems.append(f"{label} is editable, and saving it changes nothing")
            if env.lower() not in text:
                problems.append(f"{label} does not say it comes from {env}")
        if any("queue shards" in e.lower() for e in editable) or "queue shards" in text:
            problems.append("Queue Shards is still offered")
        if await page.get_by_role("button", name=re.compile("register", re.I)).count():
            problems.append("Register is still offered (POSTs a route that does not exist)")

        retry = page.get_by_role("button", name=re.compile(r"^retry", re.I)).first
        if not await retry.count():
            problems.append("the dead-letter retry button has no name")
        else:
            await retry.click()
            await asyncio.sleep(1.2)
            if DLQ_409.split(",")[0] not in await page.evaluate("() => document.body.innerText"):
                problems.append("a refused retry (409) says nothing")

        await page.get_by_role("button", name=re.compile("deploy docs", re.I)).first.click()
        await asyncio.sleep(0.6)
        await page.get_by_role("button", name=re.compile("^Environment variables reference")).first.click()
        await asyncio.sleep(0.6)
        guide = await page.evaluate("() => document.body.innerText")
        if "before sending to DLQ" in guide:
            problems.append("the guide says WORKER_MAX_RETRIES counts retries before the DLQ")
        if "NUM_QUEUE_SHARDS" in guide:
            problems.append("the guide still documents NUM_QUEUE_SHARDS")
        # S3 became real in batch 1C: a configured bucket is the whole blob
        # store, installed when each process starts, and nothing already on
        # local disk is carried across (F57).
        if "S3_PREFIX" not in guide:
            problems.append("the guide does not list S3_PREFIX, which the environment honours")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
        await page.get_by_role("button", name=re.compile("^Storage \\(S3\\)")).first.click()
        await asyncio.sleep(0.4)
        storage = (await page.evaluate("""() => [...document.querySelectorAll('button')]
            .filter(b => (b.textContent || '').trim().startsWith('Storage (S3)'))
            .map(b => b.parentElement.innerText).join(' ')""")).lower()
        # The secret access key is write-only (F95): whatever is stored, the
        # screen is never given it. Read off the field, not the route's promise.
        secret_value = await page.get_by_label("Secret Access Key").input_value()
        if secret_value:
            problems.append("the S3 secret access key is read back into the screen")
        if "s3_secret_access_key_set" not in api("/api/scale/config"):
            problems.append("the route does not say whether an S3 secret is stored")
        if "restart" not in storage:
            problems.append("the S3 section does not say a bucket takes effect on restart")
        if "not copied" not in storage:
            problems.append("the S3 section does not say files already on disk stay there")
    finally:
        await ctx.close()
    if problems:
        raise AssertionError("; ".join(problems))
    return "only saveable settings are editable; environment ones name their variable"


# ── models ───────────────────────────────────────────────────────────────────

@check
async def check_models(browser) -> str:
    """The Bedrock card does not offer a setting the engine stopped reading.

    Since batch 1B an inference profile is a model: discovery lists each one as
    `bedrock.<id>`, and the workspace-wide `bedrock_inference_profile` override
    is no longer applied. The card still offered it as a select, and when none
    was chosen it warned that one was needed "to use AWS Bedrock" — sending
    people to set a value that does nothing, and hiding the thing that works.
    Every settings save posted it too (F66).

    A stored value is kept, not cleared: the on-demand refusal names it as the
    model to pick. The screen stops sending the key, and the route merges.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": 1200})
    page = await ctx.new_page()
    posts: list[dict] = []
    page.on("request", lambda r: posts.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/settings") else None)
    problems: list[str] = []
    try:
        await page.goto(f"{APP}/settings/models", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name=re.compile("^AWS Bedrock")).click()
        await asyncio.sleep(1.2)
        if await page.get_by_label("Bedrock inference profile").count():
            problems.append("the card still offers a workspace inference profile")
        text = (await page.evaluate("() => document.querySelector('main').innerText")).lower()
        if "select an inference profile above to use aws bedrock" in text:
            problems.append("it still says Bedrock needs a profile selected")
        if "inference profiles are listed as models" not in text:
            problems.append("nothing says an inference profile is picked as the model")
        await page.get_by_role("button", name="Save changes").click()
        await asyncio.sleep(2.0)
    finally:
        await ctx.close()
    if not posts:
        problems.append("Save sent nothing")
    elif any("bedrock_inference_profile" in p for p in posts):
        problems.append("Save still posts bedrock_inference_profile")
    if problems:
        raise AssertionError("; ".join(problems))
    return "Bedrock offers profiles as models, and no workspace override"


# ── unsaved agent edits ──────────────────────────────────────────────────────

def _agent(aid: str, name: str) -> dict:
    return {"id": aid, "name": name, "description": f"{name}, a ui-smoke fixture",
            "system_prompt": "You help.", "tools": [], "repos": [],
            "type": "conversational", "avatar": "default", "max_turns": 30}


@check
async def check_unsaved(browser) -> str:
    """An unsaved agent edit is not thrown away without asking.

    The Agents screen replaced its draft wholesale whenever another agent was
    opened, and nothing stopped a person leaving through the rail. Measured:
    rename an agent, click another, come back — the rename is gone; rename it
    again, click Orchestrations — gone, and no word either time (F73).

    Both ways out are driven: switching agents, and leaving through a link. Each
    is refused once (the edit must survive "Keep editing") and then taken.
    """
    a, b = "agent_uismoke_a", "agent_uismoke_b"
    api("/api/agents", "POST", _agent(a, "UI smoke Alpha"))
    api("/api/agents", "POST", _agent(b, "UI smoke Beta"))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    problems: list[str] = []
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        name = page.get_by_label(re.compile(r"^name$", re.I)).first
        dialog = page.get_by_role("dialog")

        async def asked() -> bool:
            await asyncio.sleep(0.8)
            return await dialog.count() > 0

        # ── switching agents ──
        await page.get_by_text("UI smoke Alpha", exact=True).first.click()
        await asyncio.sleep(0.8)
        await name.fill("UI smoke Alpha, renamed")
        await page.get_by_text("UI smoke Beta", exact=True).first.click()
        if not await asked():
            problems.append("switching agents discarded an unsaved edit without asking")
        else:
            await dialog.get_by_role("button", name=re.compile("cancel|keep", re.I)).first.click()
            await asyncio.sleep(0.6)
            if await name.input_value() != "UI smoke Alpha, renamed":
                problems.append("keeping the edit did not keep it")
            await page.get_by_text("UI smoke Beta", exact=True).first.click()
            await asyncio.sleep(0.8)
            await dialog.get_by_role("button", name=re.compile("discard", re.I)).first.click()
            await asyncio.sleep(0.8)
            if await name.input_value() != "UI smoke Beta":
                problems.append("discarding did not open the other agent")

        # ── a saved edit is not an unsaved one ──
        # The route answers with what it stored; that becomes the baseline, or
        # every switch after a save would ask about changes already kept.
        await name.fill("UI smoke Beta, saved")
        await page.get_by_role("button", name=re.compile(r"^save", re.I)).first.click()
        await asyncio.sleep(1.5)
        await page.get_by_text("UI smoke Alpha", exact=True).first.click()
        if await asked():
            problems.append("switching after a save asked about changes that were saved")
            await dialog.get_by_role("button", name=re.compile("discard", re.I)).first.click()
            await asyncio.sleep(0.6)
        await page.get_by_text("UI smoke Beta, saved", exact=True).first.click()
        await asyncio.sleep(0.8)

        # ── leaving the screen ──
        await name.fill("UI smoke Beta, renamed")
        await page.click('a[href="/workflows"]')
        if not await asked():
            problems.append("leaving through the rail discarded an unsaved edit without asking")
        else:
            await dialog.get_by_role("button", name=re.compile("cancel|keep", re.I)).first.click()
            await asyncio.sleep(0.6)
            if not page.url.endswith("/agents"):
                problems.append(f"keeping the edit still left, for {page.url}")
            await page.click('a[href="/workflows"]')
            await asyncio.sleep(0.8)
            await dialog.get_by_role("button", name=re.compile("discard", re.I)).first.click()
            await asyncio.sleep(1.5)
            if not page.url.endswith("/workflows"):
                problems.append("discarding did not go where the link pointed")
    finally:
        await ctx.close()
        for aid in (a, b):
            try:
                api(f"/api/agents/{aid}", "DELETE")
            except Exception:
                pass
    stored = {x["id"]: x["name"] for x in api("/api/agents") if x["id"] in (a, b)}
    if stored:
        problems.append(f"fixtures left behind: {stored}")
    if problems:
        raise AssertionError("; ".join(problems))
    return "an unsaved edit is kept or discarded on purpose, on switch and on leave"


# ── MCP presets ──────────────────────────────────────────────────────────────

#: How each remote preset authenticates, from the commercial edition's catalog,
#: whose entries were probed live (tools/list, and RFC 8414 metadata for OAuth).
MCP_PRESET_AUTH = {"GitHub Copilot": "bearer", "Slack": "oauth", "Figma": "oauth",
                   "Notion": "oauth", "Vercel": "oauth"}


@check
async def check_mcppresets(browser) -> str:
    """A preset leaves the token empty, and says what to put there when it wants one.

    A non-empty token skips OAuth. The Slack and Figma presets pre-filled the
    names of environment variables — SLACK_CLIENT_ID, FIGMA_PERSONAL_ACCESS_TOKEN
    — into the token field, so both went out as a bearer token that was not one,
    and never reached the sign-in both servers actually use. GitHub's filled in
    a variable name too, where a real token belongs (F74).
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": 1200})
    page = await ctx.new_page()
    problems: list[str] = []
    try:
        await page.goto(f"{APP}/apps", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        # The add form is on the page (M2f); an older layout opened it first.
        if not await page.get_by_role("button", name=re.compile(r"By web address \(URL\)")).count():
            opener = page.get_by_role("button", name=re.compile(r"^(add|new)", re.I)).first
            if await opener.count():
                await opener.click()
                await asyncio.sleep(0.8)
        await page.get_by_role("button", name=re.compile(r"By web address \(URL\)")).first.click()
        await asyncio.sleep(0.5)
        token = page.locator("#mcp-token")
        for label, auth in MCP_PRESET_AUTH.items():
            await page.get_by_role("button", name=label, exact=True).first.click()
            await asyncio.sleep(0.4)
            value = await token.input_value()
            if value:
                problems.append(f"{label} pre-fills the token with {value!r}"
                                + (" and so skips its OAuth sign-in" if auth == "oauth" else ""))
            form = (await page.evaluate("() => document.querySelector('main').innerText")).lower()
            if auth == "bearer" and "with access to the repositories" not in form:
                problems.append(f"{label} wants a token and does not say which")
            if auth == "oauth" and f"{label.lower()} signs you in with oauth" not in form:
                problems.append(f"{label} signs in with OAuth and the form does not say so")
    finally:
        await ctx.close()
    if problems:
        raise AssertionError("; ".join(problems))
    return f"{len(MCP_PRESET_AUTH)} remote presets leave the token empty and say how they authenticate"


# ── guardrails ───────────────────────────────────────────────────────────────

@check
async def check_guardrails(browser) -> str:
    """An orchestration's guardrails are the ones the engine enforces.

    "Max Cost (USD)" sat beside Max Total Turns and Timeout as though it were
    the same kind of thing. Nothing ever read it: batch 1C deleted the field
    from the engine for that reason. A cap that does not cap is worse than
    none — a person who set it believed a runaway run would stop (F76).
    """
    oid = f"orch_uismoke_guard_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        # The Guardrails tab under the old canvas is a section of the open canvas's
        # Workflow settings now (M3a).
        await page.get_by_role("button", name="Workflow settings", exact=True).first.click()
        await asyncio.sleep(0.8)
        turns = await page.get_by_label("Max Total Turns").count()
        cost = await page.get_by_label(re.compile("max cost", re.I)).count()
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not turns:
        raise AssertionError("Workflow settings did not open its Guardrails (no Max Total Turns)")
    if cost:
        raise AssertionError("Guardrails still offers Max Cost (USD), which nothing enforces")
    return "Guardrails offers turns and a timeout, and no cost cap that does nothing"


# ── reasoning effort ─────────────────────────────────────────────────────────

async def _reasoning_levels(page):
    """The agent editor's Reasoning choices and which one is shown, or None.

    A dropdown since M2d (Naveen): the shown level is the trigger's text, the
    choices are its options. Read in the same shape the button row gave —
    `[{name, pressed}]`, lowercase names — so the checks below say what they
    always said.
    """
    trigger = page.get_by_role("combobox", name="Reasoning")
    if not await trigger.count():
        return None
    shown = (await trigger.first.inner_text()).strip().lower()
    await trigger.first.click()
    await asyncio.sleep(0.4)
    names = [(t or "").strip().lower() for t in await page.get_by_role("option").all_inner_texts()]
    await page.keyboard.press("Escape")
    return [{"name": n, "pressed": "true" if n == shown else "false"} for n in names]

@check
async def check_effort(browser) -> str:
    """A Claude CLI agent's reasoning effort can be set, and offers no false Off.

    Since F65 `cli.claude` passes the agent's effort as `--effort`. The editor
    shows its reasoning control only for models the server lists as taking one,
    and the Claude CLI was not on that list — so the setting that had just
    started working could not be reached. It has no "off" either: the engine
    runs off as low, so an Off button there would mean Low.

    Needs the Claude CLI on this machine, because that is what lists its models;
    without it there is nothing to show, and the check says so rather than
    passing quietly.
    """
    models = api("/api/models")
    claude = sorted(m for m in models.get("all_available", []) if m.startswith("cli.claude."))
    if not claude:
        return "skipped — no Claude CLI on this machine, so no cli.claude model to open"
    aid = "agent_uismoke_effort"
    api("/api/agents", "POST", {**_agent(aid, "UI smoke CLI"), "model": claude[0],
                                "reasoning_effort": "off"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": 1200})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        await page.get_by_text("UI smoke CLI", exact=True).first.click()
        await asyncio.sleep(1.0)
        levels = await _reasoning_levels(page)
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    if levels is None:
        raise AssertionError(f"{claude[0]} takes an effort and the editor offers no reasoning control")
    names = [lv["name"] for lv in levels]
    if "off" in names:
        raise AssertionError(f"{claude[0]} is offered Off, which runs as Low")
    pressed = [lv["name"] for lv in levels if lv["pressed"] == "true"]
    if pressed != ["low"]:
        raise AssertionError(f"a stored 'off' shows as {pressed}, but it runs as low")
    return f"{claude[0]}: effort offered as {'/'.join(names)}, and a stored off shows as the low it runs at"


@check
async def check_mcpplaceholder(browser) -> str:
    """A server an older preset saved with a placeholder token says so.

    The Slack, Figma and GitHub presets filled the token with an environment
    variable's *name*; a non-empty token skips OAuth, so such a server never
    signs in and its row said only "disconnected" (F103). F74 fixed the presets,
    not the servers already saved. Their URLs point at nothing here, so no
    third party is dialled.
    """
    servers = {
        "uismoke_ph_slack": ("Slack", "SLACK_CLIENT_ID", "preset"),
        "uismoke_ph_github": ("GitHub Copilot", "GITHUB_PERSONAL_ACCESS_TOKEN", "personal access token"),
    }
    # And one with a real-looking token, which must never reach the page (F135).
    secret = "uismoke-bearer-7f3a9c"
    for name, (label, token) in {**{n: v[:2] for n, v in servers.items()},
                                 "uismoke_ph_real": ("Real token", secret)}.items():
        api("/api/mcp/servers", "POST", {"name": name, "label": label, "server_type": "remote",
                                         "url": "http://127.0.0.1:9/mcp", "token": token}, timeout=60)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    served: list[str] = []

    async def keep(response):
        if "/api/mcp/servers" in response.url:
            try:
                served.append(await response.text())
            except Exception:
                pass
    page.on("response", keep)
    try:
        await page.goto(f"{APP}/apps", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        rows = await page.evaluate("""(names) => names.map(n => {
          const tag = [...document.querySelectorAll('span')].find(s => (s.textContent || '').trim() === n);
          const row = tag && tag.closest('[data-mcp-row]');
          return row ? row.innerText : null;
        })""", list(servers))
    finally:
        await ctx.close()
        for name in [*servers, "uismoke_ph_real"]:
            try:
                api(f"/api/mcp/servers/{name}", "DELETE")
            except Exception:
                pass
    if not served:
        raise AssertionError("the page never read /api/mcp/servers")
    if any(secret in body for body in served):
        raise AssertionError("a server's bearer token reached the browser")
    for (name, (label, token, fix)), text in zip(servers.items(), rows):
        if text is None:
            raise AssertionError(f"the {label} server's row never rendered")
        if "placeholder" not in text.lower() or token not in text:
            raise AssertionError(f"the {label} row does not say its token is a placeholder: {text!r}")
        if fix not in text.lower():
            raise AssertionError(f"the {label} row does not say what to do about it: {text!r}")
    return "a placeholder token says so and what to do; no token reaches the browser"


@check
async def check_effortdefault(browser) -> str:
    """An agent with no effort of its own shows the one it runs at.

    The control drew every such agent as Medium, while the engine uses the
    workspace's setting — and, with none, Low for the Claude CLI (F65's rule)
    and Medium for the rest (F99). The workspace setting is read, never written:
    "unset" cannot be put back through the API, and for a CLI agent it is not
    the same as Medium.
    """
    models = api("/api/models")
    claude = sorted(m for m in models.get("all_available", []) if m.startswith("cli.claude."))
    if not claude:
        return "skipped — no Claude CLI on this machine, so no cli.claude model to open"
    workspace = (api("/api/settings").get("reasoning_effort") or "").strip().lower()
    runs_at = {"off": "low"}.get(workspace, workspace) or "low"
    aid = "agent_uismoke_effortdefault"
    api("/api/agents", "POST", {**_agent(aid, "UI smoke default effort"), "model": claude[0]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": 1200})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        await page.get_by_text("UI smoke default effort", exact=True).first.click()
        await asyncio.sleep(1.0)
        levels = await _reasoning_levels(page)
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    if levels is None:
        raise AssertionError(f"{claude[0]} takes an effort and the editor offers no reasoning control")
    pressed = [lv["name"] for lv in levels if lv["pressed"] == "true"]
    if len(pressed) != 1 or not pressed[0].startswith("default") or runs_at not in pressed[0]:
        raise AssertionError(
            f"an agent with no effort shows {pressed}, but it runs at the workspace's: {runs_at}")
    return f"an agent with no effort shows '{pressed[0]}', the level it runs at"


# ── headers ──────────────────────────────────────────────────────────────────

#: Reachable only by typing the URL, so it appears in no rail and a crawl of
#: the links would miss it — which is exactly how it went unnoticed that it
#: was announcing itself as another screen (F45).
UNLISTED_SCREENS = ("/settings/datalab",)


@check
async def check_headers(browser) -> str:
    """No screen wears another screen's name.

    A settings tab with no nav entry fell back to General's entry, so
    /settings/datalab rendered the title "General" and General's blurb, and
    offered no way out of Settings either. Nothing failed: the page was
    complete, correct and about a different screen. Titles are collected from
    every tab the rail offers plus the ones it does not, and a title that
    appears twice is the fallback firing.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/general", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        offered = await page.evaluate(
            "() => [...new Set([...document.querySelectorAll('a[href^=\"/settings/\"]')]"
            ".map(a => a.getAttribute('href')))]"
        )
        seen: dict[str, str] = {}
        clash: list[str] = []
        homeless: list[str] = []
        for path in list(offered) + list(UNLISTED_SCREENS):
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(1.0)
            title = await page.evaluate("() => document.querySelector('h1')?.innerText?.trim() ?? ''")
            if not title:
                homeless.append(f"{path}: no title at all")
            elif title in seen:
                clash.append(f"{path} calls itself {title!r}, which is {seen[title]}")
            else:
                seen[title] = path
            if not await page.evaluate(
                "() => [...document.querySelectorAll('a,button')]"
                ".some(e => /close/i.test(e.getAttribute('aria-label') || ''))"
            ):
                homeless.append(f"{path}: no way out of settings")
        if clash or homeless:
            raise AssertionError("; ".join(clash + homeless))
        return f"{len(seen)} settings screens, each with its own name and an exit"
    finally:
        await ctx.close()


# ── runner ───────────────────────────────────────────────────────────────────

#: Screens whose controls are checked for a name. Every settings tab plus the
#: three screens with a form of their own; the create forms are opened where a
#: "New…" button leads to one.
LABEL_PAGES = (
    "/", "/agents", "/workflows", "/schedules", "/tools", "/vault", "/runs", "/logs",
    "/evals", "/apps", "/skills", "/templates",
    "/settings/general", "/settings/models", "/settings/agents", "/widgets",
    "/settings/databases", "/settings/schedules", "/channels", "/settings/scale",
    "/settings/memory", "/settings/api-keys", "/settings/integrations", "/settings/custom-tools",
    "/knowledge",
)

#: What the browser itself says a control is called. Not a static rule: a label
#: can be a `span` that only looks like one, a `sr-only` checkbox with a
#: decorative wrapper, or text hidden at this breakpoint — none of which a
#: linter sees, and all of which were in the app (F42).
NAMELESS = r"""
() => {
  const named = (el) => {
    if (el.getAttribute('aria-label')?.trim()) return true;
    const by = el.getAttribute('aria-labelledby');
    if (by && by.split(/\s+/).some(id => document.getElementById(id)?.innerText?.trim())) return true;
    if (el.labels && el.labels.length && [...el.labels].some(l => l.innerText.trim())) return true;
    if (el.closest('label') && el.closest('label').innerText.trim()) return true;
    if (el.getAttribute('title')?.trim()) return true;
    if (el.tagName === 'BUTTON' && el.innerText.trim()) return true;
    return false;
  };
  const out = [];
  for (const el of document.querySelectorAll(
      'input:not([type=hidden]), select, textarea, button, [contenteditable="true"]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;   // not on this screen
    if (named(el)) continue;
    out.push((el.getAttribute('placeholder') || el.tagName.toLowerCase())
             + ' — ' + el.outerHTML.slice(0, 70).replace(/\s+/g, ' '));
  }
  return out;
}
"""


#: An icon sitting beside a label is decoration, and a decoration that speaks
#: says the name twice. Chrome builds a button's name from its contents, so an
#: `<img alt="Google Gemini">` next to the words "Google Gemini" is announced
#: "Google Gemini Google Gemini" — and the Local V1 Compatible card, which
#: borrows Ollama's icon, announced somebody else's name entirely (F44).
TALKATIVE_ICONS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('button, a[href], [role="button"]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const words = el.innerText.trim();
    if (!words) continue;              // an icon-only control NEEDS its icon to speak
    for (const icon of el.querySelectorAll('img, [role="img"], [aria-label]')) {
      if (icon.closest('[aria-hidden="true"]')) continue;
      const says = (icon.getAttribute('alt') ?? icon.getAttribute('aria-label') ?? '').trim();
      if (!says) continue;
      out.push(JSON.stringify(words.split('\n')[0].slice(0, 40)) + ' also says ' + JSON.stringify(says));
    }
  }
  return out;
}
"""


@check
async def check_labels(browser) -> str:
    """Every control a person can reach says what it is.

    A screen reader announces an unlabelled input as "edit text", and a
    placeholder disappears the moment someone types — so a form of them is a
    form nobody can re-read. This walks the app and asks the page, because the
    cases that were actually in it are invisible to a linter: `Label` renders a
    `span` unless it is given `htmlFor`, a `label` with neither `htmlFor` nor a
    wrapped control names nothing, and a toggle built from a `button` and two
    `div`s has no name at all.

    It also asks the opposite question — whether anything says its name twice.
    A control's name is built from its contents, so a brand icon that kept its
    `alt` is read out beside the label it illustrates (F44).
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        nameless: list[str] = []
        talkative: list[str] = []
        for path in LABEL_PAGES:
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            nameless.extend(f"{path}: {row}" for row in await page.evaluate(NAMELESS))
            talkative.extend(f"{path}: {row}" for row in await page.evaluate(TALKATIVE_ICONS))
            # The forms live behind the screen's own "New…" button.
            opener = page.get_by_role("button", name=re.compile(r"^(new|add|create)\b", re.I)).first
            try:
                await opener.click(timeout=2000)
            except Exception:
                continue
            await asyncio.sleep(1.2)
            nameless.extend(f"{path} (create form): {row}" for row in await page.evaluate(NAMELESS))
        if nameless:
            raise AssertionError(
                f"{len(nameless)} control(s) with no accessible name — " + "; ".join(nameless[:6])
            )
        if talkative:
            raise AssertionError(
                f"{len(talkative)} control(s) announce their icon as well as their label — "
                + "; ".join(talkative[:6])
            )
        return (
            f"every control on {len(LABEL_PAGES)} screens has a name, and says it once"
        )
    finally:
        await ctx.close()


# ── a field's name is its label ──────────────────────────────────────────────

@check
async def check_fieldnames(browser) -> str:
    """A field is called what its label says — the hint is its description, once.

    The kit `Field` wrapped label, control and hint in one `<label>`, and a
    label's text *is* the name of the control inside it, so every hinted field
    was announced "Output key The state key this step writes…", and then the
    same sentence again as its description (F30). Asked of the browser's own
    accessibility tree, on the step panel (the kit's densest use) and two
    screens with hints.
    """
    oid = f"orch_uismoke_names_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    seen, doubled = 0, []
    try:
        for where, go in (
            ("step panel", lambda: open_step(page, oid, "Approve")),
            ("/settings/scale", lambda: page.goto(f"{APP}/settings/scale", wait_until="domcontentloaded")),
            ("/kitchen-sink", lambda: page.goto(f"{APP}/kitchen-sink", wait_until="domcontentloaded")),
        ):
            await go()
            await asyncio.sleep(SETTLE)
            for f in await ax_fields(page):
                if not f["desc"]:
                    continue
                seen += 1
                if f["desc"].strip() and f["desc"].strip() in f["name"]:
                    doubled.append(f"{where}: {f['name'][:80]!r}")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not seen:
        raise AssertionError("no described field found — the screens did not render")
    if doubled:
        raise AssertionError(
            f"{len(doubled)} field(s) carry their hint in their name as well as their description — "
            + "; ".join(doubled[:5])
        )
    return f"{seen} described fields, each named by its label alone"


# ── a human step's prompt ────────────────────────────────────────────────────

@check
async def check_humanprompt(browser) -> str:
    """A human step's prompt is edited like the engine fills it: `{state.key}` only.

    It was a plain textarea while every other prompt was the markdown editor,
    so it offered no keys at all. The engine fills a human prompt with its own
    single-brace pass and never renders `{{ }}` — deliberately: the prompt can
    be posted to a messaging channel, where a workspace variable would leak.
    So `{s` must offer the run's state and `{{` must offer nothing (F5, D104).
    """
    oid = f"orch_uismoke_human_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await open_step(page, oid, "Approve")
        box = page.get_by_role("textbox", name="Prompt for human", exact=True)
        if not await box.count():
            raise AssertionError("no textbox named 'Prompt for human' on the human step's panel")
        if await box.get_attribute("aria-autocomplete") != "list":
            raise AssertionError("the human prompt is a plain textarea — it offers no state keys")
        await box.click()
        await page.keyboard.press("End")
        await page.keyboard.type(" {s")
        await asyncio.sleep(0.5)
        offered = await page.get_by_role("listbox", name="Suggestions").get_by_role("option").all_inner_texts()
        if not any("line_" in o for o in offered):
            raise AssertionError(f"typing {{s offered {offered!r}, not the run's state keys")
        await page.keyboard.press("Escape")
        await page.keyboard.type(" {{ ")
        await asyncio.sleep(0.5)
        doubled = await page.get_by_role("listbox", name="Suggestions").count()
        if doubled:
            raise AssertionError("typing {{ opened suggestions — a human prompt never renders {{ }}")
    finally:
        await ctx.close()
        drop_fixture(oid)
    return f"{{s offers {len(offered)} state key(s); {{{{ offers nothing"


# ── the palette and the graph ────────────────────────────────────────────────

def laid_out_orchestration(oid: str, n: int = 3) -> dict:
    """`n` print steps in a row, 280 px apart — how Tidy up and the builder place them."""
    steps = [{
        "id": f"{oid}_s{i}",
        "name": f"Step {i + 1}",
        "type": "print",
        "print_content": f"step {i + 1}",
        "next_step_id": f"{oid}_s{i + 1}" if i < n - 1 else None,
        "output_key": f"out_{i}",
        "position_x": 280 * i,
        "position_y": 0,
    } for i in range(n)]
    return {
        "id": oid, "name": "ui-smoke laid out", "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "steps": steps, "entry_step_id": steps[0]["id"], "state_schema": {}, "trigger": "manual",
    }


NODES_UNDER_CHROME = r"""
() => {
  const chrome = [...document.querySelectorAll('[data-canvas-chrome]')]
    .map(el => ({name: el.getAttribute('data-canvas-chrome'), r: el.getBoundingClientRect()}))
    .filter(c => c.r.width && c.r.height);
  const hits = [];
  for (const n of document.querySelectorAll('.react-flow__node')) {
    const r = n.getBoundingClientRect();
    for (const {name, r: p} of chrome) {
      if (r.left < p.right && r.right > p.left && r.top < p.bottom && r.bottom > p.top)
        hits.push(`${n.innerText.split('\n')[0]} under the ${name} (${Math.round(r.left)},${Math.round(r.top)})`);
    }
  }
  return {chrome: chrome.map(c => c.name), hits};
}
"""


@check
async def check_canvasfit(browser) -> str:
    """Fitting the graph leaves room for everything that floats over it.

    Fit-view used the whole canvas while the palette was drawn on top of its
    left edge, so a workflow opened with its first step under the palette (F31).
    The open canvas (M3a) floats a bar, a dock, a zoom cluster and the run strip
    over the graph instead: no step may open, or be fitted, under any of them.
    """
    oid = f"orch_uismoke_fit_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        opened = await page.evaluate(NODES_UNDER_CHROME)
        await page.get_by_role("button", name="Fit view").click()
        await asyncio.sleep(0.8)
        refit = await page.evaluate(NODES_UNDER_CHROME)
    finally:
        await ctx.close()
        drop_fixture(oid)
    if "bar" not in opened["chrome"] or "dock" not in opened["chrome"]:
        raise AssertionError(f"the canvas's floating bar and dock are missing: {opened['chrome']}")
    problems = [f"on open: {h}" for h in opened["hits"]] + [f"after Fit view: {h}" for h in refit["hits"]]
    if problems:
        raise AssertionError("steps under the canvas's chrome — " + "; ".join(problems))
    return "every step clear of the bar, dock, zoom and strip, on open and after Fit view"


@check
async def check_palettelink(browser) -> str:
    """A step added from the step list follows the step that is selected.

    The palette only appended: four steps added one after another ran as one,
    the first completing and the run ending, while the editor said only
    "Unreachable from the entry point" (F47). Now a selected linear step with
    no next step points at the new one and hands it its output key, so adding
    steps in a row builds a chain. The list is the dock's Add step since M3a
    (D211: steps come from ⌘K). Read from what Save actually sends.
    """
    oid = f"orch_uismoke_link_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 1))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []

    def on_request(req):
        if req.method == "POST" and req.url.endswith("/api/orchestrations"):
            try:
                sent.append(json.loads(req.post_data or "{}"))
            except ValueError:
                pass

    page.on("request", on_request)
    try:
        await open_step(page, oid, "Step 1")
        for _ in range(2):
            await page.get_by_role("button", name="Add step", exact=True).click()
            await page.get_by_role("option", name=re.compile(r"^Print")).first.click()
            await asyncio.sleep(0.5)
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.5)
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not sent:
        raise AssertionError("Save sent nothing")
    steps = {s["id"]: s for s in sent[-1]["steps"]}
    first = steps[f"{oid}_s0"]
    added = [s for s in sent[-1]["steps"] if s["id"] != first["id"]]
    if len(added) != 2:
        raise AssertionError(f"expected two added steps, saved {len(added)}")
    a, b = added
    problems = []
    if first.get("next_step_id") != a["id"]:
        problems.append(f"Step 1's next step is {first.get('next_step_id')!r}, not the step added after it")
    if "out_0" not in (a.get("input_keys") or []):
        problems.append(f"the added step's input keys are {a.get('input_keys')!r} — Step 1's output key is not among them")
    if a.get("next_step_id") != b["id"]:
        problems.append("the second added step does not follow the first")
    if problems:
        raise AssertionError("; ".join(problems))
    return "two steps added from the step list chain on from the selected step"


# ── the open canvas (M3a, D204) ──────────────────────────────────────────────

def roles_orchestration(oid: str) -> dict:
    """One step for each role (D211): an LLM (a model), a print (logic) and a human (a person)."""
    steps = [
        {"id": f"{oid}_m", "name": "Draft", "type": "llm", "prompt_template": "Say hi", "next_step_id": f"{oid}_l",
         "output_key": "draft", "position_x": 0, "position_y": 0},
        {"id": f"{oid}_l", "name": "Note", "type": "print", "print_content": "noted", "next_step_id": f"{oid}_h",
         "output_key": "note", "position_x": 280, "position_y": 0},
        {"id": f"{oid}_h", "name": "Approve", "type": "human", "human_prompt": "OK?", "next_step_id": None,
         "output_key": "ok", "position_x": 560, "position_y": 0},
    ]
    return {"id": oid, "name": "ui-smoke roles", "description": "Temporary fixture created by scripts/ui-smoke.py.",
            "steps": steps, "entry_step_id": steps[0]["id"], "state_schema": {}, "trigger": "manual"}


CARD_BOX = """() => {
  const a = document.querySelector('aside[aria-label="Step configuration"]');
  const c = a && a.closest('[data-canvas-chrome="card"]');
  if (!c) return null;
  const r = c.getBoundingClientRect();
  return {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
}"""

NODE_BOX = """(id) => {
  const n = document.querySelector(`.react-flow__node[data-id="${id}"]`);
  if (!n) return null;
  const r = n.getBoundingClientRect();
  return {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
}"""


async def click_node(page, step_id: str) -> None:
    box = await page.evaluate(NODE_BOX, step_id)
    if box is None:
        raise AssertionError(f"no canvas node {step_id}")
    await page.mouse.click((box["left"] + box["right"]) / 2, (box["top"] + box["bottom"]) / 2)
    await asyncio.sleep(0.8)


async def click_empty_canvas(page) -> None:
    pane = await page.locator(".react-flow__pane").bounding_box()
    await page.mouse.click(pane["x"] + 30, pane["y"] + 100)
    await asyncio.sleep(0.6)


@check
async def check_canvascard(browser) -> str:
    """The open canvas's setup card (D204): it opens on a step and gets out of the way.

    It opens when a step is selected; Esc, its ✕ and a click on empty canvas
    close it, and Esc hands the focus back to the step. Its left edge resizes it
    within 300–560 px. A step the card would cover slides into view.
    """
    oid = f"orch_uismoke_card_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 4))
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        if await page.evaluate(CARD_BOX) is not None:
            problems.append("a card was open before any step was selected")
        last = f"{oid}_s3"
        await click_node(page, last)
        card = await page.evaluate(CARD_BOX)
        node = await page.evaluate(NODE_BOX, last)
        if card is None:
            raise AssertionError("selecting a step opened no setup card")
        if node["right"] > card["left"] and node["left"] < card["right"]:
            problems.append(f"the card covers the step it opened for (step to x {round(node['right'])}, card from {round(card['left'])})")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.6)
        if await page.evaluate(CARD_BOX) is not None:
            problems.append("Esc did not close the card")
        focused = await page.evaluate("() => document.activeElement?.closest('.react-flow__node')?.getAttribute('data-id')")
        if focused != last:
            problems.append(f"after Esc the focus is on {focused!r}, not the step")
        await click_node(page, last)
        await page.get_by_role("button", name="Close panel").click()
        await asyncio.sleep(0.5)
        if await page.evaluate(CARD_BOX) is not None:
            problems.append("✕ did not close the card")
        await click_node(page, last)
        await click_empty_canvas(page)
        if await page.evaluate(CARD_BOX) is not None:
            problems.append("a click on empty canvas did not close the card")
        await click_node(page, last)
        handle = page.get_by_role("separator", name="Resize the panel")
        before = int(await handle.get_attribute("aria-valuenow"))
        await handle.focus()
        for _ in range(30):
            await page.keyboard.press("ArrowLeft")
        wide = int(await handle.get_attribute("aria-valuenow"))
        for _ in range(40):
            await page.keyboard.press("ArrowRight")
        narrow = int(await handle.get_attribute("aria-valuenow"))
        if wide != 560 or narrow != 300:
            problems.append(f"the card resized from {before} to {wide} and {narrow}, not within 300–560")
        await handle.focus()
        for _ in range(3):
            await page.keyboard.press("ArrowLeft")  # leave it at 348, not at a bound
    finally:
        await page.evaluate("() => localStorage.removeItem('orch-card-width')")
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "the card opens on a step, clears it, closes on Esc, ✕ and empty canvas, and resizes within 300–560"


@check
async def check_canvaskeys(browser) -> str:
    """Delete removes a step only from the canvas, and Undo brings it back whole.

    xyflow takes Delete and Backspace from the whole page, skipping only text
    fields, so Backspace on a focused button in the step's panel ("Set as entry
    point") deleted the step (found in M3a). The keys act now only while the
    person is working on the canvas. And Undo (Ctrl+Z) restores a deleted step
    with its connections, read from what Save sends.
    """
    oid = f"orch_uismoke_keys_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 3))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/orchestrations") else None)
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        middle = f"{oid}_s1"
        await click_node(page, middle)
        await page.get_by_role("button", name="Set as entry point").focus()
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.6)
        if await page.locator(".react-flow__node").count() != 3:
            problems.append("Backspace on the panel's Set as entry point deleted the step")
        edges = await page.locator(".react-flow__edge").count()
        await click_node(page, middle)
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.6)
        if await page.locator(".react-flow__node").count() != 2:
            problems.append("Delete on the selected step did not remove it")
        await page.keyboard.press("Control+z")
        await asyncio.sleep(0.8)
        if await page.locator(".react-flow__node").count() != 3:
            problems.append("Ctrl+Z did not bring the step back")
        if await page.locator(".react-flow__edge").count() != edges:
            problems.append("Ctrl+Z brought the step back without its connections")
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.2)
        if not sent:
            problems.append("Save sent nothing")
        else:
            steps = {st["id"]: st for st in sent[-1]["steps"]}
            if steps.get(f"{oid}_s0", {}).get("next_step_id") != middle or steps.get(middle, {}).get("next_step_id") != f"{oid}_s2":
                problems.append("the saved workflow lost the restored step's links")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "Backspace in the panel leaves the step, Delete on the canvas removes it, and Undo restores it with its links"


@check
async def check_canvasdrop(browser) -> str:
    """A step type dragged from Add step lands where it is dropped (D211, D204).

    Steps come from ⌘K now, not a side palette; dragging one onto an exact spot
    still works, from the dock's list. The app's ⌘K lists the same steps while a
    canvas is open.
    """
    oid = f"orch_uismoke_drop_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 1))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Add step", exact=True).click()
        option = page.get_by_role("option", name=re.compile(r"^Print")).first
        await option.wait_for(timeout=5_000)
        await option.scroll_into_view_if_needed()  # Print is in I/O, the list's last group
        src = await option.bounding_box()
        pane = await page.locator(".react-flow__pane").bounding_box()
        target = (pane["x"] + pane["width"] * 0.3, pane["y"] + pane["height"] * 0.7)
        await page.mouse.move(src["x"] + 40, src["y"] + src["height"] / 2)
        await page.mouse.down()
        for i in range(1, 11):
            await page.mouse.move(src["x"] + 40 + (target[0] - src["x"] - 40) * i / 10,
                                  src["y"] + src["height"] / 2 + (target[1] - src["y"] - src["height"] / 2) * i / 10)
            await asyncio.sleep(0.03)
        await page.mouse.up()
        await asyncio.sleep(1.0)
        boxes = await page.evaluate("""() => [...document.querySelectorAll('.react-flow__node')].map(n => {
            const r = n.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })""")
        if len(boxes) != 2:
            problems.append(f"the drop made {len(boxes) - 1} steps, not one")
        elif not any(abs(b["x"] - target[0]) < 60 and abs(b["y"] - target[1]) < 60 for b in boxes):
            problems.append(f"the dropped step is not under the drop point {tuple(round(t) for t in target)}: {boxes}")
        await page.keyboard.press("Escape")
        await page.keyboard.press("Control+k")
        await asyncio.sleep(0.6)
        if not await page.locator("[cmdk-group-heading]", has_text="Add a step").count():
            problems.append("⌘K on the canvas has no 'Add a step' group")
        await page.keyboard.press("Escape")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "a step dragged from Add step lands at the drop point, and ⌘K lists the steps"


@check
async def check_canvasfold(browser) -> str:
    """The menu folds to icons on the canvas and comes back as it was (D204, M3a).

    Opening a workflow folds the rail; leaving it restores whatever the person
    had. Expanding it on the canvas holds for that visit only and never writes
    the saved setting.
    """
    oid = f"orch_uismoke_fold_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 2))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    problems = []
    folded = lambda: page.get_by_role("button", name="Expand sidebar").count()
    stored = lambda: page.evaluate("() => localStorage.getItem('synapseRailCollapsed')")
    try:
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        if await folded():
            problems.append("the list opened with the rail folded")
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        if not await folded():
            problems.append("the open canvas left the rail expanded")
        await page.get_by_role("button", name="Expand sidebar").click()
        await asyncio.sleep(0.4)
        if await folded():
            problems.append("Expand did not open the rail on the canvas")
        if (await stored()) == "1":
            problems.append("expanding on the canvas wrote the saved setting")
        await page.get_by_role("button", name="Back", exact=True).click()
        await asyncio.sleep(1.0)
        if await folded():
            problems.append("leaving the canvas left the rail folded")
        if await page.locator("main h1", has_text="Workflows").count() == 0:
            problems.append("the list's header did not come back")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "the rail folds on the canvas, opens there for the visit, and is as it was after"


@check
async def check_workflowsettings(browser) -> str:
    """Workflow settings hold every field the panels under the old canvas had (D204).

    Description, State schema and Guardrails (Max Total Turns, Timeout, strict
    state types, Result, Score runs against) moved into the card; each still
    saves. The run's input is Run now's (runinput).
    """
    oid = f"orch_uismoke_wfs_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 2))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/orchestrations") else None)
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Workflow settings", exact=True).click()
        await asyncio.sleep(0.6)
        panel = page.get_by_role("region", name="Workflow settings")
        for role, name in [("textbox", "Workflow description"),
                           ("spinbutton", "Max Total Turns"), ("spinbutton", "Timeout (minutes)"),
                           ("checkbox", re.compile(r"^Stop on a state type mismatch")), ("combobox", "Result"),
                           ("combobox", "Score runs against"), ("button", "Add key")]:
            # The checkbox's label holds its explanation too, so its name starts with the words.
            if not await panel.get_by_role(role, name=name, exact=isinstance(name, str)).count():
                problems.append(f"no {role} {name!r}")
        await panel.get_by_role("textbox", name="Workflow description").fill("smoke described")
        await panel.get_by_role("spinbutton", name="Max Total Turns").fill("77")
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.2)
        if not sent:
            problems.append("Save sent nothing")
        elif sent[-1].get("max_total_turns") != 77 or sent[-1].get("description") != "smoke described":
            problems.append(f"Save sent turns {sent[-1].get('max_total_turns')!r}, description {sent[-1].get('description')!r}")
        await page.get_by_role("button", name="Close the workflow settings").click()
        await asyncio.sleep(0.4)
        if await panel.count():
            problems.append("Close did not put the settings away")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "Workflow settings hold the description, state and every guardrail, and they save"


@check
async def check_runinput(browser) -> str:
    """Run now's chevron holds the run's input, and Run now runs with or without one (Naveen, M3a).

    The input sat under the old canvas, on the Run Log tab; on the open canvas it
    is behind Run now's chevron, and Run now sends it as it stands. Read from the
    run's own POST, not the screen.
    """
    oid = f"orch_uismoke_input_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, "ui-smoke run input"))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[str | None] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}").get("message"))
            if r.method == "POST" and r.url.endswith(f"/api/orchestrations/{oid}/run") else None)
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run now", exact=True).click()
        await asyncio.sleep(2.5)
        await page.get_by_role("button", name="Run with input").click()
        field = page.get_by_role("textbox", name="Initial input for this run")
        await field.fill("hello from ui-smoke")
        await field.press("Enter")
        await asyncio.sleep(2.5)
        if sent[:2] != ["", "hello from ui-smoke"]:
            problems.append(f"the runs sent {sent!r}, not an empty input and then the typed one")
        if await field.count():
            problems.append("the input stayed open after Enter ran")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "Run now runs with no input, and the chevron's input is what the next run gets"


@check
async def check_canvasphone(browser) -> str:
    """On a phone the canvas fills the screen and the card is a bottom sheet (D204)."""
    oid = f"orch_uismoke_sheet_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 2))
    ctx = await browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]}, has_touch=False)
    page = await ctx.new_page()
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await click_node(page, f"{oid}_s0")
        card = await page.evaluate(CARD_BOX)
        if card is None:
            raise AssertionError("selecting a step on a phone opened nothing")
        if abs(card["bottom"] - PHONE[1]) > 2 or card["left"] > 2:
            problems.append(f"the card is not a bottom sheet: {card}")
        if card["top"] < PHONE[1] * 0.29:
            problems.append(f"the sheet covers more than 70% of the screen (top at {round(card['top'])})")
        await page.get_by_role("button", name=re.compile("press to close")).click()
        await asyncio.sleep(0.5)
        if await page.evaluate(CARD_BOX) is not None:
            problems.append("the sheet's grip did not put it away")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "on a phone the step's setup is a bottom sheet that its grip puts away"


@check
async def check_canvasroles(browser) -> str:
    """Steps are coloured by who does the work, and so is the minimap (D211).

    Cyan for a step that calls a model, grey for logic, amber for a person, on
    the step's icon and in the minimap, which is off until asked for (D204).
    """
    oid = f"orch_uismoke_roles_{int(time.time())}"
    api("/api/orchestrations", "POST", roles_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        tiles = await page.evaluate("""(oid) => Object.fromEntries(['m', 'l', 'h'].map(k => {
            const tile = document.querySelector(`.react-flow__node[data-id="${oid}_${k}"] [aria-hidden].inline-flex`);
            return [k, tile ? getComputedStyle(tile).backgroundColor : null];
        }))""", oid)
        if len({tiles["m"], tiles["l"], tiles["h"]}) != 3 or None in tiles.values():
            problems.append(f"the three roles' tiles are not three colours: {tiles}")
        if await page.locator(".react-flow__minimap").count():
            problems.append("the minimap is on before anyone asked for it")
        await page.get_by_role("button", name="Minimap").click()
        await asyncio.sleep(0.6)
        fills = await page.evaluate("""() => [...document.querySelectorAll('.react-flow__minimap-node')]
            .map(n => getComputedStyle(n).fill)""")
        if len(set(fills)) != 3:
            problems.append(f"the minimap's three steps are not three colours: {fills}")
        await page.get_by_role("button", name="Minimap").click()
    finally:
        await page.evaluate("() => localStorage.removeItem('orch-minimap')")
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "model, logic and person steps are three colours on the canvas and in the minimap, which starts off"


@check
async def check_runstrip(browser) -> str:
    """The run strip opens the Run Log and Recent Runs above it, and a run opens the log (M3a)."""
    oid = f"orch_uismoke_strip_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, "ui-smoke strip"))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        drawer = page.locator('[data-canvas-chrome="drawer"]')
        if await drawer.count():
            problems.append("the run drawer was open before any run")
        await page.get_by_role("button", name=re.compile(r"^Recent Runs")).click()
        await asyncio.sleep(0.4)
        if not await drawer.get_by_role("heading", name="Recent runs").count():
            problems.append("Recent Runs did not open the recent runs")
        await page.get_by_role("button", name="Close recent runs").click()
        await page.get_by_role("button", name="Run now", exact=True).click()
        try:
            await drawer.get_by_text(re.compile(r"^Started run")).first.wait_for(timeout=20_000)
        except Exception:
            problems.append("a run did not open its log")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "the strip opens Recent Runs and the Run Log, and a run opens its log"


@check
async def check_canvasleave(browser) -> str:
    """Leaving a workflow with unsaved edits asks first (M3a, Naveen's OK).

    Back, the switcher, New and the rail used to drop an edited draft without a
    word; only a reload got the browser's prompt. A blank draft nobody touched
    still leaves quietly. Since TABS1 v2 Back, the switcher and New keep the
    edit in its tab — nothing is dropped — and leaving the page still asks.
    """
    oid = f"orch_uismoke_leave_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 2))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    problems = []
    ask = page.get_by_role("dialog", name="Discard unsaved changes?")
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Back", exact=True).click()
        await asyncio.sleep(0.6)
        if await ask.count():
            problems.append("Back with nothing edited asked anyway")
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.locator("input[aria-label='Workflow name']").fill("ui-smoke leave, edited")
        await page.get_by_role("button", name="Back", exact=True).click()
        await asyncio.sleep(0.6)
        # Since TABS1 v2 Back shows the list and the edit waits in its tab: nothing is dropped, so nothing asks.
        if await ask.count():
            problems.append("Back asked, though the edit stays in its tab")
            await ask.get_by_role("button", name="Cancel").click()
        else:
            await page.get_by_role("tablist", name="Open workflows").get_by_role("tab").first.click()
            await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
            await asyncio.sleep(0.6)
            if await page.locator("input[aria-label='Workflow name']").input_value() != "ui-smoke leave, edited":
                problems.append("Back lost the edit: its tab came back without it")
        await page.click('nav[aria-label="Primary"] a[href="/agents"]')
        await asyncio.sleep(0.6)
        if not await ask.count():
            problems.append("a rail link with an unsaved edit did not ask")
        else:
            await ask.get_by_role("button", name="Discard changes").click()
            try:
                await page.wait_for_url("**/agents", timeout=10_000)
            except Exception:
                problems.append("Discard changes did not go on to the link")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "an edited workflow asks before Back or a link drops it; an untouched one leaves quietly"


# ── the live run on the canvas (M3a-2) ───────────────────────────────────────

def signal_orchestration(oid: str) -> dict:
    """A model step, a Parallel with a model branch and a two-step logic branch, then a Merge (M3a-2)."""
    s = lambda k: f"{oid}_{k}"  # noqa: E731
    steps = [
        {"id": s("a"), "name": "Draft", "type": "llm", "prompt_template": "Say hi", "output_key": "draft",
         "next_step_id": s("p"), "position_x": 0, "position_y": 150},
        {"id": s("p"), "name": "Split", "type": "parallel", "parallel_branches": [[s("b")], [s("c"), s("d")]],
         "next_step_id": s("m"), "position_x": 280, "position_y": 150},
        {"id": s("b"), "name": "Left", "type": "llm", "prompt_template": "Left", "output_key": "left",
         "position_x": 560, "position_y": 0},
        {"id": s("c"), "name": "Right", "type": "print", "print_content": "right", "output_key": "right",
         "position_x": 560, "position_y": 300},
        {"id": s("d"), "name": "After right", "type": "print", "print_content": "after", "output_key": "after",
         "position_x": 840, "position_y": 300},
        {"id": s("m"), "name": "Join", "type": "merge", "merge_strategy": "dict", "input_keys": ["left", "right"],
         "output_key": "joined", "position_x": 1120, "position_y": 150},
    ]
    return {"id": oid, "name": "ui-smoke signals", "description": "Temporary fixture created by scripts/ui-smoke.py.",
            "steps": steps, "entry_step_id": steps[0]["id"], "state_schema": {}, "trigger": "manual"}


#: A step's run state as its frame paints it: the first status class among the frame's borders (M3a-2's
#: running colours by role included).
FRAME_CLASS = r"""(sid) => {
  const c = document.querySelector(`.react-flow__node[data-id="${sid}"] > div`);
  return c ? (c.className.match(/border-(machine|text-muted|person|success|danger|border-strong)/) || [null])[0] : null;
}"""

#: One connection's live state: lit (the machine's colour), and its signal dot (mounted, and visible now).
EDGE_STATE = r"""(eid) => {
  const g = document.querySelector(`.react-flow__edge[data-id="${eid}"]`);
  if (!g) return null;
  const path = g.querySelector('.react-flow__edge-path');
  const dot = g.querySelector('circle[data-signal-dot]');
  return {lit: (path && path.getAttribute('style') || '').includes('--machine'),
          mounted: !!dot, dot: !!dot && dot.getAttribute('visibility') === 'visible'};
}"""


async def _edge_seen(page, eid: str, seconds: float = 1.6) -> dict:
    """Watch one connection for a while: was it lit, and did a dot travel it."""
    seen = {"lit": False, "dot": False}
    end = time.time() + seconds
    while time.time() < end:
        st = await page.evaluate(EDGE_STATE, eid)
        if st:
            seen["lit"] = seen["lit"] or st["lit"]
            seen["dot"] = seen["dot"] or st["dot"]
        await asyncio.sleep(0.04)
    return seen


@check
async def check_runsignal(browser) -> str:
    """The run plays on the canvas: the working step glows by role, and a signal travels the way the run came (M3a-2).

    D211's signature moment, driven by the stream alone: a step's start sends a
    dot down the connection it came along — from the step that finished, from a
    Parallel to its branches and from it again to the join — and the connection
    stays lit while the run is live. A model step working is cyan, a logic step
    a neutral ring. A failed branch step stays failed when its step_complete
    follows (Naveen's call at M3a-2). With reduced motion nothing travels and
    the same connections light in place; when the run ends they go grey. The
    run's stream is stubbed, one event at a time: no engine, no model.
    """
    oid = f"orch_uismoke_signal_{int(time.time())}"
    api("/api/orchestrations", "POST", signal_orchestration(oid))
    s = lambda k: f"{oid}_{k}"  # noqa: E731
    a_p, p_b, p_c = f"{s('a')}->{s('p')}", f"{s('p')}->par->{s('b')}", f"{s('p')}->par->{s('c')}"
    c_d, p_m = f"par_{s('p')}_{s('c')}->{s('d')}", f"{s('p')}->{s('m')}"
    problems: list[str] = []

    async def push(page, *events):
        for e in events:
            await page.evaluate("(e) => window.__run.raw(e)", e)

    def start(k, kind):
        return {"type": "step_start", "orch_step_id": s(k), "step_name": k, "step_type": kind}

    def done(k):
        return {"type": "step_complete", "orch_step_id": s(k), "step_name": k, "duration_seconds": 0.4}

    try:
        for reduced in (False, True):
            ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]},
                                            reduced_motion="reduce" if reduced else "no-preference")
            await ctx.add_init_script(RUNLOG_STUB)
            page = await ctx.new_page()
            mode = "reduced motion" if reduced else "motion"
            try:
                await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
                await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
                await asyncio.sleep(SETTLE)
                await page.get_by_role("button", name="Run now", exact=True).click()
                for _ in range(50):
                    if await page.evaluate("() => !!window.__run.raw"):
                        break
                    await asyncio.sleep(0.1)
                await push(page, {"type": "orchestration_start", "run_id": f"run_smoke_signal_{int(reduced)}"},
                           start("a", "llm"))
                await asyncio.sleep(0.5)
                frame = await page.evaluate(FRAME_CLASS, s("a"))
                if frame != "border-machine":
                    problems.append(f"{mode}: a model step working shows {frame}, not cyan")
                if not await page.locator(f'.react-flow__node[data-id="{s("a")}"] [data-run-pill=running]').count():
                    problems.append(f"{mode}: the working step does not say Working")

                await push(page, done("a"), start("p", "parallel"))
                ap = await _edge_seen(page, a_p)
                await push(page, start("b", "llm"), start("c", "print"))
                pb, pc = await asyncio.gather(_edge_seen(page, p_b), _edge_seen(page, p_c))
                split = await page.evaluate(FRAME_CLASS, s("p"))
                if split != "border-text-muted":
                    problems.append(f"{mode}: a Parallel working shows {split}, not a neutral frame")
                await push(page, {"type": "step_error", "orch_step_id": s("b"), "error": "boom", "reason": "boom"},
                           done("b"))
                await asyncio.sleep(0.4)
                left = await page.evaluate(FRAME_CLASS, s("b"))
                if left != "border-danger":
                    problems.append(f"{mode}: a failed branch step followed by its step_complete shows {left}, not failed")
                await push(page, done("c"), start("d", "print"))
                cd = await _edge_seen(page, c_d)
                await push(page, done("d"), done("p"), start("m", "merge"))
                pm = await _edge_seen(page, p_m)
                for name, seen in (("Draft → Split", ap), ("Split → Left", pb), ("Split → Right", pc),
                                   ("Right → After right", cd), ("Split → Join", pm)):
                    if not seen["lit"]:
                        problems.append(f"{mode}: {name} never lit")
                    if reduced and seen["dot"]:
                        problems.append(f"{mode}: a signal travelled {name}")
                    if not reduced and not seen["dot"]:
                        problems.append(f"{mode}: no signal travelled {name}")
                stray = await page.evaluate(EDGE_STATE, f"{s('p')}->{s('m')}")
                if not stray or not stray["lit"]:
                    problems.append(f"{mode}: the join's connection is not lit while the run is live")
                await push(page, done("m"), {"type": "orchestration_complete", "run_id": "x", "status": "completed"})
                await asyncio.sleep(0.6)
                after = [await page.evaluate(EDGE_STATE, e) for e in (a_p, p_b, p_m)]
                if any(st and st["lit"] for st in after):
                    problems.append(f"{mode}: connections stay lit after the run ended")
            finally:
                await ctx.close()
    finally:
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return ("a working model step glows cyan, a Parallel neutral; signals travel each way the run came and the "
            "connections stay lit while it is live; reduced motion lights in place; a failed branch step stays failed")


def ask_orchestration(oid: str, fields: list | None = None) -> dict:
    """A Print, then a person asked (with `fields`, or in words), then a Print: model-free (M3a-2)."""
    ask = {"id": f"{oid}_ask", "name": "Ask", "type": "human", "human_prompt": "Anything to add?",
           "next_step_id": f"{oid}_z", "output_key": "extra", "position_x": 300, "position_y": 0}
    if fields:
        ask["human_fields"] = fields
    return {
        "id": oid, "name": "ui-smoke ask", "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "trigger": "manual", "state_schema": {}, "entry_step_id": f"{oid}_a",
        "steps": [
            {"id": f"{oid}_a", "name": "Prepare", "type": "print", "print_content": "ready", "output_key": "ready",
             "next_step_id": f"{oid}_ask", "position_x": 0, "position_y": 0},
            ask,
            {"id": f"{oid}_z", "name": "Finish", "type": "print", "print_content": "done", "output_key": "fin",
             "next_step_id": None, "position_x": 600, "position_y": 0},
        ],
    }


CARD_QUESTION = "[data-canvas-chrome='card'] section[aria-label='Waiting for a person']"


@check
async def check_runcard(browser) -> str:
    """The question a run waits on is answered in its step's setup card, which opens by itself (M3a-2).

    D204's "the approval is answered in the card": no click opens it. The
    editor's rule holds there (Naveen's item 7): an empty answer in words is
    not sent, by the button or by Enter (the old dialog's Enter sent it anyway).
    Enter sends a typed answer; a lone Yes / No is its label's button (D218).
    Each answer is read off the wire, and each run completes. Model-free.
    """
    problems: list[str] = []
    for fields, label in ((None, None), ([{"name": "approved", "type": "boolean", "label": "Approve?"}], "Approve")):
        oid = f"orch_uismoke_card_{int(time.time())}_{1 if fields else 0}"
        api("/api/orchestrations", "POST", ask_orchestration(oid, fields))
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        page = await ctx.new_page()
        sent: list = []
        page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}").get("response"))
                if r.method == "POST" and "/human-input" in r.url else None)
        try:
            await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
            await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
            await asyncio.sleep(SETTLE)
            await page.get_by_role("button", name="Run now", exact=True).click()
            card = page.locator(CARD_QUESTION)
            try:
                await card.wait_for(timeout=30_000)
            except Exception:
                problems.append(f"{'fields' if fields else 'words'}: the card did not open on the question by itself")
                continue
            if fields:
                await card.get_by_role("button", name=label, exact=True).click()
                want = {"approved": True}
            else:
                send = card.get_by_role("button", name="Send answer", exact=True)
                if not await send.is_disabled():
                    problems.append("words: Send answer is enabled with nothing typed")
                box = card.get_by_role("textbox", name="Your answer")
                await box.focus()
                await page.keyboard.press("Enter")
                await asyncio.sleep(1.0)
                if sent:
                    problems.append(f"words: Enter on an empty answer sent {sent}")
                await box.fill("ok from ui-smoke")
                await page.keyboard.press("Enter")
                want = "ok from ui-smoke"
            await page.get_by_text("Done — status: completed").first.wait_for(timeout=30_000)
            if sent != [want]:
                problems.append(f"{'fields' if fields else 'words'}: the answers sent were {sent}, not [{want!r}]")
        except Exception as e:
            problems.append(f"{'fields' if fields else 'words'}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
        finally:
            await ctx.close()
            drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "the card opens on the question by itself; empty words are not sent; Enter and a lone Yes / No answer it"


def branch_orchestration(oid: str) -> dict:
    """A Print, an If/Else that always takes its true road, a Print on each road: one is never reached (M3a-2)."""
    s = lambda k: f"{oid}_{k}"  # noqa: E731
    return {
        "id": oid, "name": "ui-smoke last run", "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "trigger": "manual", "state_schema": {}, "entry_step_id": s("a"),
        "steps": [
            {"id": s("a"), "name": "First", "type": "print", "print_content": "one", "output_key": "one",
             "next_step_id": s("if"), "position_x": 0, "position_y": 100},
            {"id": s("if"), "name": "Which way", "type": "if_else", "if_condition": "1 == 1",
             "if_true_step_id": s("yes"), "if_false_step_id": s("no"), "position_x": 280, "position_y": 100},
            {"id": s("yes"), "name": "Taken", "type": "print", "print_content": "yes", "output_key": "yes",
             "next_step_id": None, "position_x": 560, "position_y": 0},
            {"id": s("no"), "name": "Not taken", "type": "print", "print_content": "no", "output_key": "no",
             "next_step_id": None, "position_x": 560, "position_y": 220},
        ],
    }


@check
async def check_lastrun(browser) -> str:
    """Each step shows how it went and its time from the last run (M3a-2).

    As a run finishes on the canvas, each step it ran says done and its time;
    one it never reached is dimmed. Reopened from its link, the same, from its
    journal and the record its reopening already reads. On a fresh visit with no run on the canvas, the
    bar's Last run reads the workflow's newest run once (one GET of its record:
    Naveen's call) and shows the same. Model-free.
    """
    oid = f"orch_uismoke_lastrun_{int(time.time())}"
    api("/api/orchestrations", "POST", branch_orchestration(oid))
    s = lambda k: f"{oid}_{k}"  # noqa: E731
    pills = r"""(ids) => ids.map((id) => {
      const n = document.querySelector(`.react-flow__node[data-id="${id}"]`);
      const pill = n && n.querySelector('[data-run-pill]');
      const frame = n && n.querySelector(':scope > div');
      return {pill: pill ? pill.getAttribute('data-run-pill') : null,
              dim: !!frame && /(^|\s)opacity-55(\s|$)/.test(frame.className)};
    })"""
    ids = [s("a"), s("if"), s("yes"), s("no")]
    problems: list[str] = []
    try:
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
            await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
            await asyncio.sleep(SETTLE)
            await page.get_by_role("button", name="Run now", exact=True).click()
            await page.get_by_text("Done — status: completed").first.wait_for(timeout=30_000)
            await asyncio.sleep(1.0)
            run_id = await page.evaluate("() => (document.body.innerText.match(/Started run (\\S+)/) || [])[1] || null")
            live = await page.evaluate(pills, ids)
            if [p["pill"] for p in live[:3]] != ["done"] * 3:
                problems.append(f"after the run, the steps it ran show {[p['pill'] for p in live[:3]]}")
            if not live[3]["dim"] or live[3]["pill"]:
                problems.append(f"the step the run never reached shows {live[3]}")

            reads: list[str] = []
            page.on("request", lambda r: reads.append(r.url) if r.method == "GET" and run_id and r.url.endswith(f"/api/orchestrations/runs/{run_id}") else None)
            await page.goto(f"{APP}/workflows?run={run_id}", wait_until="domcontentloaded")
            await page.get_by_text("Done — status: completed").first.wait_for(timeout=30_000)
            await asyncio.sleep(1.0)
            again = await page.evaluate(pills, ids)
            if [p["pill"] for p in again[:3]] != ["done"] * 3 or not again[3]["dim"]:
                problems.append(f"reopened from its link, the steps show {again}")

            await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
            await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
            await asyncio.sleep(SETTLE + 1.0)
            if any(p["pill"] for p in await page.evaluate(pills, ids)):
                problems.append("a fresh visit shows times before Last run is pressed")
            before = len(reads)
            await page.get_by_role("button", name="Last run", exact=True).click()
            await asyncio.sleep(2.0)
            fresh = await page.evaluate(pills, ids)
            if [p["pill"] for p in fresh[:3]] != ["done"] * 3 or not fresh[3]["dim"]:
                problems.append(f"Last run on a fresh visit shows {fresh}")
            if len(reads) - before != 1:
                problems.append(f"Last run read the run's record {len(reads) - before} times, not once")
        finally:
            await ctx.close()
    finally:
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "each step shows its time as the run ends, reopened, and from Last run with one read; an unreached step is dimmed"


@check
async def check_ctrls(browser) -> str:
    """Ctrl+S saves from inside a field, and the browser's "Save page" never opens.

    The editor's shortcut handler returned early for any focused input, so
    Ctrl+S while typing a name did nothing at all — no save, and nothing stopped
    the browser saving the page instead (F48). Read from the keydown's own
    `defaultPrevented` and from what the save sent.
    """
    oid = f"orch_uismoke_ctrls_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid, 1))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/orchestrations") else None)
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        await page.evaluate(r"""() => { window.__saveKey = null;
            window.addEventListener('keydown', (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's')
                    setTimeout(() => { window.__saveKey = e.defaultPrevented; });
            }, true); }""")
        name = page.get_by_role("textbox", name="Workflow name", exact=True)
        await name.click()
        await page.keyboard.press("End")
        await page.keyboard.type(" renamed")
        await page.keyboard.press("Control+s")
        await asyncio.sleep(1.5)
        prevented = await page.evaluate("() => window.__saveKey")
    finally:
        await ctx.close()
        drop_fixture(oid)
    problems = []
    if prevented is not True:
        problems.append(f"the keydown was not prevented ({prevented!r}) — the browser saves the page")
    if not sent:
        problems.append("nothing was saved")
    elif not str(sent[-1].get("name", "")).endswith(" renamed"):
        problems.append(f"the save carried the name {sent[-1].get('name')!r}, not the one being typed")
    if problems:
        raise AssertionError("; ".join(problems))
    return "Ctrl+S in the name field saves the name being typed"


@check
async def check_runnernote(browser) -> str:
    """The Transform panel says what the sandbox really allows.

    It said "no network". The engine asks the runner for the internet, and a
    Transform in the shipped Docker sandbox opened a socket to 1.1.1.1:53 when
    measured (F70). A person deciding whether code may call out reads this
    sentence, so it must be the true one.
    """
    oid = f"orch_uismoke_note_{int(time.time())}"
    orch = laid_out_orchestration(oid, 1)
    orch["steps"][0].update({"type": "transform", "transform_code": "result = 1", "name": "Code"})
    api("/api/orchestrations", "POST", orch)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await open_step(page, oid, "Code")
        note = await page.locator('aside[aria-label="Step configuration"]').get_by_text("Python execution").first.locator("xpath=..").inner_text()
    finally:
        await ctx.close()
        drop_fixture(oid)
    if re.search(r"no network", note, re.I) or not re.search(r"internet", note, re.I):
        raise AssertionError(f"the Transform note says {note[:140]!r} — the sandbox has internet access")
    return "the Transform note says the sandbox reaches the internet"


@check
async def check_historyflag(browser) -> str:
    """"Include full revision history" shows what runs, and can be turned off.

    The engine reads an unset flag as on (`context.py`: None → True). The box
    showed an unset step unticked, and unticking wrote `undefined` — so both
    states ran with full history and the box could never say otherwise (F76).
    Read from the box and from what Save sends.
    """
    oid = f"orch_uismoke_hist_{int(time.time())}"
    orch = laid_out_orchestration(oid, 1)
    orch["steps"][0].update({"type": "llm", "name": "Write", "prompt_template": "Say hi."})
    api("/api/orchestrations", "POST", orch)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/orchestrations") else None)
    try:
        await open_step(page, oid, "Write")
        box = page.get_by_role("checkbox", name="Include full revision history")
        shown = await box.is_checked()
        if shown:
            await box.click()
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.5)
    finally:
        await ctx.close()
        drop_fixture(oid)
    problems = []
    if not shown:
        problems.append("an unset step shows the box unticked, while the engine runs it with full history")
    saved = (sent[-1]["steps"][0] if sent else {}).get("include_full_history", "absent")
    if saved is not False:
        problems.append(f"unticking saved include_full_history={saved!r}, not false")
    if problems:
        raise AssertionError("; ".join(problems))
    return "the box shows the engine's default, and unticking saves false"


@check
async def check_cachetoggle(browser) -> str:
    """The step panel offers no tool cache: there is none.

    The engine's tool-result cache was removed in batch 1B — it cached one
    tool, and wrongly (F67). The panel kept "Cache deterministic tools" and its
    TTL, a switch wired to nothing.
    """
    oid = f"orch_uismoke_cache_{int(time.time())}"
    orch = laid_out_orchestration(oid, 1)
    orch["steps"][0].update({"type": "llm", "name": "Write", "prompt_template": "Say hi."})
    api("/api/orchestrations", "POST", orch)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await open_step(page, oid, "Write")
        panel = page.locator('aside[aria-label="Step configuration"]')
        await panel.get_by_role("button", name=re.compile(r"^Caching")).click()
        await asyncio.sleep(0.5)
        responses = await panel.get_by_role("checkbox", name="Cache LLM responses").count()
        tools = await panel.get_by_text(re.compile("cache deterministic tools", re.I)).count()
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not responses:
        raise AssertionError("the Caching section did not open (no Cache LLM responses)")
    if tools:
        raise AssertionError("the panel still offers 'Cache deterministic tools', which nothing reads")
    return "Caching offers the response cache, and no tool cache"


@check
async def check_semanticnote(browser) -> str:
    """Semantic match names the setting it embeds with, and says whether one is set (F327).

    It said "Ollama with nomic-embed-text (default)", which the cache always
    asked whatever Settings → Models chose; it now embeds with the Embedding
    Model, and without one runs exact match only.
    """
    oid = f"orch_uismoke_semnote_{int(time.time())}"
    orch = laid_out_orchestration(oid, 1)
    orch["steps"][0].update({"type": "llm", "name": "Write", "prompt_template": "Say hi.",
                             "cache_responses_enabled": True})
    api("/api/orchestrations", "POST", orch)
    chosen = str(api("/api/settings").get("embedding_model") or "")
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await open_step(page, oid, "Write")
        panel = page.locator('aside[aria-label="Step configuration"]')
        await panel.get_by_role("button", name=re.compile(r"^Caching")).click()
        label = panel.locator("label").filter(has=page.get_by_role("checkbox", name="Semantic match"))
        await label.wait_for(timeout=10_000)
        note = " ".join((await label.inner_text()).split())
        if re.search(r"ollama|nomic", note, re.I):
            raise AssertionError(f"Semantic match still names Ollama: {note!r}")
        if "Embedding Model" not in note or "Settings → Models" not in note:
            raise AssertionError(f"Semantic match does not name the setting: {note!r}")
        if chosen and chosen not in note:
            raise AssertionError(f"Semantic match does not name the chosen model {chosen!r}: {note!r}")
        if not chosen and "only exact match" not in note:
            raise AssertionError(f"with no Embedding Model, Semantic match does not say only exact match runs: {note!r}")
        await page.goto(f"{APP}/settings/models", wait_until="domcontentloaded")
        models = page.get_by_text(re.compile(r"semantic cache", re.I)).first
        try:
            await models.wait_for(timeout=15_000)
        except Exception:
            raise AssertionError("Settings → Models does not say the Embedding Model feeds the semantic cache")
        models_says = " ".join((await models.inner_text()).split())
    finally:
        await ctx.close()
        drop_fixture(oid)
    return f"Semantic match: {note!r}; Settings → Models: {models_says[:80]!r}…"


# ── a human step's fields ────────────────────────────────────────────────────

#: What the AI builder itself writes (`textarea`), a flag, and a type nobody
#: knows — the three shapes the forms disagreed on (F75).
HUMAN_FIELDS = [
    {"name": "notes", "type": "textarea", "label": "Notes"},
    {"name": "approved", "type": "boolean", "label": "Approved?"},
    {"name": "when", "type": "weird", "label": "When"},
]
HUMAN_ANSWER = {"notes": "looks fine", "approved": True, "when": "friday"}

HUMAN_CHAT_STUB = r"""
const _fetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (url.includes('/api/chat/stream')) {
    const body = new ReadableStream({
      start(c) {
        const send = (e) => c.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(e) + '\n\n'));
        send({type: 'human_input_required', prompt: 'Review the draft.', run_id: 'run_uismoke_fields',
              orch_step_id: 'approve', fields: FIELDS});
        c.close();
      },
    });
    return Promise.resolve(new Response(body, {status: 200, headers: {'Content-Type': 'text/event-stream'}}));
  }
  if (url.includes('/api/orchestrations/runs/run_uismoke_fields/human-input')) {
    window.__answer = JSON.parse(init.body);
    return Promise.resolve(new Response('', {status: 200, headers: {'Content-Type': 'text/event-stream'}}));
  }
  return _fetch(input, init);
};
"""


async def fill_human_fields(scope) -> None:
    """Answer HUMAN_FIELDS the way a person would, by their labels."""
    await scope.get_by_role("textbox", name="Notes", exact=True).fill(HUMAN_ANSWER["notes"])
    await scope.get_by_role("checkbox", name="Approved?", exact=True).check()
    await scope.get_by_role("textbox", name="When", exact=True).fill(HUMAN_ANSWER["when"])


@check
async def check_humanfields(browser) -> str:
    """A human step's fields mean the same thing in the editor and in every answer form.

    Four forms had four ideas of a field (F75). The canvas offered three types
    and showed the builder's own `textarea` as a blank box; the orchestration
    screen ignored the fields and took one line of text; chat keyed the answer
    by each field's *label* and rendered a yes/no as a text box — while the
    engine, cloud's run page and a channel reply key it by *name*, which is
    what a later step reads (`{state.approval.approved}`).
    """
    oid = f"orch_uismoke_fields_{int(time.time())}"
    orch = human_gate_orchestration(oid)
    orch["steps"][-1]["human_fields"] = HUMAN_FIELDS
    api("/api/orchestrations", "POST", orch)
    problems: list[str] = []
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and "/human-input" in r.url else None)
    try:
        # The editor: every stored type reads as something.
        await open_step(page, oid, "Approve")
        shown = [
            (await page.get_by_role("combobox", name=f"Field {i + 1} type").inner_text()).strip()
            for i in range(len(HUMAN_FIELDS))
        ]
        for field, text in zip(HUMAN_FIELDS, shown):
            if not text:
                problems.append(f"the editor shows {field['type']!r} as a blank type")
        # The orchestration screen: run it, answer it, read what went out. Since
        # M3a-2 the question is answered in the waiting step's setup card (D204),
        # where the lone Yes / No is its label's button (D218), not a checkbox.
        await page.keyboard.press("Escape")
        await page.get_by_role("button", name="Run now", exact=True).click()
        for _ in range(25):
            await asyncio.sleep(1.0)
            if await page.get_by_text("Waiting for your input").count():
                break
        await page.get_by_text("Waiting for your input").first.click()
        await asyncio.sleep(0.6)
        card = page.locator(CARD_QUESTION)
        try:
            await card.get_by_role("textbox", name="Notes", exact=True).fill(HUMAN_ANSWER["notes"])
            await card.get_by_role("textbox", name="When", exact=True).fill(HUMAN_ANSWER["when"])
            await card.get_by_role("button", name="Approved", exact=True).click()
            await asyncio.sleep(1.5)
            got = sent[-1].get("response") if sent else None
            if got != HUMAN_ANSWER:
                problems.append(f"the orchestration screen answered {got!r}, not {HUMAN_ANSWER!r}")
        except Exception as e:
            problems.append(f"the orchestration screen's answer form has no field per declared field ({type(e).__name__})")
    finally:
        await ctx.close()
        drop_fixture(oid)

    # Chat, fed the same question by a stubbed stream.
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(HUMAN_CHAT_STUB.replace("FIELDS", json.dumps(HUMAN_FIELDS)))
    page = await ctx.new_page()
    try:
        await page.goto(APP, wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        box = page.locator("main textarea").last
        await box.fill("start the review")
        await box.press("Enter")
        await asyncio.sleep(1.5)
        try:
            form = page.locator("form", has=page.get_by_role("textbox", name="Notes", exact=True)).last
            await fill_human_fields(form)
            await form.get_by_role("button", name="Submit").click()
            await asyncio.sleep(1.0)
            got = (await page.evaluate("() => window.__answer || null") or {}).get("response")
            if got != HUMAN_ANSWER:
                problems.append(f"chat answered {got!r}, not {HUMAN_ANSWER!r}")
        except Exception as e:
            problems.append(f"chat's answer form has no field per declared field ({type(e).__name__})")
    finally:
        await ctx.close()
    if problems:
        raise AssertionError("; ".join(problems))
    return "the editor, the orchestration screen and chat agree on every field, keyed by name"


@check
async def check_loopmode(browser) -> str:
    """A loop can be switched to "While a condition holds" and stays there.

    The panel works out a loop's mode from its fields — a collection, else a
    condition, else a count — and picking "while" wrote an empty condition,
    which reads as no condition: the picker snapped straight back to "A fixed
    number of times" and the condition field never appeared (Naveen, cloud,
    2026-09-19). Read from the picker and from what Save sends.
    """
    oid = f"orch_uismoke_loop_{int(time.time())}"
    orch = laid_out_orchestration(oid, 2)
    orch["steps"][0].update({"type": "loop", "name": "Repeat", "loop_count": 3,
                             "loop_step_ids": [f"{oid}_s1"], "next_step_id": None})
    api("/api/orchestrations", "POST", orch)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/orchestrations") else None)
    try:
        await open_step(page, oid, "Repeat")
        mode = page.get_by_role("combobox", name="Loop mode")
        await mode.click()
        await page.get_by_role("option", name=re.compile("While a condition holds")).click()
        await asyncio.sleep(0.6)
        shown = (await mode.inner_text()).strip()
        field = page.get_by_role("textbox", name="While condition")
        if not await field.count():
            raise AssertionError(f"picking 'While a condition holds' left the picker on {shown!r}, with no condition field")
        await field.fill("(state.n or 0) < 2")
        await field.press("Tab")
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.5)
    finally:
        await ctx.close()
        drop_fixture(oid)
    loop = next((s for s in (sent[-1]["steps"] if sent else []) if s["id"] == f"{oid}_s0"), {})
    if loop.get("loop_while_condition") != "(state.n or 0) < 2" or loop.get("loop_over"):
        raise AssertionError(f"Save sent loop_while_condition={loop.get('loop_while_condition')!r}, loop_over={loop.get('loop_over')!r}")
    return "a loop switched to 'while' keeps the mode and saves its condition"


# ── a chat turn outlives the page (F49) ──────────────────────────────────────

#: Chat's network, driven from the test. `/api/chat/stream` and
#: `/api/chat/{id}/events` answer with streams this script feeds frame by frame
#: (and can break, the way a dead socket does); a conversation's history can be
#: answered from `window.__fakeHistory`. Every call is recorded. Model-free: what
#: is under test is what the page does with a turn it does not own.
CHAT_STUB = r"""
window.__calls = [];
window.__streams = {};
const _fetch = window.fetch;
const _stream = (name) => new ReadableStream({
  start(c) {
    window.__streams[name] = {
      push: (frame) => c.enqueue(new TextEncoder().encode(frame)),
      close: () => { try { c.close(); } catch (e) {} },
      fail: () => { try { c.error(new TypeError('network error')); } catch (e) {} },
    };
  },
});
const _sse = (name) => Promise.resolve(new Response(_stream(name), {
  status: 200, headers: {'Content-Type': 'text/event-stream'},
}));
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
  if (url.includes('/api/chat') || url.includes('/api/sessions')) window.__calls.push({url: url, method: method});
  if (url.includes('/api/chat/stream')) return _sse('post');
  if (/\/api\/chat\/[^/?]+\/events/.test(url)) return _sse('events');
  if (/\/api\/chat\/[^/?]+\/cancel/.test(url) || (/\/api\/chat$/.test(url) && method === 'POST')) {
    return Promise.resolve(new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}));
  }
  if (/\/api\/sessions\/[^/?]+\/history/.test(url) && window.__fakeHistory) {
    return Promise.resolve(new Response(JSON.stringify(window.__fakeHistory), {
      status: 200, headers: {'Content-Type': 'application/json'},
    }));
  }
  return _fetch(input, init);
};
"""


def _frame(event: dict, cursor: str | None = None) -> str:
    return (f"id: {cursor}\n" if cursor else "") + f"data: {json.dumps(event)}\n\n"


async def _chat_page(browser, session_id: str, history: dict | None = None,
                     extra_init: str | None = None, width=WIDE):
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    await ctx.add_init_script(CHAT_STUB)
    await ctx.add_init_script(
        f"try {{ localStorage.setItem('synapseSessionId', {json.dumps(session_id)}); }} catch (e) {{}}"
        + (f"\nwindow.__fakeHistory = {json.dumps(history)};" if history else ""))
    if extra_init:
        await ctx.add_init_script(extra_init)
    page = await ctx.new_page()
    await page.goto(APP, wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE)
    return ctx, page


async def _stream_open(page, name: str) -> None:
    for _ in range(60):
        if await page.evaluate(f"() => !!(window.__streams || {{}})[{json.dumps(name)}]"):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"the page never opened its {name} stream")


async def _on_screen(page, text: str, tries: int = 30) -> bool:
    for _ in range(tries):
        if text in await page.evaluate("() => document.body.innerText"):
            return True
        await asyncio.sleep(0.2)
    return False


@check
async def check_chatreopen(browser) -> str:
    """The chat page opens on its conversation — and on a turn still being written.

    It used to open empty while keeping the conversation's id, so the next
    message continued a conversation nobody could see, and a turn left running
    when the page went away had nowhere to be seen (F49).
    """
    history = {
        "turns": [{"user": "asked earlier", "assistant": "answered earlier", "tools": [], "timestamp": ""}],
        "pending_approval": None,
        "running": {"turn_id": "t1", "message": "still being written?", "started_at": 0, "cursor": "t1-0"},
    }
    ctx, page = await _chat_page(browser, "uismoke-reopen", history)
    try:
        await _stream_open(page, "events")
        for shown in ("asked earlier", "answered earlier", "still being written?"):
            if not await _on_screen(page, shown):
                raise AssertionError(f"the reopened conversation does not show {shown!r}")
        await page.evaluate(
            f"() => {{ window.__streams.events.push({json.dumps(_frame({'type': 'response', 'content': 'the late answer'}, 't1-3'))});"
            f" window.__streams.events.push({json.dumps(_frame({'type': 'done'}, 't1-4'))}); }}")
        if not await _on_screen(page, "the late answer"):
            raise AssertionError("the running turn's answer never reached the screen")
        calls = await page.evaluate("() => window.__calls")
    finally:
        await ctx.close()
    follows = [c["url"] for c in calls if "/events" in c["url"]]
    if not any("after=t1-0" in u for u in follows):
        raise AssertionError(f"the running turn was not read from its start: {follows}")
    if any("/api/chat/stream" in c["url"] for c in calls):
        raise AssertionError("reopening a conversation sent a message")
    return "the conversation reopens, and a running turn is followed to its answer"


#: The page-open history read held until the test releases it (F552).
HOLD_HISTORY = r"""
const _stubbedFetch = window.fetch;
window.__historyGate = new Promise((release) => { window.__releaseHistory = release; });
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (/\/api\/sessions\/[^/?]+\/history/.test(url)) return window.__historyGate.then(() => _stubbedFetch(input, init));
  return _stubbedFetch(input, init);
};
"""


@check
async def check_openrace(browser) -> str:
    """A message sent before the page has read its conversation shows its answer once (F552).

    The page-open read, answering mid-turn, found that turn running, drew the
    conversation again and followed the turn a second time beside the stream it
    was already reading: the answer twice, every tool and thought counted twice.
    """
    ctx, page = await _chat_page(browser, "uismoke-open-race", None, extra_init=HOLD_HISTORY)
    try:
        box = page.locator("main textarea").last
        await box.fill("sent before the page read its conversation")
        await box.press("Enter")
        await _stream_open(page, "post")
        frames = "".join(_frame(e, f"r1-{i + 1}") for i, e in enumerate([
            {"type": "tool_execution", "tool_name": "lookup_order", "args": {"order": "HP-2041"}},
            {"type": "tool_result", "tool_name": "lookup_order", "preview": "shipped day 12"},
            {"type": "llm_thought", "thought": "HP-2041 shipped on day 12."},
        ]))
        await page.evaluate(f"() => window.__streams.post.push({json.dumps(frames)})")
        # The read answers now, as the server would mid-turn: that turn is running.
        await page.evaluate("""() => { window.__fakeHistory = {turns: [], pending_approval: null,
            running: {turn_id: 'r1', message: 'sent before the page read its conversation', started_at: 0, cursor: 'r1-0'}};
            window.__releaseHistory(); }""")
        await asyncio.sleep(1.5)
        tail = _frame({"type": "response", "content": "HP-2041 shipped inside 30 days."}, "r1-4") \
            + _frame({"type": "done"}, "r1-5")
        await page.evaluate(f"() => window.__streams.post.push({json.dumps(tail)})")
        if not await _on_screen(page, "HP-2041 shipped inside 30 days."):
            raise AssertionError("the turn's answer never showed")
        await asyncio.sleep(1.0)
        # On screen in the conversation; since TABS1 the tab's title is its first question too.
        text = await page.evaluate("""() => {
            const all = document.body.innerText;
            const strip = [...document.querySelectorAll('[role=tablist]')].map(e => e.innerText).join('\\n');
            return all.replace(strip, '');
        }""")
        calls = await page.evaluate("() => window.__calls")
    finally:
        await ctx.close()
    if any("/events" in c["url"] for c in calls):
        raise AssertionError("the page-open read followed the turn it was already reading (F552)")
    for shown in ("HP-2041 shipped inside 30 days.", "sent before the page read its conversation"):
        if text.count(shown) != 1:
            raise AssertionError(f"{shown!r} is on screen {text.count(shown)} times")
    return "a message sent before the page read its conversation is drawn once, its turn followed once"


@check
async def check_thoughtecho(browser) -> str:
    """The panels beside an answer do not repeat the answer.

    The engine sends each turn's text as `llm_thought`, the answering turn
    included, so the panel of the agent's turns ended with the answer itself —
    measured on a real turn: the answer on screen twice with the panels open
    (F102). The model's own reasoning still shows, and so does an earlier turn.
    """
    ctx, page = await _chat_page(browser, "uismoke-echo")
    answer = "The tide turns at 4:12 this afternoon."
    try:
        box = page.locator("main textarea").last
        await box.fill("when does the tide turn?")
        await box.press("Enter")
        await _stream_open(page, "post")
        frames = [
            _frame({"type": "llm_reasoning", "reasoning": "Look it up, then say when.", "turn": 1}, "e-1"),
            _frame({"type": "llm_thought", "thought": "I will check the tide table first.", "turn": 1}, "e-2"),
            _frame({"type": "llm_thought", "thought": answer, "turn": 2}, "e-3"),
            _frame({"type": "response", "content": answer}, "e-4"),
            _frame({"type": "done"}, "e-5"),
        ]
        await page.evaluate(
            "(frames) => frames.forEach(f => window.__streams.post.push(f))", frames)
        if not await _on_screen(page, answer):
            raise AssertionError("the answer never reached the screen")
        # Two panels: "Thinking" is the model's own reasoning, "Reasoning" the
        # text of each turn the agent took.
        for toggle in await page.get_by_role("button", name=re.compile("reasoning|thinking", re.I)).all():
            await toggle.click()
        await asyncio.sleep(0.5)
        text = await page.evaluate("() => document.body.innerText")
    finally:
        await ctx.close()
    if text.count(answer) != 1:
        raise AssertionError(f"with its panels open the answer is on screen {text.count(answer)} times")
    for kept in ("Look it up, then say when.", "I will check the tide table first."):
        if kept not in text:
            raise AssertionError(f"a panel lost what it should keep: {kept!r}")
    return "the answer is shown once; the reasoning and the earlier turn stay"


@check
async def check_chatrecover(browser) -> str:
    """A broken stream is read again from its cursor, never sent again.

    The old fallback re-ran the whole turn over `POST /api/chat` — its tools
    and all — while the first run was still going (F49).
    """
    ctx, page = await _chat_page(browser, "uismoke-recover")
    try:
        box = page.locator("main textarea").last
        await box.fill("recover me")
        await box.press("Enter")
        await _stream_open(page, "post")
        await page.evaluate(
            f"() => window.__streams.post.push({json.dumps(_frame({'type': 'status', 'message': 'working'}, 't2-1'))})")
        await asyncio.sleep(0.3)
        await page.evaluate("() => window.__streams.post.fail()")
        await _stream_open(page, "events")
        await page.evaluate(
            f"() => {{ window.__streams.events.push({json.dumps(_frame({'type': 'response', 'content': 'recovered answer'}, 't2-2'))});"
            f" window.__streams.events.push({json.dumps(_frame({'type': 'done'}, 't2-3'))}); }}")
        if not await _on_screen(page, "recovered answer"):
            raise AssertionError("the answer read after the break never reached the screen")
        calls = await page.evaluate("() => window.__calls")
    finally:
        await ctx.close()
    resent = [c for c in calls if c["method"] == "POST" and c["url"].rstrip("/").endswith("/api/chat")]
    if resent:
        raise AssertionError("a broken stream sent the turn again over POST /api/chat")
    follows = [c["url"] for c in calls if "/events" in c["url"]]
    if not any("after=t2-1" in u for u in follows):
        raise AssertionError(f"the turn was not read again from the last event handled: {follows}")
    return "a broken stream is read again from its cursor, and nothing is re-sent"


@check
async def check_chatleave(browser) -> str:
    """Leaving mid-turn neither asks nor stops: the turn is the server's (F49).

    The rail used to confirm "Navigating away will cancel it" and then cancel it.
    """
    ctx, page = await _chat_page(browser, "uismoke-leave")
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
    try:
        box = page.locator("main textarea").last
        await box.fill("leave me")
        await box.press("Enter")
        await _stream_open(page, "post")
        await page.evaluate(
            f"() => window.__streams.post.push({json.dumps(_frame({'type': 'status', 'message': 'working'}, 't3-1'))})")
        await asyncio.sleep(0.3)
        await page.get_by_role("link", name="Agents").first.click()
        await asyncio.sleep(1.5)
        left = "/agents" in page.url
        # The rail navigates client-side, so the page's record survives the move.
        calls = await page.evaluate("() => window.__calls || []")
    finally:
        await ctx.close()
    if dialogs:
        raise AssertionError(f"leaving mid-turn asked first: {dialogs[0]!r}")
    if not left:
        raise AssertionError("the rail did not leave the chat page")
    if any("/cancel" in c["url"] for c in calls):
        raise AssertionError("leaving mid-turn cancelled the turn")
    return "leaving mid-turn asks nothing and stops nothing"


# ── chat tabs (TABS1, D319) ──────────────────────────────────────────────────

#: Chat's network for several tabs, over CHAT_STUB. Streams are also kept by
#: conversation (`post:<session>`, `events:<session>`), histories and statuses
#: are answered per session from `window.__histories` / `window.__status`, a
#: turn's body is recorded, and an aborted read is noted and its stream broken —
#: the way a real fetch's body dies when its signal aborts.
TABS_STUB = r"""
window.__histories = {};
window.__status = {};
window.__bodies = [];
window.__aborted = [];
const _tabsInner = window.fetch;
const _json = (v) => Promise.resolve(new Response(JSON.stringify(v), {
  status: 200, headers: {'Content-Type': 'application/json'},
}));
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
  if (url.includes('/api/sessions/status') && method === 'POST') {
    const asked = JSON.parse((init && init.body) || '{"sessions": []}').sessions || [];
    window.__calls.push({url: url, method: method, ids: asked.map((s) => s.session_id)});
    return _json({sessions: asked.map((s) => Object.assign({
      session_id: s.session_id, agent_id: s.agent_id, running: false, automated: false,
      pending_approval: false, turn_count: 0, last_updated: null,
    }, window.__status[s.session_id] || {}))});
  }
  const h = /\/api\/sessions\/([^/?]+)\/history/.exec(url);
  if (h && window.__histories[decodeURIComponent(h[1])]) {
    window.__calls.push({url: url, method: method});
    return _json(window.__histories[decodeURIComponent(h[1])]);
  }
  let name = null;
  if (url.includes('/api/chat/stream') && init && init.body) {
    const body = JSON.parse(init.body);
    window.__bodies.push(body);
    name = 'post:' + body.session_id;
  }
  const ev = /\/api\/chat\/([^/?]+)\/events/.exec(url);
  if (ev) name = 'events:' + decodeURIComponent(ev[1]);
  const answered = _tabsInner(input, init);
  if (name) {
    const plain = name.startsWith('post:') ? 'post' : 'events';
    window.__streams[name] = window.__streams[plain];
    const signal = init && init.signal;
    if (signal) signal.addEventListener('abort', () => {
      window.__aborted.push(name);
      const s = window.__streams[name];
      if (s) s.fail();
    });
  }
  return answered;
};
"""

TAB_ALPHA, TAB_BETA = "agent_uismoke_tab_alpha", "agent_uismoke_tab_beta"


def _tab_agents() -> None:
    api("/api/agents", "POST", _agent(TAB_ALPHA, "UI smoke Tab Alpha"))
    api("/api/agents", "POST", _agent(TAB_BETA, "UI smoke Tab Beta"))


async def _tabs_page(browser, session_id: str, agent_id: str = TAB_ALPHA, width=WIDE, histories: dict | None = None,
                     status: dict | None = None):
    """A chat page whose first tab is `session_id` under `agent_id`, the way a page before tabs left it.

    `histories` and `status` are set again on every load, so they hold across a reload.
    """
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    await ctx.add_init_script(CHAT_STUB)
    await ctx.add_init_script(TABS_STUB)
    await ctx.add_init_script(
        f"try {{ if (!sessionStorage.getItem('uismokeSeeded')) {{"
        f" localStorage.setItem('synapseSessionId', {json.dumps(session_id)});"
        f" localStorage.setItem('synapseAgentId', {json.dumps(agent_id)});"
        f" sessionStorage.setItem('uismokeSeeded', '1'); }} }} catch (e) {{}}"
        + (f"\nwindow.__histories = {json.dumps(histories)};" if histories else "")
        + (f"\nwindow.__status = {json.dumps(status)};" if status else ""))
    page = await ctx.new_page()
    await page.goto(APP, wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE)
    return ctx, page


def _strip(page):
    return page.get_by_role("tablist", name="Open conversations")


async def _tab_titles(page) -> list[str]:
    return [t.strip() for t in await _strip(page).get_by_role("tab").all_inner_texts()]


async def _new_tab(page, agent_name: str) -> None:
    await page.get_by_role("button", name="New tab").click()
    await page.get_by_role("option", name=re.compile(re.escape(agent_name))).first.click()
    await asyncio.sleep(0.8)


async def _send(page, text: str) -> None:
    box = page.locator("main textarea").last
    await box.fill(text)
    await box.press("Enter")


async def _wait(page, js: str, tries: int = 40) -> bool:
    for _ in range(tries):
        if await page.evaluate(js):
            return True
        await asyncio.sleep(0.25)
    return False


@check
async def check_tabsonestream(browser) -> str:
    """A turn running in one tab survives a switch to another, read once, and only the front tab streams (TABS1).

    Ten tabs each holding a stream would stall the page: plain HTTP allows about
    six connections per host. So a tab going behind stops reading — never the
    turn — and coming back reads its turn again from the start, drawing the
    question and the answer exactly once.
    """
    _tab_agents()
    ctx, page = await _tabs_page(browser, "uismoke-tab-a")
    try:
        await _send(page, "first tab question")
        await _stream_open(page, "post")
        await page.evaluate(
            f"() => window.__streams.post.push({json.dumps(_frame({'type': 'status', 'message': 'working'}, 'ta-1'))})")
        await asyncio.sleep(0.3)

        await _new_tab(page, "UI smoke Tab Beta")
        if not await _wait(page, "() => window.__aborted.includes('post:uismoke-tab-a')"):
            raise AssertionError("tab A kept reading its turn behind tab B")
        calls = await page.evaluate("() => window.__calls")
        if any("/cancel" in c["url"] for c in calls):
            raise AssertionError("switching tabs stopped the running turn")

        # Behind, A learns it is working from the one status read, not a stream.
        await page.evaluate("() => { window.__status['uismoke-tab-a'] = {running: true}; }")
        tab_a = _strip(page).get_by_role("tab", name=re.compile("first tab question"))
        for _ in range(40):
            if "working" in ((await tab_a.get_attribute("aria-label")) or (await tab_a.inner_text())).lower():
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("the tab behind never showed that its turn is working")

        await _send(page, "second tab question")
        await _wait(page, "() => window.__bodies.length >= 2")
        bodies = await page.evaluate("() => window.__bodies")
        if bodies[-1].get("agent_id") != TAB_BETA:
            raise AssertionError(f"tab B sent to {bodies[-1].get('agent_id')!r}, not its own agent")
        if bodies[-1].get("session_id") == "uismoke-tab-a":
            raise AssertionError("tab B wrote into tab A's conversation")
        behind = [c for c in await page.evaluate("() => window.__calls")
                  if "/events" in c["url"] and "uismoke-tab-a" in c["url"]]
        if behind:
            raise AssertionError("tab A opened a stream while behind")

        # Back to A: its turn is read again from the start, and the answer lands once.
        await page.evaluate("""() => { window.__histories['uismoke-tab-a'] = {turns: [], pending_approval: null,
            running: {turn_id: 'ta', message: 'first tab question', started_at: 0, cursor: 'ta-0'}}; }""")
        await tab_a.click()
        await _stream_open(page, "events:uismoke-tab-a")
        frames = _frame({"type": "response", "content": "the first tab's answer"}, "ta-2") + _frame({"type": "done"}, "ta-3")
        await page.evaluate(f"() => window.__streams['events:uismoke-tab-a'].push({json.dumps(frames)})")
        if not await _on_screen(page, "the first tab's answer"):
            raise AssertionError("the running turn's answer never reached its tab")
        await asyncio.sleep(1.0)
        # The conversation, not the strip: a tab's title is its first question.
        text = await page.evaluate("""() => {
            const main = document.querySelector('main').innerText;
            const strip = [...document.querySelectorAll('[role=tablist]')].map(e => e.innerText).join('\\n');
            return main.replace(strip, '');
        }""")
        follows = [c["url"] for c in await page.evaluate("() => window.__calls") if "/events" in c["url"]]
    finally:
        await ctx.close()
    for shown in ("first tab question", "the first tab's answer"):
        if text.count(shown) != 1:
            raise AssertionError(f"{shown!r} is in the conversation {text.count(shown)} times")
    if "second tab question" in text:
        raise AssertionError("tab B's message shows in tab A")
    if not any("after=ta-0" in u for u in follows):
        raise AssertionError(f"tab A's turn was not read again from its start: {follows}")
    return "a switch stops reading, never the turn; the tab behind reads its state from one poll; back, the answer lands once"


@check
async def check_tabsneedsyou(browser) -> str:
    """A tab behind waiting on the person says so, and its question is asked once when it comes forward (TABS1)."""
    _tab_agents()
    card = {"tool_name": "send_email", "tool_args": {"to": "accounts@northwind.example"}, "approval_id": "ap_tab",
            "category": "external", "message": "send the reminders",
            "prompt": "The agent wants to run **send_email**", "fields": []}
    ctx, page = await _tabs_page(browser, "uismoke-tab-ask", histories={
        "uismoke-tab-ask": {"turns": [], "pending_approval": card, "running": None}})
    try:
        if not await _on_screen(page, "send_email"):
            raise AssertionError("the waiting question did not show in its own tab")
        await _new_tab(page, "UI smoke Tab Beta")
        await page.evaluate("() => { window.__status['uismoke-tab-ask'] = {pending_approval: true}; }")
        asking = _strip(page).get_by_role("tab").first
        for _ in range(40):
            if "needs you" in ((await asking.get_attribute("aria-label")) or "").lower():
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("the tab behind never said it needs the person")
        await asking.click()
        await asyncio.sleep(1.5)
        text = await page.evaluate("() => document.querySelector('main').innerText")
    finally:
        await ctx.close()
    if text.count("send_email") < 1:
        raise AssertionError("the question was lost on the way back")
    approve = text.lower().count("approve")
    if approve > 2:
        raise AssertionError(f"the question is asked more than once ({approve} approve buttons' worth of text)")
    return "a tab behind waiting on the person says 'needs you'; back, its question is asked once"


@check
async def check_tabsrestore(browser) -> str:
    """Open tabs, their order and the front one come back after a reload (TABS1)."""
    _tab_agents()
    ctx, page = await _tabs_page(browser, "uismoke-tab-r1", histories={
        "uismoke-tab-r1": {"turns": [{"user": "the restored first", "assistant": "kept", "tools": [], "timestamp": ""}],
                           "pending_approval": None, "running": None}})
    try:
        await _new_tab(page, "UI smoke Tab Beta")
        before = await _tab_titles(page)
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        after = await _tab_titles(page)
        front = await _strip(page).get_by_role("tab", selected=True).inner_text()
    finally:
        await ctx.close()
    if len(before) != 2 or after != before:
        raise AssertionError(f"tabs before the reload {before}, after {after}")
    if "Beta" not in front and "New" not in front:
        raise AssertionError(f"the front tab did not come back in front: {front!r}")
    return "two tabs and the front one survive a reload"


@check
async def check_tabsnoagent(browser) -> str:
    """Chat with no agent at all still opens one tab with its composer (TABS1, F924). The first tab started only
    from the workspace's active agent, so a workspace with none — a new install, or every agent removed — showed
    an empty strip and nothing under it (stack 1's baseline: eight chat checks waited for a composer that never
    came). The page before tabs showed its composer with no agent, and a tab with none never sets one."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    posted: list[str] = []

    async def no_agents(route):
        res = await route.fetch()
        body = await res.json()
        body.update({"active_agent_id": None, "agents": {}})
        await route.fulfill(response=res, json=body)

    async def active(route):
        posted.append(route.request.post_data or "")
        await route.continue_()

    async def kept_history(route):
        await route.fulfill(json={"turns": [{"user": "kept before tabs", "assistant": "kept answer", "tools": [],
                                             "timestamp": ""}], "pending_approval": None, "running": None})

    await ctx.route("**/api/status", no_agents)
    await ctx.route("**/api/agents/active", active)
    await ctx.route("**/api/sessions/uismoke-noagent-kept/history*", kept_history)
    try:
        await page.goto(APP, wait_until="domcontentloaded")
        box = page.locator("main textarea").last
        await box.wait_for(timeout=15_000)
        await asyncio.sleep(SETTLE)
        titles = await _tab_titles(page)
        # The conversation the page before tabs kept, with no agent to name: it is still the first tab.
        kept = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        await kept.add_init_script("try { localStorage.setItem('synapseSessionId', 'uismoke-noagent-kept'); } catch (e) {}")
        for pattern, handler in (("**/api/status", no_agents), ("**/api/agents/active", active),
                                 ("**/api/sessions/uismoke-noagent-kept/history*", kept_history)):
            await kept.route(pattern, handler)
        try:
            again = await kept.new_page()
            await again.goto(APP, wait_until="domcontentloaded")
            reopened = await _on_screen(again, "kept answer")
        finally:
            await kept.close()
    finally:
        await ctx.close()
    if len(titles) != 1:
        raise AssertionError(f"with no agent the strip holds {titles}, not one tab")
    if not reopened:
        raise AssertionError("with no agent, the conversation kept before tabs did not reopen")
    if posted:
        raise AssertionError(f"a tab with no agent set the workspace's active agent: {posted}")
    return "with no agent, one tab and its composer, and a kept conversation reopens; no active agent is set"


@check
async def check_tabsdeeplink(browser) -> str:
    """A link to a conversation opens it in a tab, or brings its open tab forward (TABS1)."""
    _tab_agents()
    ctx, page = await _tabs_page(browser, "uismoke-tab-d1", histories={
        "uismoke-tab-d2": {"turns": [{"user": "linked conversation", "assistant": "here", "tools": [], "timestamp": ""}],
                           "pending_approval": None, "running": None}})
    try:
        await page.goto(f"{APP}/?session=uismoke-tab-d2&agent={TAB_BETA}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        once = await _tab_titles(page)
        if not await _on_screen(page, "linked conversation"):
            raise AssertionError("the linked conversation did not open")
        await page.goto(f"{APP}/?session=uismoke-tab-d2&agent={TAB_BETA}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        twice = await _tab_titles(page)
    finally:
        await ctx.close()
    if len(once) != 2 or len(twice) != 2:
        raise AssertionError(f"the link opened a tab each time: {once} then {twice}")
    return "a link opens its conversation in a tab once, then brings that tab forward"


@check
async def check_tabskeys(browser) -> str:
    """Alt+T opens a new tab's picker, Alt+1…9 jumps, Alt+W closes; the browser's own keys stay the browser's (TABS1)."""
    _tab_agents()
    ctx, page = await _tabs_page(browser, "uismoke-tab-k1")
    try:
        await _new_tab(page, "UI smoke Tab Beta")
        await page.keyboard.press("Alt+1")
        await asyncio.sleep(0.6)
        first = await _strip(page).get_by_role("tab", selected=True).inner_text()
        await page.keyboard.press("Alt+t")
        await asyncio.sleep(0.5)
        picker = await page.get_by_role("dialog", name="Start a conversation with").count()
        await page.keyboard.press("Escape")
        await page.keyboard.press("Alt+2")
        await asyncio.sleep(0.4)
        await page.keyboard.press("Alt+w")
        await asyncio.sleep(0.6)
        left = await _tab_titles(page)
    finally:
        await ctx.close()
    if "Beta" in first:
        raise AssertionError("Alt+1 did not bring the first tab forward")
    if not picker:
        raise AssertionError("Alt+T did not open the new tab's agent picker")
    if len(left) != 1:
        raise AssertionError(f"Alt+W did not close the front tab: {left}")
    return "Alt+T, Alt+1…9 and Alt+W drive the strip"


@check
async def check_tabscap(browser) -> str:
    """Ten tabs at most: the oldest idle one closes, and a working one never does (TABS1)."""
    _tab_agents()
    # The first tab works the whole time, across every load; it must survive the cap.
    ctx, page = await _tabs_page(browser, "uismoke-tab-c0", status={"uismoke-tab-c0": {"running": True}})
    try:
        for i in range(1, 11):
            await page.goto(f"{APP}/?session=uismoke-tab-c{i}&agent={TAB_ALPHA}", wait_until="domcontentloaded")
            await asyncio.sleep(1.2)
        await asyncio.sleep(SETTLE)
        # What the page saved is what is open: the strip shows what fits and keeps the rest behind "N more".
        sessions = await page.evaluate("""() => Object.keys(localStorage).filter(k => k.startsWith('synapseTabs:') && k.endsWith(':chat'))
            .flatMap(k => (JSON.parse(localStorage.getItem(k) || '{}').tabs || []).map(t => t.sessionId))""")
        more = await page.get_by_role("button", name=re.compile(r"^\d+ more")).count()
    finally:
        await ctx.close()
    if len(sessions) != 10:
        raise AssertionError(f"{len(sessions)} tabs are open, not the cap of 10")
    if "uismoke-tab-c0" not in sessions:
        raise AssertionError("the cap closed the tab whose turn was working")
    if "uismoke-tab-c1" in sessions:
        raise AssertionError("the cap did not close the oldest idle tab")
    if not more:
        raise AssertionError("ten tabs fit the strip at 1600 px with no 'N more'")
    return "ten tabs at most; the oldest idle one closed, the working one kept, the rest behind 'N more'"


@check
async def check_tabsphone(browser) -> str:
    """On a phone the strip gives way to one switcher that opens a sheet of every tab (TABS1)."""
    _tab_agents()
    ctx, page = await _tabs_page(browser, "uismoke-tab-p1", width=PHONE)
    try:
        if await _strip(page).is_visible():
            raise AssertionError("the tab strip shows at phone width")
        switcher = page.get_by_role("button", name=re.compile(r"^Switch tab"))
        # The tour's "new tab" stop has a "+" to stand on at phone width too (GUIDE1).
        if not await page.locator('[data-tour="chat-new-tab"]:visible').count():
            raise AssertionError("no visible chat-new-tab anchor at phone width: the tour skips its stop")
        await switcher.click()
        sheet = page.get_by_role("dialog", name=re.compile(r"open tabs$"))
        await sheet.wait_for(timeout=5_000)
        await sheet.get_by_role("button", name=re.compile("New tab")).click()
        await page.get_by_role("option", name=re.compile("UI smoke Tab Beta")).first.click()
        await asyncio.sleep(0.8)
        label = await switcher.get_attribute("aria-label") or ""
        overflow = await page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
    finally:
        await ctx.close()
    if "2 open" not in label:
        raise AssertionError(f"the switcher does not count two tabs: {label!r}")
    if overflow:
        raise AssertionError("the page scrolls sideways at phone width")
    return "the phone switcher counts the tabs and its sheet opens a new one"


# ── several Synapse AI chats (TABS2, D327) ───────────────────────────────────

#: Synapse AI's routes, kept per chat in the page: a turn (`/api/builder/chat`) is recorded and answered at once and
#: its words kept under its `client_session_id`, so reading a chat back (`/api/builder/conversation?session=`) draws
#: what was said in it. `window.__chatList` is the panel's list, `window.__history` History's rows. No model is called.
ASSISTANT_STUB = r"""
window.__chats = window.__chats || {};
window.__sent = [];
window.__asked = [];
const _assistantInner = window.fetch;
const _reply = (v) => Promise.resolve(new Response(JSON.stringify(v), {
  status: 200, headers: {'Content-Type': 'application/json'},
}));
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || 'GET').toUpperCase();
  if (url.includes('/api/builder/conversations')) {
    window.__asked.push(url);
    return _reply({conversations: window.__chatList || []});
  }
  if (url.includes('/api/builder/conversation')) {
    window.__asked.push(url);
    const id = new URL(url, location.href).searchParams.get('session');
    const turns = id ? window.__chats[id] : null;
    return _reply({conversation: turns && turns.length ? {
      client_session_id: id, run_id: 'run_' + id, status: 'completed', turns: turns, pending: null, cursor: '0',
    } : null});
  }
  if (url.includes('/api/builder/chat') && method === 'POST') {
    const body = JSON.parse((init && init.body) || '{}');
    window.__sent.push(body);
    const id = body.client_session_id;
    (window.__chats[id] = window.__chats[id] || []).push({user: body.message, assistant: 'heard: ' + body.message});
    const held = window.__openChat ? 'data: ' + JSON.stringify(Object.assign({type: 'open_chat'}, window.__openChat)) + '\n\n' : '';
    const frames = held + 'data: ' + JSON.stringify({type: 'orchestration_complete', run_id: 'run_' + id + '_' + window.__sent.length,
      final_state: {summary: 'heard: ' + body.message}}) + '\n\n';
    return Promise.resolve(new Response(frames, {status: 200, headers: {'Content-Type': 'text/event-stream'}}));
  }
  if (window.__history && method === 'GET' && /\/api\/sessions(\?|$)/.test(url)) {
    window.__asked.push(url);
    return _reply(window.__history);
  }
  return _assistantInner(input, init);
};
"""

ASSISTANT_NAME = "Synapse AI"


def _assistant_agent() -> None:
    """Synapse AI, the agent a new install starts with (the installer makes it; a scratch store has none)."""
    try:
        api("/api/agents/agent_synapse_ai")
    except urllib.error.HTTPError:
        api("/api/agents", "POST", {**_agent("agent_synapse_ai", ASSISTANT_NAME), "type": "builder",
                                    "system_prompt": ""})


async def _assistant_page(browser, path: str = "", chats: dict | None = None, chat_list: list | None = None,
                          history: list | None = None, width=WIDE):
    """The chat page with Synapse AI's routes stubbed (ASSISTANT_STUB); its first tab is an agent's, as a new
    install's is. `chats` are the chats on the "server", by id."""
    _tab_agents()
    _assistant_agent()
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    await ctx.add_init_script(
        "try { if (!sessionStorage.getItem('uismokeSeeded')) {"
        f" localStorage.setItem('synapseSessionId', 'uismoke-sai-first');"
        f" localStorage.setItem('synapseAgentId', {json.dumps(TAB_ALPHA)});"
        " sessionStorage.setItem('uismokeSeeded', '1'); } } catch (e) {}"
        f"\nwindow.__chats = {json.dumps(chats or {})};"
        + (f"\nwindow.__chatList = {json.dumps(chat_list)};" if chat_list is not None else "")
        + (f"\nwindow.__history = {json.dumps(history)};" if history is not None else ""))
    await ctx.add_init_script(ASSISTANT_STUB)
    page = await ctx.new_page()
    await page.goto(f"{APP}/{path}", wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE)
    return ctx, page


async def _assistant_ids(page) -> list[str]:
    return [b.get("client_session_id") for b in await page.evaluate("() => window.__sent")]


def _conversation_text(page_js_main: str = "main") -> str:
    """The conversation's words, without the strip's titles (a tab's title is its first question)."""
    return f"""() => {{
        const main = document.querySelector('{page_js_main}').innerText;
        const strip = [...document.querySelectorAll('[role=tablist]')].map(e => e.innerText).join('\\n');
        return main.replace(strip, '');
    }}"""


@check
async def check_tabsassistantnew(browser) -> str:
    """Each new Synapse AI tab is a new chat, and each keeps its own words (TABS2, F1061).

    TABS1 kept one Synapse AI tab, the panel's one conversation. Naveen, 2026-10-27: a new tab is a new chat.
    """
    ctx, page = await _assistant_page(browser)
    try:
        await _new_tab(page, ASSISTANT_NAME)
        await _send(page, "the first chat's ask")
        if not await _on_screen(page, "heard: the first chat's ask"):
            raise AssertionError("the first Synapse AI tab's turn never answered")
        await _new_tab(page, ASSISTANT_NAME)
        second = await page.evaluate(_conversation_text())
        await _send(page, "the second chat's ask")
        if not await _on_screen(page, "heard: the second chat's ask"):
            raise AssertionError("the second Synapse AI tab's turn never answered")
        titles = await _tab_titles(page)
        await _strip(page).get_by_role("tab", name=re.compile("the first chat's ask")).click()
        await asyncio.sleep(1.5)
        first = await page.evaluate(_conversation_text())
        ids = await _assistant_ids(page)
    finally:
        await ctx.close()
    if "the first chat's ask" in second:
        raise AssertionError("a new Synapse AI tab opened on the first chat, not a new one")
    if sum("chat's ask" in t for t in titles) != 2:
        raise AssertionError(f"two Synapse AI chats are not two tabs: {titles}")
    if len(ids) != 2 or ids[0] == ids[1] or not all(ids):
        raise AssertionError(f"the two tabs' turns did not go to two chats: {ids}")
    if "the first chat's ask" not in first or "the second chat's ask" in first:
        raise AssertionError("back on the first tab, its chat is not its own")
    return "two Synapse AI tabs are two chats, each with its own words and its own id"


@check
async def check_tabsassistanthistory(browser) -> str:
    """History lists a Synapse AI chat among the agents', and opening it carries that chat on (TABS2, F1062)."""
    cid = "uismoke-sai-old"
    history = [
        {"session_id": cid, "agent_id": "agent_synapse_ai", "first_user_message": "the weekly digest chat",
         "last_response": "Built it.", "last_updated": "2026-10-27T09:00:00.000000Z", "turn_count": 1},
        {"session_id": "uismoke-agent-old", "agent_id": TAB_ALPHA, "first_user_message": "an agent's chat",
         "last_response": "ok", "last_updated": "2026-10-27T08:00:00.000000Z", "turn_count": 1},
    ]
    ctx, page = await _assistant_page(browser, chats={cid: [{"user": "the weekly digest chat", "assistant": "Built it."}]},
                                      history=history)
    try:
        await page.get_by_role("button", name="History").first.click()
        row = page.get_by_role("button", name=re.compile("the weekly digest chat")).first
        await row.wait_for(timeout=8_000)
        named = await page.evaluate(
            "() => [...document.querySelectorAll('button')].find(b => b.innerText.includes('the weekly digest chat'))"
            "?.innerText || ''")
        await row.click()
        if not await _on_screen(page, "Built it."):
            raise AssertionError("the Synapse AI chat picked from History did not open with its turns")
        await _send(page, "and add Fridays")
        await _on_screen(page, "heard: and add Fridays")
        ids = await _assistant_ids(page)
    finally:
        await ctx.close()
    if ASSISTANT_NAME not in named:
        raise AssertionError(f"History's row does not say it is Synapse AI's: {named!r}")
    if ids != [cid]:
        raise AssertionError(f"the next message did not carry the picked chat on: {ids}")
    return "History lists Synapse AI's chat; opening it shows its turns and the next message continues it"


@check
async def check_panelhistory(browser) -> str:
    """The panel's history icon lists Synapse AI's chats only, and a pick carries that chat on in the panel (F1062)."""
    cid = "uismoke-sai-panel"
    rows = [{"session_id": cid, "title": "move the digest to 8am", "last_response": "Moved.",
             "last_updated": "2026-10-27T09:00:00.000000Z", "turn_count": 1, "running": False, "waiting": False}]
    ctx, page = await _assistant_page(
        browser, path="agents", chats={cid: [{"user": "move the digest to 8am", "assistant": "Moved."}]}, chat_list=rows)
    try:
        await page.locator('[data-testid="assistant-open"]').first.click()
        await asyncio.sleep(0.8)
        icon = page.locator('[data-testid="assistant-panel"] [data-tour="assistant-history"]')
        await icon.click()
        listed = page.locator('[data-testid="assistant-chats"]')
        await listed.wait_for(timeout=8_000)
        await listed.get_by_role("button", name=re.compile("move the digest to 8am")).click()
        await asyncio.sleep(1.0)
        text = await _panel_text(page)
        await page.locator('[data-testid="assistant-panel"] textarea').fill("and on Mondays")
        await page.locator('[data-testid="assistant-panel"] textarea').press("Enter")
        await asyncio.sleep(1.0)
        ids = await _assistant_ids(page)
        asked = await page.evaluate("() => window.__asked")
    finally:
        await ctx.close()
    if not any("/api/builder/conversations" in u for u in asked):
        raise AssertionError("the panel's list did not read Synapse AI's own list")
    if any(re.search(r"/api/sessions(\?|$)", u) for u in asked):
        raise AssertionError("the panel's list read History's, which holds every agent's chats")
    if "Moved." not in text:
        raise AssertionError(f"the picked chat did not open in the panel: {text[:200]!r}")
    if ids != [cid]:
        raise AssertionError(f"the panel's next message did not carry the picked chat on: {ids}")
    return "the panel's clock lists Synapse AI's chats; a pick opens it there and the next message continues it"


@check
async def check_panelandtab(browser) -> str:
    """A chat begun in the panel carries on in a tab, as one conversation (TABS2, F1061)."""
    ctx, page = await _assistant_page(browser, history=[])
    try:
        await page.locator('[data-testid="assistant-open"]').first.click()
        await asyncio.sleep(0.8)
        await page.locator('[data-testid="assistant-panel"] textarea').fill("begun in the panel")
        await page.locator('[data-testid="assistant-panel"] textarea').press("Enter")
        await asyncio.sleep(1.0)
        [cid] = await _assistant_ids(page)
        await page.locator('[data-testid="assistant-panel"] button[aria-label="Close Synapse AI"]').click()
        await asyncio.sleep(0.5)
        await page.evaluate(
            "(id) => { window.__history = [{session_id: id, agent_id: 'agent_synapse_ai',"
            " first_user_message: 'begun in the panel', last_response: 'heard: begun in the panel',"
            " last_updated: new Date().toISOString(), turn_count: 1}]; }", cid)
        await page.get_by_role("button", name="History").first.click()
        await page.get_by_role("button", name=re.compile("begun in the panel")).first.click()
        if not await _on_screen(page, "heard: begun in the panel"):
            raise AssertionError("the panel's chat did not open in a tab")
        await _send(page, "carried on in the tab")
        await _on_screen(page, "heard: carried on in the tab")
        ids = await _assistant_ids(page)
        await page.locator('[data-testid="assistant-open"]').first.click()
        await asyncio.sleep(0.8)
        panel = await _panel_text(page)
    finally:
        await ctx.close()
    if ids != [cid, cid]:
        raise AssertionError(f"the tab did not carry the panel's chat on: {ids}")
    if "carried on in the tab" not in panel:
        raise AssertionError("the panel does not show what the tab added to the same chat")
    return "a chat begun in the panel opens in a tab, carries on there, and the panel shows it"


@check
async def check_assistantdraft(browser) -> str:
    """`?assistant=new&draft=` opens a new Synapse AI tab with the words waiting, labelled, never sent (TABS2; ONB3).

    Anyone can make such a link, so the words are only a suggestion: nothing runs until the person presses Send.
    """
    words = "Make a lead list of design agencies in Leeds and keep it in the Vault"
    ctx, page = await _assistant_page(browser, path=f"?assistant=new&draft={quote(words + chr(0x202E))}")
    try:
        box = page.locator("main textarea").last
        for _ in range(30):
            if await box.input_value() == words:
                break
            await asyncio.sleep(0.2)
        held = await box.input_value()
        labelled = await page.get_by_text("Suggested prompt", exact=True).count()
        focused = await box.evaluate("(el) => document.activeElement === el && el.selectionStart === el.value.length")
        await asyncio.sleep(3.0)
        sent = await page.evaluate("() => window.__sent.length")
        address = page.url
        titles = await _tab_titles(page)
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        after = await _tab_titles(page)
    finally:
        await ctx.close()
    if held != words:
        raise AssertionError(f"the composer holds {held!r}, not the link's words cleaned")
    if not labelled:
        raise AssertionError("the words are not said to be a suggestion")
    if not focused:
        raise AssertionError("the composer is not focused with the caret after the words")
    if sent:
        raise AssertionError("the link's words were sent without the person")
    if "assistant=" in address or "draft=" in address:
        raise AssertionError(f"the address keeps the link's parameters: {address}")
    if len(after) != len(titles):
        raise AssertionError(f"a reload opened another chat: {titles} → {after}")
    return "the link opens a new Synapse AI tab with its words cleaned, labelled and focused, never sent, once"


@check
async def check_openthatchat(browser) -> str:
    """A workflow another of the person's Synapse AI chats holds: "Open that chat" brings that chat forward (F1063).

    In a tab it opens (or focuses) the holding chat's own tab; in the panel it shows that chat there.
    """
    held = "uismoke-sai-holder"
    chats = {held: [{"user": "move the digest to 8am", "assistant": "Here is the change: move it to 8am."}]}
    ctx, page = await _assistant_page(browser, chats=chats)
    try:
        await page.evaluate(
            "(id) => { window.__openChat = {chat_id: id, title: 'move the digest to 8am'}; }", held)
        await _new_tab(page, ASSISTANT_NAME)
        await _send(page, "rename the digest")
        button = page.get_by_role("button", name="Open that chat")
        await button.wait_for(timeout=8_000)
        # Under the answer that says why, not above it.
        after = await page.evaluate("""() => {
            const b = [...document.querySelectorAll('main button')].find(e => e.innerText.trim() === 'Open that chat');
            const said = [...document.querySelectorAll('main *')].find(e => e.childElementCount === 0
                && e.textContent.includes('heard: rename the digest'));
            return !!(b && said && (said.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
        }""")
        if not after:
            raise AssertionError("Open that chat is drawn above the answer that says why")
        await button.click()
        if not await _on_screen(page, "Here is the change: move it to 8am."):
            raise AssertionError("Open that chat did not open the chat holding the workflow in a tab")
        titles = await _tab_titles(page)
        # The panel: the same button shows that chat there.
        await page.locator('[data-testid="assistant-open"]').first.click()
        await asyncio.sleep(0.8)
        await page.locator('[data-testid="assistant-panel"] textarea').fill("rename it again")
        await page.locator('[data-testid="assistant-panel"] textarea').press("Enter")
        await page.locator('[data-testid="assistant-panel"]').get_by_role("button", name="Open that chat").click()
        await asyncio.sleep(1.0)
        panel = await _panel_text(page)
    finally:
        await ctx.close()
    if not any("move the digest" in t for t in titles):
        raise AssertionError(f"no tab for the holding chat: {titles}")
    if "Here is the change: move it to 8am." not in panel:
        raise AssertionError("in the panel, Open that chat did not show the holding chat")
    return "Open that chat brings the holding chat forward: its own tab on Chat, and in the panel"


@check
async def check_cancelanswer(browser) -> str:
    """A cancelled run does not go on asking for an answer (F141).

    `cancel.cancel_run` wrote the status and left `waiting_for_human` set and
    the `paused` entry open, and the screen showed its answer strip for any run
    with a prompt — so a cancelled run kept offering an Answer the API refuses
    with a 409. Nothing cleared it either: an in-process cancel writes the
    checkpoint directly while the engine loop is suspended, so the page sees no
    `orchestration_complete` and no `orchestration_error`.

    Checked twice: in the tab that cancelled, and after a reload, which is
    where the engine's half has to be right.
    """
    oid = f"orch_uismoke_cancel_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run now", exact=True).click()
        for _ in range(25):
            await asyncio.sleep(1.0)
            if await page.get_by_text("Waiting for your input").count():
                break
        else:
            raise AssertionError("the run never reached its question")

        await page.get_by_role("button", name="Stop", exact=True).click()
        await asyncio.sleep(1.5)
        if await page.get_by_text("Waiting for your input").count():
            raise AssertionError("a cancelled run still offers Answer in the tab that cancelled it")

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        if await page.get_by_text("Waiting for your input").count():
            raise AssertionError("a cancelled run offers Answer again after a reload")
        return "a cancelled run stops asking, in the tab and after a reload"
    finally:
        await ctx.close()
        drop_fixture(oid)


# ── shell: notifications and theme (B4) ──────────────────────────────────────

def notify_orchestration(oid: str) -> dict:
    """One print step: a run of it ends in a second with a run_completed notification."""
    return {
        "id": oid,
        "name": "ui-smoke notify",
        "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "steps": [{
            "id": f"{oid}_say", "name": "Say", "type": "print", "print_content": "hello",
            "next_step_id": None, "output_key": "said",
        }],
        "entry_step_id": f"{oid}_say",
        "state_schema": {},
        "trigger": "manual",
    }


def run_to_end(oid: str, timeout: int = 60) -> None:
    """Run an orchestration and read its event stream until the server closes it."""
    req = urllib.request.Request(
        f"{API}/api/orchestrations/{oid}/run",
        method="POST",
        data=json.dumps({"message": "go"}).encode(),
        headers={"Content-Type": "application/json", "X-Synapse-Internal": TOKEN},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


#: The app-wide toast host (NotificationProvider).
TOASTS = "div.fixed.bottom-20.right-6 > div"


async def _toasts_during(page, seconds: float) -> list[str]:
    """Every toast that appears over `seconds`, in order, each counted once."""
    seen: list[str] = []
    end = time.time() + seconds
    while time.time() < end:
        for text in await page.locator(TOASTS).all_inner_texts():
            text = text.strip()
            if text and text not in seen:
                seen.append(text)
        await asyncio.sleep(0.2)
    return seen


@check
async def check_replaytoast(browser) -> str:
    """Notifications already in the ring are history, on every connection (F216).

    The provider used to call an item live once the stream's first read chunk
    had been handled, and kept that across reconnects: a longer replay, or any
    reconnect (a backend restart, a dropped proxy), toasted old runs again. The
    first stream here is answered with one that ends at once, so the page goes
    through exactly that reconnect. A run finished while it watches must still
    toast once, or "no toasts" would pass for a provider that never toasts.
    """
    oid = f"orch_uismoke_notify_{int(time.time())}"
    api("/api/orchestrations", "POST", notify_orchestration(oid))
    run_to_end(oid)                                     # something in the ring
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    first = {"done": False}

    async def cut_first_stream(route):
        if first["done"]:
            await route.continue_()
            return
        first["done"] = True
        await route.fulfill(status=200, content_type="text/event-stream", body=": gone\n\n")

    try:
        await page.route("**/api/notifications/stream*", cut_first_stream)
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        # load, the ended stream, the 5 s retry, the second connection
        replayed = await _toasts_during(page, 12.0)
        if replayed:
            raise AssertionError(f"old notifications toasted again on load/reconnect: {replayed}")
        await asyncio.to_thread(run_to_end, oid)
        fresh = await _toasts_during(page, 4.0)
        if fresh != ["ui-smoke notify completed"]:
            raise AssertionError(f"a run that finished while the page was open toasted {fresh}, not once")
        return "a load and a reconnect toast nothing old; a new run toasts once"
    finally:
        await ctx.close()
        drop_fixture(oid)


@check
async def check_bellmenu(browser) -> str:
    """The bell's menu opens on screen, closes on Escape, and the bell is named (F192).

    Anchored by its right edge under a bell at the top of a 240px rail, the
    panel opened at x = -97 with every title cut, and with unread items the
    button's whole name was its badge ("9+").
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        bad: list[str] = []
        for collapsed in ("0", "1"):
            await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
            await page.evaluate(f"() => localStorage.setItem('synapseRailCollapsed', '{collapsed}')")
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            where = "collapsed rail" if collapsed == "1" else "open rail"
            if not await page.get_by_role("button", name=re.compile(r"^Notifications")).count():
                bad.append(f"{where}: the bell's accessible name does not start with Notifications")
            await page.locator('button[title="Notifications"]').click()
            panel = page.get_by_text("Browser notifications when tab is in background") \
                .locator("xpath=ancestor::div[contains(@class, 'w-80')][1]")
            await panel.wait_for()
            box = await panel.bounding_box()
            if box["x"] < 0 or box["x"] + box["width"] > WIDE[0]:
                bad.append(f"{where}: the panel spans {box['x']:.0f}..{box['x'] + box['width']:.0f}, off the screen")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            if await panel.count():
                bad.append(f"{where}: Escape leaves the panel open")
        if bad:
            raise AssertionError("; ".join(bad))
        return "on screen with the rail open and collapsed, named, closed by Escape"
    finally:
        await ctx.close()


#: Records <html>'s theme the moment <body> is inserted, before anything paints.
PREPAINT_PROBE = """(() => {
  try { localStorage.setItem('synapseTheme', 'light'); } catch (e) {}
  new MutationObserver((m, o) => {
    if (!document.body) return;
    o.disconnect();
    const h = document.documentElement;
    window.__firstPaint = { light: h.getAttribute('data-theme') === 'light', scheme: getComputedStyle(h).colorScheme };
  }).observe(document, { childList: true, subtree: true });
})()"""


@check
async def check_prepaint(browser) -> str:
    """A saved light theme is light from the first frame, native controls too (F220).

    The shell applied the theme after hydration, so every load painted dark
    first; `color-scheme` stayed "normal", so scrollbars and selects never
    followed the theme.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(PREPAINT_PROBE)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        first = await page.evaluate("() => window.__firstPaint")
        if not first or not first["light"]:
            raise AssertionError(f"a saved light theme paints dark first: {first}")
        if first["scheme"] != "light":
            raise AssertionError(f"color-scheme is {first['scheme']!r} under the light theme")
        return "light, with color-scheme light, when <body> is inserted"
    finally:
        await ctx.close()


#: Records the theme the first frame gets when nothing is saved.
DEFAULT_THEME_PROBE = """(() => {
  try { localStorage.removeItem('synapseTheme'); } catch (e) {}
  new MutationObserver((m, o) => {
    if (!document.body) return;
    o.disconnect();
    const h = document.documentElement;
    window.__firstPaint = { theme: h.getAttribute('data-theme'), scheme: getComputedStyle(h).colorScheme };
  }).observe(document, { childList: true, subtree: true });
})()"""


@check
async def check_themedefault(browser) -> str:
    """With no saved theme the app follows the OS, light or dark, from the first frame (M0a, D207).

    Nothing saved used to mean dark whatever the OS said. Now a light OS paints
    light and a dark OS dark, `color-scheme` follows, and nothing is written to
    localStorage until someone picks a theme, so the OS keeps deciding.
    """
    seen = {}
    for scheme in ("light", "dark"):
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, color_scheme=scheme)
        await ctx.add_init_script(DEFAULT_THEME_PROBE)
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
            first = await page.evaluate("() => window.__firstPaint")
            if not first or first["theme"] != scheme or first["scheme"] != scheme:
                raise AssertionError(f"with nothing saved, a {scheme} OS paints {first}")
            await asyncio.sleep(SETTLE)
            saved = await page.evaluate("() => [document.documentElement.getAttribute('data-theme'), localStorage.getItem('synapseTheme')]")
            if saved[0] != scheme:
                raise AssertionError(f"after hydration a {scheme} OS shows {saved[0]!r}")
            if saved[1] is not None:
                raise AssertionError(f"a visit saved a theme nobody picked: {saved[1]!r}")
            seen[scheme] = first["theme"]
        finally:
            await ctx.close()
    return f"nothing saved: a light OS paints {seen['light']} and a dark OS {seen['dark']}, from the first frame, and nothing is saved"


# ── chat and the builder (B5) ────────────────────────────────────────────────

#: `/api/status` as the test says it is, for a page that must describe it.
SERVE_STATUS = """
window.__status = %s;
const _statusFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (/\\/api\\/status(\\?|$)/.test(url)) {
    return Promise.resolve(new Response(JSON.stringify(window.__status), {
      status: 200, headers: {'Content-Type': 'application/json'},
    }));
  }
  return _statusFetch(input, init);
};
"""


def _status(ready: bool, problem: str | None = None) -> dict:
    return {"agents": {}, "active_agent_id": None, "overall": "operational",
            "model": "ollama.mistral", "mode": "local", "provider": "ollama",
            "model_ready": ready, "model_problem": problem}


GREETING = """() => {
  // The empty chat's agent card (M2c): its name, then the line with its model and its state.
  const name = document.querySelector('main [data-agent-label]');
  const line = name && name.nextElementSibling;
  return {
    label: name ? name.textContent + (line ? ' · ' + line.textContent : '') : null,
    toModels: [...document.querySelectorAll('main a')]
      .filter((a) => a.getAttribute('href') === '/settings/models').length,
  };
}"""


@check
async def check_firstrun(browser) -> str:
    """A page no model can answer says so, and where to fix it (F182).

    A fresh install's default model is Ollama's; with nothing listening the
    page said "Ready" and answered the first message with httpx's own text —
    "Local Agent Error: Server error '500 …' … developer.mozilla.org …" — cut
    off by a one-line banner, and nothing pointed at Settings → Models.
    """
    problem = "Ollama is not reachable at http://127.0.0.1:11434."
    ctx, page = await _chat_page(browser, "uismoke-firstrun",
                                 extra_init=SERVE_STATUS % json.dumps(_status(False, problem)))
    try:
        greeting = await page.evaluate(GREETING)
        if "not ready" not in (greeting["label"] or "").lower():
            raise AssertionError(f"with no model able to answer the greeting reads {greeting['label']!r}")
        if not await _on_screen(page, problem):
            raise AssertionError("the greeting does not say what is wrong")
        if not greeting["toModels"]:
            raise AssertionError("the greeting does not point at Settings → Models")

        box = page.locator("main textarea").last
        await box.fill("hello")
        await box.press("Enter")
        await _stream_open(page, "post")
        await page.evaluate(
            f"() => {{ window.__streams.post.push({json.dumps(_frame({'type': 'error', 'kind': 'model', 'message': 'Local Agent Error: ' + problem}, 'f-1'))});"
            f" window.__streams.post.push({json.dumps(_frame({'type': 'done'}, 'f-2'))}); window.__streams.post.close(); }}")
        if not await _on_screen(page, "The model couldn't answer."):
            raise AssertionError("a model failure is shown as a bare error, not as the model's")
        links = await page.evaluate(
            "() => [...document.querySelectorAll('main a')].filter(a => a.getAttribute('href') === '/settings/models').length")
        if not links:
            raise AssertionError("the failed turn does not point at Settings → Models")
    finally:
        await ctx.close()

    ctx, page = await _chat_page(browser, "uismoke-firstrun-ok",
                                 extra_init=SERVE_STATUS % json.dumps(_status(True)))
    try:
        greeting = await page.evaluate(GREETING)
    finally:
        await ctx.close()
    if not (greeting["label"] or "").rstrip().endswith("Ready") or greeting["toModels"]:
        raise AssertionError(f"a model that can answer reads {greeting}")
    return "not ready says why and links to Settings → Models; a model failure does too; ready says Ready"


@check
async def check_turnpause(browser) -> str:
    """A turn that stops to ask is waiting, not broken (F209).

    The stream closes at a pause by design — a tool call waiting for approval,
    or an orchestrator agent's human step — and the page read that close as a
    dropped connection: the question, and under it "The connection to this turn
    ended before it finished. — Try again", offering to re-run a turn that was
    only waiting for its answer.
    """
    pauses = {
        "a tool approval": ({
            "type": "human_input_required", "kind": "tool_approval",
            "prompt": "vault_delete wants to run", "tool_name": "vault_delete",
            "tool_args": {"path": "old.md"}, "category": "destructive",
            "approval_id": "ap_uismoke", "fields": [],
        }, "Permission needed"),
        "a human step": ({
            "type": "human_input_required", "run_id": "run_uismoke_pause",
            "orch_step_id": "s_ask", "prompt": "Your answer?",
            "fields": [{"name": "ans", "label": "Answer", "type": "text"}],
        }, "Your answer?"),
    }
    for what, (event, shown) in pauses.items():
        ctx, page = await _chat_page(browser, f"uismoke-pause-{event.get('kind') or 'human'}")
        try:
            box = page.locator("main textarea").last
            await box.fill("do the thing")
            await box.press("Enter")
            await _stream_open(page, "post")
            await page.evaluate(
                f"() => {{ window.__streams.post.push({json.dumps(_frame(event, 'p-1'))});"
                f" window.__streams.post.push({json.dumps(_frame({'type': 'done'}, 'p-2'))});"
                " window.__streams.post.close(); }")
            # `innerText` applies text-transform, so compare without case.
            for _ in range(30):
                if shown.lower() in (await page.evaluate("() => document.body.innerText")).lower():
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError(f"{what}: the pause never rendered ({shown!r})")
            await asyncio.sleep(1.0)
            text = await page.evaluate("() => document.querySelector('main').innerText")
        finally:
            await ctx.close()
        if "connection to this turn ended" in text or "Try again" in text:
            raise AssertionError(f"{what}: a turn waiting for its answer is reported as a dropped connection")
    return "a tool approval and a human step each wait without a failure bar"


#: The builder's conversation as the backend would bring it back after a
#: reload, and every stream the panel then opens held open by the test:
#: `/api/builder/resume` and a run's journal (`/events/stream`). Each call is
#: recorded with its body.
BUILDER_BACK = r"""
window.__calls = [];
window.__follow = {};
const _backFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || 'GET').toUpperCase();
  if (url.includes('/api/builder/conversation')) {
    return Promise.resolve(new Response(JSON.stringify({conversation: window.__conversation}), {
      status: 200, headers: {'Content-Type': 'application/json'},
    }));
  }
  if (url.includes('/api/builder/') || url.includes('/events/stream')) {
    window.__calls.push({url: url, method: method, body: init && init.body ? String(init.body) : null});
    const body = new ReadableStream({
      start(c) {
        window.__follow.push = (event, id) => c.enqueue(new TextEncoder().encode(
          (id != null ? 'id: ' + id + '\n' : '') + 'data: ' + JSON.stringify(event) + '\n\n'));
        window.__follow.close = () => { try { c.close(); } catch (e) {} };
      },
      cancel() { window.__follow.cancelled = true; },
    });
    return Promise.resolve(new Response(body, {
      status: 200, headers: {'Content-Type': 'text/event-stream'},
    }));
  }
  return _backFetch(input, init);
};
"""


async def _builder_back(browser, conversation: dict):
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(f"window.__conversation = {json.dumps(conversation)};")
    await ctx.add_init_script(BUILDER_BACK)
    page = await ctx.new_page()
    await page.goto(APP, wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE + 1.0)
    return ctx, page


async def _panel_text(page) -> str:
    return await page.evaluate(
        "() => (document.querySelector('[data-testid=\"assistant-panel\"]') || {}).innerText || ''")


@check
async def check_builderreload(browser) -> str:
    """A reload brings the builder's conversation back (F214).

    The panel held its conversation in React state only: a reload showed the
    greeting while the server finished the turn, and a plan waiting for
    approval stayed paused on no screen, for ever. Both shapes are checked: a
    plan waiting (redrawn, and the next message answers it), and a build still
    working (followed from its journal until it asks).
    """
    # ── a plan waiting for approval ─────────────────────────────────────────
    parked = {
        "client_session_id": "conv-uismoke", "run_id": RUN_ID, "status": "paused",
        "turns": [{"user": "a weekly digest of the tickets", "assistant": ""}],
        "pending": {"run_id": RUN_ID, "field": "feedback", "step_id": "step_human_review",
                    "prompt": "Here is the proposed plan: one step that summarises."},
    }
    ctx, page = await _builder_back(browser, parked)
    try:
        # The menu's Synapse AI row says so (the corner launcher did, until M2c retired it).
        row = page.locator('[data-testid="assistant-open"]').first
        await row.wait_for(timeout=10_000)
        named = await row.inner_text()
        if "waiting" not in named:
            raise AssertionError(f"a plan waiting after a reload does not show on the Synapse AI row ({named!r})")
        await row.click()
        await asyncio.sleep(0.5)
        text = await _panel_text(page)
        for shown in ("a weekly digest of the tickets", "Here is the proposed plan: one step that summarises."):
            if shown not in text:
                raise AssertionError(f"after a reload the panel does not show {shown!r}")
        await page.locator('[data-testid="assistant-panel"] textarea').fill("approve")
        # With a question open, Send is named for what it does.
        send = page.get_by_role("button", name="Send your answer", exact=True)
        for _ in range(20):
            if not await send.is_disabled():
                break
            await asyncio.sleep(0.1)
        await send.click()
        await asyncio.sleep(0.5)
        calls = await page.evaluate("() => window.__calls")
    finally:
        await ctx.close()
    if any("/api/builder/chat" in c["url"] for c in calls):
        raise AssertionError("answering the plan brought back by a reload started a new conversation")
    resumed = [json.loads(c["body"]) for c in calls if "/api/builder/resume" in c["url"] and c["body"]]
    if not resumed or resumed[0].get("run_id") != RUN_ID or resumed[0].get("response") != {"feedback": "approve"}:
        raise AssertionError(f"the answer did not resume the waiting run: {calls}")

    # ── a build still working ───────────────────────────────────────────────
    # Its journal is replayed from the start, so the question it already had
    # answered (id 3, at or before the cursor) comes past again; reading must
    # not stop there, only at a question asked after the conversation was read.
    running = {
        "client_session_id": "conv-uismoke", "run_id": RUN_ID, "status": "running",
        "turns": [{"user": "build the digest", "assistant": ""}], "pending": None, "cursor": "5",
    }
    ctx, page = await _builder_back(browser, running)
    try:
        for _ in range(40):
            if await page.evaluate("() => typeof (window.__follow || {}).push === 'function'"):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("a build still working after a reload is not followed")
        await page.evaluate(
            f"(id) => {{ window.__follow.push({{type: 'human_input_required', run_id: id,"
            f" orch_step_id: 'step_human_clarify', prompt: 'Which tickets?', fields: [{{name: 'answer'}}]}}, 3);"
            " window.__follow.push({type: '_step_progress', orch_step_id: 's1'}, 4);"
            " window.__follow.push({type: 'step_start', orch_step_id: 's1', step_name: 'Draft the plan'}, 6); }", RUN_ID)
        await asyncio.sleep(0.5)
        if await page.evaluate("() => !!window.__follow.cancelled"):
            raise AssertionError("reading stopped at a question the build had already had answered")
        await _build_with_ai(page)
        await asyncio.sleep(0.5)
        if "Draft the plan" not in await _panel_text(page):
            raise AssertionError("the followed build's progress never reached the panel")
        await page.evaluate(
            f"(id) => window.__follow.push({{type: 'human_input_required', run_id: id,"
            f" orch_step_id: 'step_human_review', prompt: 'Is this plan right?',"
            f" fields: [{{name: 'feedback'}}]}}, 7)", RUN_ID)
        state = await _settle(page, "(s) => s.parked && s.textareaDisabled === false")
        stopped = await page.evaluate("() => !!window.__follow.cancelled")
        calls = await page.evaluate("() => window.__calls")
    finally:
        await ctx.close()
    if not any(f"/runs/{RUN_ID}/events/stream?after=0" in c["url"] for c in calls):
        raise AssertionError(f"the build was not read from its journal's start: {calls}")
    if not state.get("parked") or state.get("textareaDisabled") is not False:
        raise AssertionError(f"the followed build's question cannot be answered: {state}")
    if not stopped:
        raise AssertionError("the panel kept reading the journal past the question it answers elsewhere")
    return "a waiting plan comes back and is answered; a working build is followed until it asks"


#: Agent switches recorded instead of sent: the stub's agents do not exist.
RECORD_SWITCH = """
window.__switches = [];
const _switchFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || 'GET').toUpperCase();
  if (url.includes('/api/agents/active') && method === 'POST') {
    window.__switches.push(JSON.parse((init && init.body) || '{}'));
    return Promise.resolve(new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}));
  }
  return _switchFetch(input, init);
};
"""


@check
async def check_agentmenu(browser) -> str:
    """Chat's agent switcher opens from the keyboard, and at phone width (F185).

    It was a `<div>` whose list appeared on hover: not focusable, no keyboard or
    touch path, and hidden below `sm`, so a phone could not change agent at all.
    Its rows stayed in the tab order while invisible.

    Since F271 it is a searchable list (a Popover and `cmdk`, as the model
    picker is), so its rows are `option`s and the keyboard walks them from the
    search box with the arrow keys.
    """
    status = {**_status(True), "agents": {
        "agent_alpha": {"name": "Alpha", "status": "online", "can_see_images": True},
        "agent_beta": {"name": "Beta", "status": "online", "can_see_images": True},
    }, "active_agent_id": "agent_alpha"}
    for width in (WIDE, PHONE):
        ctx, page = await _chat_page(
            browser, "uismoke-agentmenu", width=width,
            extra_init=SERVE_STATUS % json.dumps(status) + RECORD_SWITCH)
        try:
            trigger = page.get_by_role("button", name=re.compile(r"^agents", re.I))
            if not await trigger.count() or not await trigger.first.is_visible():
                raise AssertionError(f"at {width[0]}px there is no visible button that switches agent")
            await trigger.first.focus()
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
            items = await page.get_by_role("option").all_inner_texts()
            if not any("Beta" in t for t in items) or not any("Alpha" in t for t in items):
                raise AssertionError(f"at {width[0]}px the switcher lists {items}")
            for _ in range(len(items)):
                if await page.get_by_role("option", name=re.compile("Beta")).get_attribute("aria-selected") == "true":
                    break
                await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.8)
            switched = await page.evaluate("() => window.__switches")
            # The switcher is the composer's pill since M2c, so look in the whole page, past the pill itself.
            hidden_rows = await page.evaluate(
                "() => [...document.querySelectorAll('main button')]"
                ".filter(b => !/^agents/i.test(b.getAttribute('aria-label') || ''))"
                ".filter(b => /^(Alpha|Beta)/.test(b.innerText.trim())).length")
        finally:
            await ctx.close()
        if switched[-1:] != [{"agent_id": "agent_beta"}]:
            raise AssertionError(f"at {width[0]}px choosing Beta sent {switched}")
        if hidden_rows:
            raise AssertionError(f"at {width[0]}px the closed switcher leaves {hidden_rows} agent rows on the page")
    return "a named button at both widths; Enter opens it, Enter on an agent switches to it"


#: A conversation's history held until the test lets it go, and every chat
#: message's body recorded: the race is between a reopen still waiting on its
#: history and whatever the person does meanwhile.
HOLD_HISTORY = """
window.__sent = [];
const _holdFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  if (/\\/api\\/sessions\\/[^/?]+\\/history/.test(url)) {
    return new Promise((resolve) => { window.__releaseHistory = () => resolve(_holdFetch(input, init)); });
  }
  if (url.includes('/api/chat/stream') && init && init.body) window.__sent.push(JSON.parse(init.body));
  return _holdFetch(input, init);
};
"""


@check
async def check_newchatrace(browser) -> str:
    """New Chat pressed while the page is still reopening is not undone (F188).

    The reopen applied its conversation whenever its history arrived: pressed
    before that, New Chat emptied the page, the old conversation was drawn
    back over it, and the next message went into the old conversation. Since
    TABS1 a new chat is a new tab, opened here while the first is reopening.
    """
    history = {"turns": [{"user": "an old question", "assistant": "an old answer", "tools": [], "timestamp": ""}],
               "pending_approval": None, "running": None}
    _tab_agents()
    ctx, page = await _chat_page(browser, "uismoke-old-conversation", history, extra_init=HOLD_HISTORY)
    try:
        for _ in range(40):
            if await page.evaluate("() => typeof window.__releaseHistory === 'function'"):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("the page never asked for its conversation's history")
        # A new chat is a new tab since TABS1: opened while the first tab is still reopening.
        await _new_tab(page, "UI smoke Tab Beta")
        await asyncio.sleep(0.3)
        await page.evaluate("() => window.__releaseHistory()")
        await asyncio.sleep(1.0)
        redrawn = "an old question" in await page.evaluate("() => document.querySelector('main').innerText")
        box = page.locator("main textarea").last
        await box.fill("a new question")
        await box.press("Enter")
        await _stream_open(page, "post")
        sent = await page.evaluate("() => window.__sent")
    finally:
        await ctx.close()
    if redrawn:
        raise AssertionError("the old conversation was drawn back over New Chat")
    if not sent or sent[-1].get("session_id") == "uismoke-old-conversation":
        raise AssertionError(f"the message after New Chat went into the old conversation: {sent}")
    return "New Chat holds against a reopen still waiting on its history"


LAYOUT_WIDTHS = """() => {
  const pane = document.querySelector('main .overflow-y-auto');
  const label = document.querySelector('main [data-agent-label]');
  const tag = document.querySelector('main [data-answer-name]');
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return {left: r.left, right: r.right, height: r.height, text: el.textContent.slice(0, 30)}; };
  return {
    pane: pane ? {scroll: pane.scrollWidth, client: pane.clientWidth, right: pane.getBoundingClientRect().right} : null,
    label: box(label), tag: box(tag), bubble: box(tag && tag.closest('[data-answer]')), vw: innerWidth,
  };
}"""


@check
async def check_longcontent(browser) -> str:
    """Long content stays inside the chat: a long token wraps, a long name truncates (F217).

    A 400-character unbroken token (a URL, a path, base64) ran out of its
    message across the avatar and slid the pane sideways; a long agent name was
    spelled out 1,742 px wide in the empty state's letter-spaced label, off
    both edges, and wrapped the answer's name tag over the answer itself.
    """
    name = "Very Long Agent Name " * 10 + "End"
    token = "x" * 400
    status = {**_status(True), "agents": {"agent_long": {"name": name, "status": "online", "can_see_images": True}},
              "active_agent_id": "agent_long"}
    # Both of the page's sources for the name: it takes whichever answers last.
    settings = """
const _settingsFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || 'GET').toUpperCase();
  if (/\\/api\\/settings$/.test(url) && method === 'GET') {
    return Promise.resolve(new Response(JSON.stringify({agent_name: %s}), {
      status: 200, headers: {'Content-Type': 'application/json'},
    }));
  }
  return _settingsFetch(input, init);
};
""" % json.dumps(name)
    # 360px as well as wide: below `sm` an answer had less room beside the
    # avatar than its 288px minimum, and ran out of the pane on any content.
    for width in (WIDE, (360, 780)):
        ctx, page = await _chat_page(browser, "uismoke-long",
                                     extra_init=SERVE_STATUS % json.dumps(status) + settings, width=width)
        try:
            empty = await page.evaluate(LAYOUT_WIDTHS)
            box = page.locator("main textarea").last
            await box.fill("words before the token " + token + " and after")
            await box.press("Enter")
            await _stream_open(page, "post")
            await page.evaluate(
                f"() => {{ window.__streams.post.push({json.dumps(_frame({'type': 'response', 'content': 'Here: ' + token + ' — done.', 'intent': 'chat'}, 'l-1'))});"
                f" window.__streams.post.push({json.dumps(_frame({'type': 'done'}, 'l-2'))}); }}")
            await _on_screen(page, "— done.")
            await asyncio.sleep(0.5)
            answered = await page.evaluate(LAYOUT_WIDTHS)
        finally:
            await ctx.close()
        problems = []
        label = empty["label"]
        # One line, and no wider than its 36rem: the label spanned the whole pane.
        # One line: since M2c the name is the agent card's 24px heading (32px a line; two would be 64).
        if not label or label["left"] < 0 or label["right"] - label["left"] > 600 or label["height"] > 40:
            problems.append(f"the empty state's label spans {label} in a {empty['vw']}px window")
        pane = answered["pane"]
        if not pane or pane["scroll"] > pane["client"] + 1:
            problems.append(f"the pane scrolls sideways: {pane}")
        tag, bubble = answered["tag"], answered["bubble"]
        # One line (the tag inherits the bubble's 28px line height) inside the bubble.
        if not tag or "VERY LONG" not in tag["text"].upper() or tag["height"] > 40 or tag["right"] > bubble["right"] + 1:
            problems.append(f"the answer's name tag is {tag} on a bubble {bubble}")
        if problems:
            raise AssertionError(f"at {width[0]}px: " + "; ".join(problems))
    return "at 1600 and 360px a 400-character token wraps inside its message; a long name stays on one line"



SEND_HIT = """() => {
  const send = document.querySelector('main form button[type="submit"]');
  if (!send) return {send: false};
  const r = send.getBoundingClientRect();
  // Inside the circle (Send is round since M2c): the centre and four points at 70% of the radius.
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2, d = r.width * 0.35;
  const points = [[cx, cy], [cx + d, cy], [cx - d, cy], [cx, cy + d], [cx, cy - d]];
  const covered = points.filter(([x, y]) => {
    const hit = document.elementFromPoint(x, y);
    return !(hit && (hit === send || send.contains(hit)));
  }).map(([x, y]) => {
    const hit = document.elementFromPoint(x, y);
    return (hit && (hit.getAttribute('aria-label') || hit.tagName)) + ' at ' + Math.round(x) + ',' + Math.round(y);
  });
  return {send: true, covered, name: (send.getAttribute('aria-label') || send.innerText || '').trim()};
}"""


@check
async def check_chatsend(browser) -> str:
    """Chat's Send can be tapped at every width, and it has a name (F181).

    The AI builder's launcher owns the bottom-right corner (44px, 20px in, fixed)
    and sat over Send's corner at every width where the composer reaches the
    edge — measured at 360, 400, 640, 768 and 1024px; at phone width a tap on
    Send's centre opened the builder. Below 768px Send was an icon with no
    accessible name.
    """
    bad = []
    for width in ((360, 780), PHONE, (640, 900), (768, 900), (1024, 800), WIDE):
        ctx, page = await _chat_page(browser, "uismoke-send", width=width)
        try:
            await page.locator("main textarea").last.fill("something to send")
            await asyncio.sleep(0.3)
            probe = await page.evaluate(SEND_HIT)
        finally:
            await ctx.close()
        if not probe["send"]:
            bad.append(f"{width[0]}px: no Send button")
            continue
        if probe["covered"]:
            bad.append(f"{width[0]}px: Send is covered ({'; '.join(probe['covered'])})")
        if not probe["name"]:
            bad.append(f"{width[0]}px: Send has no name")
    if bad:
        raise AssertionError(" | ".join(bad))
    return "Send is the thing under every point of it, and named, from 360 to 1600px"


# ── the engine: what a run says (B3) ─────────────────────────────────────────

def failing_orchestration(oid: str, words: str) -> dict:
    """One Transform that raises `words`: a real step failure, with no model."""
    return {
        "id": oid,
        "name": "ui-smoke failing step",
        "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "steps": [{
            "id": f"{oid}_t", "name": "Crunch", "type": "transform",
            "transform_code": f"raise Exception({words!r})",
            "next_step_id": None, "output_key": "out",
        }],
        "entry_step_id": f"{oid}_t",
        "state_schema": {},
        "trigger": "manual",
    }


STEP_REASON = """() => ({
  log: Array.from(document.querySelectorAll('span')).map(s => s.innerText)
    .filter(t => t.startsWith('Step error:')),
  card: Array.from(document.querySelectorAll('.react-flow__node .text-danger')).map(e => e.innerText),
  run: (document.body.innerText.match(/Started run (\\S+)/) || [])[1] || null,
})"""


@check
async def check_stepreason(browser) -> str:
    """A failed step says why, on its card and in the run log (F195).

    The engine sent every step failure to the stream as "An internal error
    occurred while executing this step." and kept the real one in the run
    record, which no screen read: a Transform's own exception, a model's 422
    and an HTTP tool's 404 all looked the same. Checked live and on a run
    reopened from its link, which reads the journal and the record.
    """
    oid = f"orch_uismoke_reason_{int(time.time())}"
    api("/api/orchestrations", "POST", failing_orchestration(oid, "smoke boom"))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run now", exact=True).click()
        await page.get_by_text("Done — status: failed").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        live = await page.evaluate(STEP_REASON)
        if not any("smoke boom" in line for line in live["log"]):
            raise AssertionError(f"the run log does not say why: {live['log']}")
        if live["card"] != ["Exception: smoke boom"]:
            raise AssertionError(f"the failed step's card says {live['card']}")
        if not live["run"]:
            raise AssertionError("the run log never named its run")

        await page.goto(f"{APP}/workflows?run={live['run']}", wait_until="domcontentloaded")
        await page.get_by_text("Done — status: failed").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        again = await page.evaluate(STEP_REASON)
        if not any("smoke boom" in line for line in again["log"]) or again["card"] != ["Exception: smoke boom"]:
            raise AssertionError(f"reopened, the run says {again['log']} and its card {again['card']}")
        return "a failed step's own exception is on its card and in the run log, live and reopened"
    finally:
        await ctx.close()
        drop_fixture(oid)

QUESTION_FRAME = """(sid) => {
  const card = document.querySelector(`.react-flow__node[data-id="${sid}"] > div`);
  return card ? (card.className.match(/border-(person|warning|success|danger|accent|border-strong)/) || [card.className])[0] : null;
}"""


@check
async def check_cancelquestion(browser) -> str:
    """A cancelled run's question is closed: on its step, in the bell, and reopened (F193).

    Cancelling a run parked on a person took the answer strip away (F141) and
    nothing else: the step that asked stayed amber, its "needs your input"
    item stayed open (a restart later called it *answered*), no "cancelled"
    notification came, and the run reopened from its link replayed its journal
    to the question and offered Answer again — `cancelanswer` reloads `?id=`,
    which never reopens the run.
    """
    oid = f"orch_uismoke_cancelq_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    asking = f"{oid}_human"
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run now", exact=True).click()
        await page.get_by_text("Waiting for your input").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        run_id = await page.evaluate("() => (document.body.innerText.match(/Started run (\\S+)/) || [])[1] || null")
        if await page.evaluate(QUESTION_FRAME, asking) != "border-person":
            raise AssertionError("the question never showed as waiting, so this checks nothing")

        await page.get_by_role("button", name="Stop", exact=True).click()
        await asyncio.sleep(2.0)
        if await page.evaluate(QUESTION_FRAME, asking) == "border-person":
            raise AssertionError("the step that asked still shows as waiting after the cancel")
        mine = [n for n in api("/api/notifications")["notifications"] if n.get("run_id") == run_id]
        asked = [n for n in mine if n.get("kind") == "human_input"]
        if not asked or not all(n.get("resolved") and n.get("resolved_as") == "cancelled" for n in asked):
            raise AssertionError(f"the question's notification is {asked}")
        if not any(n.get("title", "").endswith("cancelled") for n in mine):
            raise AssertionError(f"no notification says the run was cancelled: {[n.get('title') for n in mine]}")

        await page.goto(f"{APP}/workflows?run={run_id}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 4.0)
        if await page.get_by_text("Waiting for your input").count():
            raise AssertionError("reopened from its link, the cancelled run asks again")
        if await page.evaluate(QUESTION_FRAME, asking) == "border-person":
            raise AssertionError("reopened, the step that asked shows as waiting")
        return "a cancelled run's question is closed on its step, in the bell (as cancelled) and reopened"
    finally:
        await ctx.close()
        drop_fixture(oid)


def run_to_end(oid: str) -> str:
    """Run a model-free orchestration through the API and return its run id.

    The run route answers with its event stream, which `api` cannot parse; this
    reads it to the end, which for a Print-only flow is the finished run.
    """
    req = urllib.request.Request(
        f"{API}/api/orchestrations/{oid}/run", method="POST", data=b"{}",
        headers={"Content-Type": "application/json", "X-Synapse-Internal": TOKEN},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    found = re.search(r'"run_id": "([^"]+)"', raw)
    if not found:
        raise AssertionError(f"the fixture run never named itself: {raw[:200]}")
    return found.group(1)


def print_orchestration(oid: str, name: str) -> dict:
    """Two Print steps and an End: a run of it costs nothing and finishes at once."""
    return {
        "id": oid, "name": name, "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "trigger": "manual", "state_schema": {}, "entry_step_id": f"{oid}_a",
        "steps": [
            {"id": f"{oid}_a", "name": "First", "type": "print", "print_content": "one",
             "next_step_id": f"{oid}_b", "output_key": "one"},
            {"id": f"{oid}_b", "name": "Second", "type": "print", "print_content": "two",
             "next_step_id": f"{oid}_end", "output_key": "two"},
            {"id": f"{oid}_end", "name": "End", "type": "end", "next_step_id": None},
        ],
    }


@check
async def check_keyrows(browser) -> str:
    """Every row a mouse opens, a keyboard opens too (F219).

    The Agents list, the vault tree, the Memory cards and the Orchestrations
    landing's tables were `div`s and `tr`s with a click handler and nothing
    else: Tab skipped them (or landed on a hidden Delete), and a screen reader
    heard no control. On the canvas a focused node took Enter and selected
    itself, but its settings opened on a mouse click only, so a step could not
    be configured without a mouse.
    """
    stamp = int(time.time())
    aid, oid = f"agent_uismoke_keys_{stamp}", f"orch_uismoke_keys_{stamp}"
    agent_name, orch_name, vault_file = f"Keys {stamp}", f"ui-smoke keys {stamp}", f"uismoke-keys-{stamp}.md"
    api("/api/agents", "POST", _agent(aid, agent_name))
    api("/api/vault/file", "POST", {"name": vault_file, "path": "", "content": "keyboard"})
    api("/api/orchestrations", "POST", print_orchestration(oid, orch_name))
    run_id = run_to_end(oid)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        row = page.get_by_role("button", name=agent_name)
        try:
            await row.first.wait_for(timeout=30_000)
        except Exception:
            raise AssertionError("an agent in the list is not a control a keyboard can reach")
        await asyncio.sleep(SETTLE)
        await row.first.focus()
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.0)
        if await row.first.get_attribute("aria-current") != "true":
            raise AssertionError("Enter on an agent did not open it")

        await page.goto(f"{APP}/vault", wait_until="domcontentloaded")
        item = page.get_by_role("button", name=vault_file, exact=True)
        try:
            await item.wait_for(timeout=30_000)
        except Exception:
            raise AssertionError("a vault file is not a control a keyboard can reach")
        await asyncio.sleep(SETTLE)
        async with page.expect_request(lambda r: "/api/vault/file?" in r.url, timeout=10_000):
            await item.focus()
            await page.keyboard.press("Enter")

        await page.goto(f"{APP}/settings/memory", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        boxes = page.get_by_role("checkbox")
        # Seven since F270 added "Agent Memory".
        if await boxes.count() != 7:
            raise AssertionError(f"Settings → Memory has {await boxes.count()} checkboxes, not its seven cards")
        first = boxes.first
        await first.focus()
        await page.keyboard.press(" ")
        await asyncio.sleep(0.3)
        if await first.get_attribute("aria-checked") != "true":
            raise AssertionError("Space on a Memory card's checkbox did not tick it")

        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        await page.get_by_role("button", name=re.compile(r"^Recent")).click()
        try:
            await page.get_by_role("button", name=f"View run {run_id}").wait_for(timeout=20_000)
        except Exception:
            raise AssertionError("a run in the Recent table is not a control a keyboard can reach")
        await page.get_by_role("button", name=re.compile(r"^Workflows")).click()
        opener = page.get_by_role("button", name=f"Open {orch_name}")
        try:
            await opener.wait_for(timeout=20_000)
        except Exception:
            raise AssertionError("an orchestration in the list is not a control a keyboard can reach")
        await opener.focus()
        await page.keyboard.press("Enter")
        node = page.locator(f'.react-flow__node[data-id="{oid}_b"]')
        await node.wait_for(timeout=30_000)
        await asyncio.sleep(1.0)
        if await node.get_attribute("aria-label") != "Second":
            raise AssertionError(f"a focused step is announced as {await node.get_attribute('aria-label')!r}")
        await node.focus()
        await page.keyboard.press("Enter")
        panel = page.locator('aside[aria-label="Step configuration"]')
        try:
            await panel.wait_for(timeout=5_000)
        except Exception:
            raise AssertionError("Enter on a focused step selects it but does not open its settings")
        await node.focus()
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        if await panel.count():
            raise AssertionError("Escape on a focused step leaves its settings open")
        return "agents, vault files, Memory cards, runs, orchestrations and canvas steps open from the keyboard"
    finally:
        await ctx.close()
        drop_fixture(oid)
        for path, body in ((f"/api/agents/{aid}", None), ("/api/vault/item", {"path": vault_file})):
            try:
                api(path, "DELETE", body)
            except Exception:
                pass


@check
async def check_agenttools(browser) -> str:
    """An agent's tools are controls a keyboard can toggle, and "all" can be narrowed (F184, F235).

    The Capabilities cards were `div`s with a click handler: no role, no state,
    out of the tab order, and nothing else in the product gives an agent tools.
    Beside that, with the agent on "all" a group's box could never be turned
    off: it re-added the group's names next to "all". Since M2d a group of one
    tool (Time & Date) is a switch; a group of several keeps its checkbox.
    """
    aid = f"agent_uismoke_tools_{int(time.time())}"
    name = f"Tools {aid[-6:]}"
    api("/api/agents", "POST", {**_agent(aid, name), "tools": ["all"]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await page.get_by_text(name, exact=True).first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_text(name, exact=True).first.click()
        await page.wait_for_function(
            "(n) => [...document.querySelectorAll('input')].some(i => i.value === n)", arg=name, timeout=20_000)
        await asyncio.sleep(1.0)
        box = page.get_by_role("switch", name="Time & Date", exact=True)
        if not await box.count():
            raise AssertionError("a one-tool group (Time & Date) is not a switch")
        if not await page.locator('section[aria-labelledby="setup-tools"]').get_by_role("checkbox").count():
            raise AssertionError("no group of several tools has its checkbox")
        if await box.get_attribute("aria-checked") != "true":
            raise AssertionError("on an agent with every tool, Time & Date does not read as ticked")
        await box.focus()
        await page.keyboard.press(" ")
        await asyncio.sleep(0.4)
        if await box.get_attribute("aria-checked") != "false":
            raise AssertionError("on an agent with every tool, Space does not turn a group off")
        await page.keyboard.press(" ")
        await asyncio.sleep(0.4)
        if await box.get_attribute("aria-checked") != "true":
            raise AssertionError("Space does not turn the group back on")
        expander = page.locator("button[aria-expanded]", has_text="/").first
        if not await expander.count():
            raise AssertionError("a group of several tools has no control that opens it")
        await expander.focus()
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)
        if await expander.get_attribute("aria-expanded") != "true":
            raise AssertionError("Enter on a group's expander does not open it")
        return "tool groups (checkboxes) and tools (switches) toggle from the keyboard, and \"all\" narrows"
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass


@check
async def check_logrows(browser) -> str:
    """A log opens from the keyboard, and its Delete is not the only stop (F191).

    Every log row was a `div` with a click handler; the one focusable thing in
    it was its Delete button, invisible until hovered. Tab landed on Delete, and
    a log could be deleted from the keyboard but never read.
    """
    oid = f"orch_uismoke_logs_{int(time.time())}"
    name = f"ui-smoke logs {oid[-10:]}"
    api("/api/orchestrations", "POST", print_orchestration(oid, name))
    run_id = run_to_end(oid)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/logs", wait_until="domcontentloaded")
        tab = page.get_by_role("button", name=re.compile("^Workflow logs"))
        await tab.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await tab.click()
        row = page.get_by_role("button", name=re.compile("^" + re.escape(name)))
        try:
            await row.wait_for(timeout=20_000)
        except Exception:
            raise AssertionError("a log in the list is not a control a keyboard can reach")
        await row.focus()
        async with page.expect_request(lambda r: f"/api/logs/orchestrations/{run_id}" in r.url, timeout=10_000):
            await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)
        if await row.get_attribute("aria-current") != "true":
            raise AssertionError("Enter on a log fetched it but did not mark it open")
        await page.keyboard.press("Tab")
        after = await page.evaluate(
            "() => ({label: document.activeElement.getAttribute('aria-label'),"
            " opacity: getComputedStyle(document.activeElement).opacity})")
        if after["label"] != f"Delete log {name}" or after["opacity"] != "1":
            raise AssertionError(f"the next stop after an open log is {after}")
        return "a log opens on Enter, and its Delete is named and visible when reached"
    finally:
        await ctx.close()
        drop_fixture(oid)
        try:
            api(f"/api/logs/orchestrations/{run_id}", "DELETE")
        except Exception:
            pass


#: What a keyboard or a screen reader can reach inside the drawer titled `title`.
DRAWER_REACH = """(title) => {
  const h = [...document.querySelectorAll('h2')].find(x => x.textContent.trim() === title);
  if (!h) return null;
  const d = h.closest('.fixed');
  const reach = [...d.querySelectorAll('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter(e => !e.closest('[inert]') && !e.closest('[aria-hidden="true"]'));
  return {reach: reach.length,
          unnamed: reach.filter(b => b.tagName === 'BUTTON' && !b.getAttribute('aria-label') && !b.textContent.trim()).length};
}"""


@check
async def check_drawers(browser) -> str:
    """A closed drawer is out of reach, and an open one's close button has a name (F179).

    The Scale deployment guide slides off-screen when closed but stayed in the page: 10 controls a keyboard
    tabbed into and a screen reader read while nothing was visible. Settings → API Keys had the same drawer (the
    API reference); it is gone since F906 (POL1, Naveen: "link to documentation"), so that page must carry no
    drawer at all — nothing hidden in reach.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/api_keys", wait_until="domcontentloaded")
        await page.get_by_role("link", name="API reference").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        if await page.evaluate(DRAWER_REACH, "API reference") is not None:
            raise AssertionError("/settings/api_keys: an API reference drawer is back in the page (F906 links to the docs)")
        for path, title, opener in (
            ("/settings/scale", "Scale deployment guide", re.compile("deploy docs", re.I)),
        ):
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await page.get_by_role("button", name=opener).first.wait_for(timeout=30_000)
            await asyncio.sleep(SETTLE)
            closed = await page.evaluate(DRAWER_REACH, title)
            if closed is None:
                raise AssertionError(f"{path}: no drawer titled {title!r}, so this checks nothing")
            if closed["reach"]:
                raise AssertionError(f"{path}: the closed {title} still has {closed['reach']} controls in reach")
            await page.get_by_role("button", name=opener).first.click()
            await asyncio.sleep(0.8)
            opened = await page.evaluate(DRAWER_REACH, title)
            if not opened["reach"]:
                raise AssertionError(f"{path}: the open {title} has nothing in reach")
            if opened["unnamed"]:
                raise AssertionError(f"{path}: the open {title} has {opened['unnamed']} unnamed button(s)")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.8)
            if (await page.evaluate(DRAWER_REACH, title))["reach"]:
                raise AssertionError(f"{path}: closed with Escape, the {title} is back in reach")
        return "the Scale guide's closed drawer is out of reach and its open one has named buttons; API Keys has no drawer"
    finally:
        await ctx.close()


@check
async def check_knowledge_drawer(browser) -> str:
    """Knowledge's report drawer (D157) is a dialog with named controls that Escape closes, and a
    closed one is not in the page at all — so nothing in it is in reach."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_ROUTES:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        opener = page.get_by_role("button", name="Report for failed.md")
        await opener.wait_for(timeout=30_000)
        if await page.get_by_role("dialog").count():
            raise AssertionError("a drawer is open before anything opened it")
        await opener.click()
        dialog = page.get_by_role("dialog", name="failed.md")
        await dialog.wait_for(timeout=10_000)
        unnamed = await dialog.evaluate("""d => [...d.querySelectorAll('button')]
            .filter(b => !b.getAttribute('aria-label') && !b.textContent.trim()).length""")
        if unnamed:
            raise AssertionError(f"the open report drawer has {unnamed} unnamed button(s)")
        if not await dialog.get_by_role("button", name="Retry").count():
            raise AssertionError("the report drawer offers no Retry")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
        if await page.get_by_role("dialog").count():
            raise AssertionError("Escape did not close the report drawer")
        return "the report drawer is a named dialog, Escape closes it, and closed it is gone"
    finally:
        await ctx.close()


@check
async def check_knowledge_recrawl(browser) -> str:
    """Add web pages says when its pages are fetched again from the base's own `recrawl_days`, as the Web sources
    section beside it does (F370). It said "every week" whatever the base said."""
    for days, expected in ((3, "fetched again every 3 days"), (1, "fetched again every 1 day"),
                           (0, "fetched again only when you ask")):
        kb = {**_SMOKE_KB, "web": {**_SMOKE_KB["web"], "recrawl_days": days}}
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        try:
            for pattern, handler in _SMOKE_KB_ROUTES + [("**/api/knowledge/bases", _answer([kb]))]:
                await ctx.route(pattern, handler)
            page = await ctx.new_page()
            await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
            await page.get_by_role("button", name="Add web pages").click(timeout=30_000)
            dialog = page.get_by_role("dialog", name="Add web pages")
            await dialog.wait_for(timeout=10_000)
            said = " ".join((await dialog.inner_text()).split())
            if expected not in said or "every week" in said:
                raise AssertionError(f"recrawl_days {days}: the dialog says {said[:160]!r}, not {expected!r}")
        finally:
            await ctx.close()
    return "Add web pages says the base's own re-crawl, for 3, 1 and 0 days"


@check
async def check_knowledge_fusion_word(browser) -> str:
    """The fusion weight has one name on every screen that shows it, Meaning (F371): the settings drawer said
    "Meaning weight", Evaluate's Try weights "Vector weight" and its rankings "Vector only"."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_EVAL_ROUTES:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile("^Evaluate")).wait_for(timeout=30_000)
        await _kb_eval(page)
        await page.get_by_text(re.compile(r"^Each question")).first.click()
        await page.get_by_role("button", name=re.compile(r"^Try weights")).wait_for(timeout=10_000)
        said = await page.locator("main").inner_text()
        if "Meaning weight" not in said or re.search(r"\bVector\b", said):
            found = sorted(set(re.findall(r"[^\n]*\bVector\b[^\n]*", said)))[:4]
            raise AssertionError(f"Evaluate still says Vector: {found}")
        return "Evaluate names the weight Meaning, in Try weights, the rankings and each question's ranks"
    finally:
        await ctx.close()


@check
async def check_knowledge_retrieve_words(browser) -> str:
    """The run log's Retrieve row says what a search did without in words, as its opened trace does (F372); it
    printed the code, `vector_unavailable`."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_RUN_ROUTES:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?run=run_smoke_kb", wait_until="domcontentloaded")
        row = page.get_by_text(re.compile(r"^Retrieve · Find clauses")).first
        await row.wait_for(timeout=30_000)
        said = " ".join((await row.inner_text()).split())
        if "vector_unavailable" in said or "Matched by words only" not in said:
            raise AssertionError(f"the Retrieve row says {said!r}")
        return "the Retrieve row says its degraded search in words"
    finally:
        await ctx.close()


@check
async def check_knowledge_dropzone(browser) -> str:
    """The drop zone says what this install reads: no OCR here, so scanned pages and images are not read (KP1). It
    said they are read "once OCR is configured", and nothing in OSS configures one. In plain words (GUIDE1, F921):
    no "OCR" in it at all."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_ROUTES:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        zone = page.get_by_text(re.compile(r"^Drop files here")).first
        await zone.wait_for(timeout=30_000)
        said = " ".join((await zone.inner_text()).split())
        if "OCR" in said or "Scanned pages and images aren't read: this install can't read text in pictures." not in said:
            raise AssertionError(f"the drop zone says {said!r}")
        return "the drop zone says this install reads no scanned pages or images"
    finally:
        await ctx.close()


@check
async def check_knowledge_strip(browser) -> str:
    """A base opens on its numbers (M2e): documents, this month against its budget, and how often its served index
    finds the answer from the last evaluation; "Not measured" when no question set has run."""
    async def strip(routes):
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        for pattern, handler in routes:
            await ctx.route(pattern, handler)
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
            box = page.locator("dl[aria-label='Smoke contracts at a glance']")
            await box.wait_for(timeout=30_000)
            await page.wait_for_timeout(1500)       # Evaluate reads its sets, then hands the score up
            return " ".join((await box.inner_text()).split())
        finally:
            await ctx.close()

    measured = await strip(_SMOKE_KB_ROUTES)
    for want in ("Documents 2", "1 failed", "This month $1.25", "of a $5.00 budget", "Finds the answer 100%"):
        if want not in measured:
            raise AssertionError(f"the strip says {measured!r}, not {want!r}")
    unmeasured = await strip(_SMOKE_KB_ROUTES + [("**/api/knowledge/bases/*/eval/sets", _answer([]))])
    if "Finds the answer — Not measured" not in unmeasured:
        raise AssertionError(f"with no question set the strip says {unmeasured!r}")
    return "documents, this month of its budget, and 100% found (Not measured with no set)"


@check
async def check_knowledge_sources(browser) -> str:
    """The documents are grouped by where they came from (M2e): pills for files and web pages, each with its count,
    and a pill shows only the documents of its kind; the status filter stays beside them."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_ROUTES:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        pills = page.get_by_role("group", name="Show documents")
        await pills.wait_for(timeout=30_000)
        said = " ".join((await pills.inner_text()).split())
        if said != "All 9 Files 8 Web pages 1":
            raise AssertionError(f"the pills say {said!r}")
        await page.get_by_role("combobox", name="Filter by status").wait_for(timeout=5_000)
        await pills.get_by_role("button", name=re.compile("^Web pages")).click()
        reports = page.get_by_role("button", name=re.compile("^Report for "))
        await page.wait_for_function("() => document.querySelectorAll('button[aria-label^=\"Report for \"]').length === 1",
                                     timeout=5_000)
        only = await reports.first.get_attribute("aria-label")
        if "install.md" not in (only or ""):
            raise AssertionError(f"Web pages shows {only!r}")
        if await pills.get_by_role("button", name=re.compile("^Web pages")).get_attribute("aria-pressed") != "true":
            raise AssertionError("the Web pages pill is not marked pressed")
        return "All 9 · Files 8 · Web pages 1; Web pages shows the one fetched page"
    finally:
        await ctx.close()


@check
async def check_knowledge_first(browser) -> str:
    """The detail never opens empty (UX1): the first base opens by itself, its test search is ready without a click,
    and the page fits the window on a laptop and a phone."""
    seen = []
    for width, height in (WIDE, PHONE):
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        for pattern, handler in _SMOKE_KB_ROUTES:
            await ctx.route(pattern, handler)
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
            await page.get_by_role("heading", name="Smoke contracts", level=2).wait_for(timeout=30_000)
            if not await page.get_by_role("searchbox", name="Search Smoke contracts").is_visible():
                raise AssertionError(f"at {width}px the test search is not open")
            wide = await page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            if wide > 1:
                raise AssertionError(f"at {width}px the page scrolls {wide}px sideways")
            seen.append(str(width))
        finally:
            await ctx.close()
    return f"the first base opens with its search ready at {' and '.join(seen)}px, no sideways scroll"


@check
async def check_knowledge_eval(browser) -> str:
    """A knowledge base's eval (V1, D168): the Run dialog is named, every control in it is named, it prices the
    run before anything starts, and Escape closes it without starting one."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    started = []
    for pattern, handler in _SMOKE_KB_EVAL_ROUTES:
        await ctx.route(pattern, handler)

    async def record_start(route):
        if route.request.method == "POST":
            started.append(route.request.post_data)
        await route.fallback()
    await ctx.route("**/api/knowledge/bases/*/eval/runs", record_start)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile("^Evaluate")).wait_for(timeout=30_000)
        await _kb_eval(page)
        await page.get_by_role("button", name="Run", exact=True).click()
        dialog = page.get_by_role("dialog", name="Run Smoke questions")
        await dialog.wait_for(timeout=10_000)
        unnamed = await dialog.evaluate("""d => [...d.querySelectorAll('button, input, [role=combobox], [role=checkbox]')]
            .filter(e => e.type !== 'hidden' && !e.getAttribute('aria-label') && !e.labels?.length
                         && !e.getAttribute('aria-labelledby') && !e.textContent.trim()).length""")
        if unnamed:
            raise AssertionError(f"the Run dialog has {unnamed} unnamed control(s)")
        await dialog.get_by_text(re.compile(r"^About \$")).wait_for(timeout=10_000)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
        if await page.get_by_role("dialog").count():
            raise AssertionError("Escape did not close the Run dialog")
        if started:
            raise AssertionError(f"closing the dialog started a run: {started}")
        return "the Run dialog is named throughout, prices the run, and Escape closes it without starting one"
    finally:
        await ctx.close()


@check
async def check_knowledge_settings(browser) -> str:
    """A knowledge base's settings drawer (B2b, D167) is a named dialog whose every control is named; nothing
    is saved before something changes; a change to how the index is built is priced before it is saved; and
    Escape closes it without a save."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    patched = []
    for pattern, handler in _SMOKE_KB_ROUTES:
        await ctx.route(pattern, handler)

    async def record_patch(route):
        if route.request.method == "PATCH":
            patched.append(route.request.post_data)
        await route.fallback()
    await ctx.route("**/api/knowledge/bases/*", record_patch)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Settings", exact=True).wait_for(timeout=30_000)
        await _kb_settings_rebuild(page)
        dialog = page.get_by_role("dialog", name="Settings for Smoke contracts")
        await dialog.wait_for(timeout=10_000)
        unnamed = await dialog.evaluate("""d => [...d.querySelectorAll('button, input, [role=combobox]')]
            .filter(e => e.type !== 'hidden' && !e.getAttribute('aria-label') && !e.labels?.length
                         && !e.getAttribute('aria-labelledby') && !e.textContent.trim()).length""")
        if unnamed:
            raise AssertionError(f"the settings drawer has {unnamed} unnamed control(s)")
        await dialog.get_by_text("Saving rebuilds the index").wait_for(timeout=10_000)
        if not await dialog.get_by_role("button", name="Save and rebuild").is_enabled():
            raise AssertionError("a priced index change cannot be saved")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
        if await page.get_by_role("dialog").count():
            raise AssertionError("Escape did not close the settings drawer")
        if patched:
            raise AssertionError(f"closing the drawer saved something: {patched}")
        return "the settings drawer is named throughout, prices an index change, and Escape closes it unsaved"
    finally:
        await ctx.close()


@check
async def check_knowledge_switch(browser) -> str:
    """Knowledge's switch (F367): it ships off, and Settings → General is where a person turns it on. The switch is
    named, reads the saved setting, is saved by the page's Save, and the rail shows Knowledge at once, with no
    reload. Runs against the real backend, whose setting is put back as it was found."""
    found = bool(api("/api/settings").get("knowledge_enabled"))
    if found:
        api("/api/settings", "POST", {"knowledge_enabled": False})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    posted: list[dict] = []

    async def record(route):
        if route.request.method == "POST":
            posted.append(json.loads(route.request.post_data or "{}"))
        await route.fallback()
    await ctx.route("**/api/settings", record)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/general", wait_until="domcontentloaded")
        switch = page.get_by_role("switch", name="Knowledge", exact=True)
        await switch.wait_for(timeout=30_000)
        await page.wait_for_function(
            "() => { const s = document.querySelector('[role=switch][aria-label=Knowledge]'); return s && !s.disabled; }",
            timeout=30_000)
        if await switch.get_attribute("aria-checked") != "false":
            raise AssertionError("the switch does not read the saved setting (off)")
        rail = page.locator('nav[aria-label="Primary"] a[href="/knowledge"]')
        if await rail.count():
            raise AssertionError("the rail shows Knowledge while it is off")
        await switch.click()
        await page.get_by_role("button", name="Save changes", exact=True).click()
        await rail.first.wait_for(timeout=15_000)
        if not any(p.get("knowledge_enabled") is True for p in posted):
            raise AssertionError(f"the save did not send knowledge_enabled: true ({posted})")
        if api("/api/settings").get("knowledge_enabled") is not True:
            raise AssertionError("the backend did not keep the switch")
        return "Settings → General switches Knowledge on with its Save, and the rail shows it without a reload"
    finally:
        await ctx.close()
        api("/api/settings", "POST", {"knowledge_enabled": found})


@check
async def check_knowledge_contextual(browser) -> str:
    """Contextual Retrieval in a knowledge base's settings (CR1, D180): choosing it shows a named Context model
    picker that starts on the workspace's default, the rebuild is priced with a "Writing contexts" line before
    anything is saved, a picked model reaches that price, and going back to the saved mode leaves nothing to save."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    priced = []
    for pattern, handler in _SMOKE_KB_CONTEXTUAL_ROUTES:
        await ctx.route(pattern, handler)

    async def record_preview(route):
        if route.request.method == "POST":
            priced.append(json.loads(route.request.post_data or "{}"))
        await route.fallback()
    await ctx.route("**/api/knowledge/bases/*/cost-preview", record_preview)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Settings", exact=True).wait_for(timeout=30_000)
        await _kb_settings_contextual(page)
        dialog = page.get_by_role("dialog", name="Settings for Smoke contracts")
        picker = dialog.get_by_role("button", name="Context model", exact=True)
        if "Workspace default (gpt-4o-mini)" not in (await picker.inner_text()):
            raise AssertionError(f"the Context model picker does not start on the workspace default: "
                                 f"{await picker.inner_text()!r}")
        def asked(body: dict) -> dict:
            return (body.get("overrides") or {}).get("enrichment") or {}

        if not priced or asked(priced[-1]).get("mode") != "contextual":
            raise AssertionError(f"choosing Contextual Retrieval was not priced: {priced}")
        await picker.click()
        await page.get_by_role("option", name="gpt-4.1-nano", exact=True).click()
        for _ in range(40):
            if asked(priced[-1]).get("contextual_model") == "gpt-4.1-nano":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"the picked model never reached the price: {asked(priced[-1])}")
        await dialog.get_by_role("combobox", name="Context for each passage", exact=True).click()
        await page.get_by_role("option", name="Title, date and section (free)").click()
        if await dialog.get_by_role("button", name="Context model", exact=True).count():
            raise AssertionError("the Context model picker stayed after going back to the free prefix")
        if await dialog.get_by_role("button", name=re.compile("^Save")).is_enabled():
            raise AssertionError("going back to the saved mode left a change to save (a rebuild for nothing)")
        return ("Contextual Retrieval shows a named Context model picker on the workspace default, is priced before "
                "Save, prices a picked model, and going back leaves nothing to save")
    finally:
        await ctx.close()


@check
async def check_knowledge_upload(browser) -> str:
    """An upload into a knowledge base (D175) speaks every `UploadSession` shape: each part is PUT with exactly
    its `headers` and its share of the bytes, its ETag is read from the answer's `ETag` header — an object store
    answers a part with that and an empty body, which a JSON-only reader fails on — and complete gets the ETags
    back as they came, quotes included. While a part is on its way the upload has a named bar (PG1). An upload
    whose part fails is cancelled — a DELETE of its session, never a complete (F341)."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_ROUTES:
        await ctx.route(pattern, handler)
    puts: list[tuple[str, dict, int]] = []
    completed: list[dict] = []
    cancelled: list[str] = []
    sessions = [{"upload_id": upload, "part_size": 5, "expires_at": _NOW, "parts": [
        {"part_number": n, "url": f"/smoke-parts/{upload}/{n}", "method": "PUT", "headers": {"X-Smoke-Part": str(n)}}
        for n in (1, 2)]} for upload in ("kup_smoke", "kup_broken")]

    async def start(route):
        if route.request.method != "POST":
            await route.fallback()
            return
        await route.fulfill(status=201, content_type="application/json", body=json.dumps(sessions.pop(0)))

    async def put_part(route):
        req = route.request
        upload, n = req.url.rsplit("/", 2)[-2:]
        if upload == "kup_broken" and n == "2":
            await route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "disk full"}))
            return
        puts.append((n, await req.all_headers(), len(req.post_data_buffer or b"")))
        if n == "2":
            await asyncio.sleep(1.5)  # a slow last part: the bar is on the page meanwhile
        await route.fulfill(status=200, headers={"ETag": f'"etag-{n}"'}, body="")

    async def complete(route):
        completed.append(json.loads(route.request.post_data or "{}"))
        await route.fulfill(status=202, content_type="application/json", body=json.dumps({"documents": []}))

    async def cancel(route):
        if route.request.method != "DELETE":
            await route.fallback()
            return
        cancelled.append(route.request.url.rsplit("/", 1)[-1])
        await route.fulfill(status=204, body="")
    await ctx.route("**/api/knowledge/bases/*/uploads", start)
    await ctx.route("**/smoke-parts/*/*", put_part)
    await ctx.route("**/api/knowledge/uploads/*/complete", complete)
    await ctx.route(re.compile(r".*/api/knowledge/uploads/[^/]+$"), cancel)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Upload files").wait_for(timeout=30_000)
        await page.locator("input[type=file]").set_input_files(
            {"name": "smoke.md", "mimeType": "text/markdown", "buffer": b"# Hi\nyo"})
        bar = page.get_by_role("list", name="Uploads").get_by_role("progressbar", name="Uploading smoke.md")
        await bar.wait_for(timeout=10_000)
        for _ in range(50):
            if completed:
                break
            await asyncio.sleep(0.2)
        uploads = page.get_by_role("list", name="Uploads")
        on_page = (await uploads.inner_text()).strip() if await uploads.count() else ""
        if not completed:
            raise AssertionError(f"the upload never completed; parts PUT: {puts}; on the page: {on_page!r}")
        sizes = [size for _, _, size in puts]
        if sizes != [5, 2]:
            raise AssertionError(f"parts were PUT with {sizes} bytes, not [5, 2]")
        for n, headers, _ in puts:
            if headers.get("x-smoke-part") != n:
                raise AssertionError(f"part {n} was PUT without its own headers: {headers}")
            if "authorization" in headers:
                raise AssertionError(f"part {n} was PUT with an Authorization header")
        want = [{"part_number": 1, "etag": '"etag-1"'}, {"part_number": 2, "etag": '"etag-2"'}]
        if completed[0].get("parts") != want:
            raise AssertionError(f"complete was sent {completed[0]}, not the ETags as they came")
        await page.locator("input[type=file]").set_input_files(
            {"name": "broken.md", "mimeType": "text/markdown", "buffer": b"# Oh\nno"})
        await page.get_by_role("list", name="Uploads").get_by_text("disk full").wait_for(timeout=10_000)
        for _ in range(25):
            if cancelled:
                break
            await asyncio.sleep(0.2)
        if cancelled != ["kup_broken"] or len(completed) != 1:
            raise AssertionError(f"a failed part cancelled {cancelled} and completed {len(completed)} upload(s); "
                                 "wanted kup_broken cancelled and nothing more completed")
        return ("parts PUT with their own headers and sizes, ETags read from the header, complete sent them as they "
                "came; a named bar while a part was on its way; a failed part cancelled its upload")
    finally:
        await ctx.close()


@check
async def check_promptwriter(browser) -> str:
    """The AI Prompt Writer says why it wrote nothing (F183).

    A default model the provider refused made `generate-prompt` answer 500, and
    the agent editor showed nothing — only a console line — with the prompt left
    as it was. The route's refusal is stubbed here, as the backend now sends it
    (502, `detail` holding the provider's words), so the check needs no model.
    """
    aid = f"agent_uismoke_writer_{int(time.time())}"
    name = f"Writer {aid[-6:]}"
    said = "The model couldn't write the prompt: this model is no longer available to new users"
    api("/api/agents", "POST", _agent(aid, name))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    await page.route("**/api/agents/generate-prompt", lambda route: route.fulfill(
        status=502, content_type="application/json", body=json.dumps({"detail": said})))
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await page.get_by_text(name, exact=True).first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_text(name, exact=True).first.click()
        await page.wait_for_function(
            "(n) => [...document.querySelectorAll('input')].some(i => i.value === n)", arg=name, timeout=20_000)
        await page.locator("#agn-prompt-description").fill("a helper for the smoke test")
        await page.get_by_role("button", name=re.compile("generate", re.I)).click()
        note = page.get_by_role("alert").filter(has_text="no longer available to new users")
        try:
            await note.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("a refused Prompt Writer call shows nothing on the page")
        if not await note.get_by_role("link", name=re.compile("Settings → Models")).count():
            raise AssertionError("the Prompt Writer's failure does not point to Settings → Models")
        return "a refused Prompt Writer call says why under its field and points to Settings → Models"
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass


@check
async def check_streamdocs(browser) -> str:
    """The API page links to the docs and says where to call; it never shows a stream example the API refuses.

    F199: V2's React example opened `new EventSource(…/stream?token=YOUR_API_KEY)`, which the API refuses (the key
    is read only from the Authorization header). F906 (POL1, Naveen: "under api lets not have a documentation …
    lets have a link to documentation") moved every example to the docs site (DOC5 keeps the stream examples
    there); the page keeps the key, the v1 and v2 base URLs, how to send a key, and a link to the API reference
    that opens in a new tab.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/api_keys", wait_until="domcontentloaded")
        ref = page.locator("main a", has_text="API reference").first
        await ref.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        if await page.get_by_role("button", name=re.compile("view docs", re.I)).count():
            raise AssertionError("the in-page docs drawer is back (F906 links to the docs site)")
        href, target, rel = (await ref.get_attribute("href"), await ref.get_attribute("target"),
                             await ref.get_attribute("rel"))
        if href != "https://docs.synapseorch.com/api/overview" or target != "_blank" or "noopener" not in (rel or ""):
            raise AssertionError(f"the API reference link is {href!r} target={target!r} rel={rel!r}")
        mcp = await page.locator("main a", has_text="MCP server guide").first.get_attribute("href")
        if mcp != "https://docs.synapseorch.com/api/mcp-server":
            raise AssertionError(f"the MCP guide link is {mcp!r}")
        text = await page.locator("main").inner_text()
        origin = APP.rstrip("/")
        for need in (f"{origin}/api/v1", f"{origin}/api/v2", "Authorization: Bearer", "Generate"):
            if need not in text:
                raise AssertionError(f"the API page does not say {need!r}")
        if "new EventSource(" in text or "?token=" in text:
            raise AssertionError("the API page shows a stream the API refuses (EventSource / ?token=)")
        return "the API page says the base URLs and how to send a key, and links the reference in a new tab"
    finally:
        await ctx.close()


#: Each toolbar control's box and whether a tap at its centre reaches it. A
#: disabled control lets the hit through, so it is only checked for place.
TOOLBAR_REACH = """(names) => {
  const vw = document.documentElement.clientWidth;
  const out = {};
  for (const name of names) {
    const b = [...document.querySelectorAll('main button')]
      .find(x => (x.getAttribute('aria-label') || x.textContent.trim()) === name);
    if (!b) { out[name] = 'missing'; continue; }
    const r = b.getBoundingClientRect();
    if (!r.width) { out[name] = 'hidden'; continue; }
    if (r.right > vw + 1 || r.left < 0) { out[name] = 'off-screen'; continue; }
    if (b.disabled) { out[name] = 'ok'; continue; }
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    out[name] = hit && (hit === b || b.contains(hit)) ? 'ok'
      : 'covered by ' + ((hit && hit.closest('button') && (hit.closest('button').getAttribute('aria-label') || hit.closest('button').textContent.trim())) || (hit && hit.tagName));
  }
  return out;
}"""


@check
async def check_phonetoolbar(browser) -> str:
    """At phone width every editor action can be tapped (F218).

    At 360 px the toolbar sat in one clipped row: Deploy as Agent, Duplicate
    and Delete were past the edge, and the two halves overlapped, so a tap on
    New or Build with AI landed on Save or Run. On the open canvas (M3a) the
    bar keeps back, the name, the switcher, Save, Run now, help and ⋯; the rest
    is in ⋯; the dock keeps Add step and Undo, and Fit stays at the corner.
    There is no palette to cover the canvas: steps come from Add step.
    """
    oid = f"orch_uismoke_phone_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, "ui-smoke phone toolbar"))
    ctx = await browser.new_context(viewport={"width": 360, "height": 780})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        names = ["Back", "Workflow", "Save", "Run now", "More actions", "Add step", "Undo (Ctrl+Z)",
                 "Fit view"]
        reach = await page.evaluate(TOOLBAR_REACH, names)
        bad = {k: v for k, v in reach.items() if v != "ok"}
        if bad:
            raise AssertionError(f"at 360 px these editor controls cannot be tapped: {bad}")
        await page.get_by_role("button", name="More actions").click()
        items = await page.get_by_role("menuitem").all_inner_texts()
        # Last run is in ⋯ on a phone since M3a-2 (D204's bar; the toggle reads "Show last run" until pressed).
        want = ["Workflow settings", "Change with AI", "Show last run", "New workflow", "Deploy as Agent",
                "Duplicate", "Delete"]
        if [i.strip() for i in items] != want:
            raise AssertionError(f"the ⋯ menu holds {items}, not {want}")
        await page.keyboard.press("Escape")
        return "at 360 px every editor action can be tapped, and the rest are behind ⋯"
    finally:
        await ctx.close()
        drop_fixture(oid)


@check
async def check_duplicateurl(browser) -> str:
    """After Duplicate the address names the copy, so a reload reopens it (F198).

    Duplicate opened the copy in the editor and left `?id=<original>`: a reload
    or a shared link reopened the original, and the person carried on editing
    the wrong one.
    """
    oid = f"orch_uismoke_dup_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke dup {oid[-6:]}"))
    copy_id = None
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        async with page.expect_response(
            lambda r: r.url.endswith("/api/orchestrations") and r.request.method == "POST", timeout=20_000,
        ) as made:
            # In the open canvas's ⋯ (M3a), with Deploy as Agent and Delete.
            await page.get_by_role("button", name="More actions", exact=True).click()
            await page.get_by_role("menuitem", name="Duplicate", exact=True).click()
        copy_id = (await (await made.value).json())["id"]
        await asyncio.sleep(1.0)
        if f"id={copy_id}" not in page.url:
            raise AssertionError(f"after Duplicate the address is {page.url}, not the copy {copy_id}")
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        shown = await page.get_by_role("button", name="Workflow", exact=True).inner_text()
        if "(Copy)" not in shown:
            raise AssertionError(f"reloaded after Duplicate, the editor shows {shown!r}, not the copy")
        return "after Duplicate the address names the copy, and a reload reopens it"
    finally:
        await ctx.close()
        drop_fixture(oid)
        if copy_id:
            drop_fixture(copy_id)


#: Every canvas node's frame: its status colour, and whether it pulses (running).
NODE_FRAMES = """() => Object.fromEntries([...document.querySelectorAll('.react-flow__node')].map(n => {
  const cls = (n.querySelector(':scope > div') || {}).className || '';
  return [n.getAttribute('data-id'),
          (cls.match(/border-(person|warning|success|danger|accent|border-strong)/) || ['?'])[0]
          + (/animate-pulse/.test(cls) ? '+pulse' : '')];
}))"""


@check
async def check_resumesettle(browser) -> str:
    """A resumed run that completes leaves no step pulsing, live or reopened (F190).

    Run → a person answers → Print → End → "Done — status: completed", and the
    step that asked and the End kept the running pulse for as long as the page
    stayed open: the engine sends no step_complete for either. Reopened from
    its link, the step that asked turned grey and End still pulsed.
    """
    oid = f"orch_uismoke_settle_{int(time.time())}"
    steps = [
        {"id": f"{oid}_a", "name": "Research", "type": "print", "print_content": "researching",
         "next_step_id": f"{oid}_ask", "output_key": "research"},
        {"id": f"{oid}_ask", "name": "Review", "type": "human", "human_prompt": "Approve?",
         "next_step_id": f"{oid}_b", "output_key": "approval"},
        {"id": f"{oid}_b", "name": "Print", "type": "print", "print_content": "approved",
         "next_step_id": f"{oid}_end", "output_key": "out"},
        {"id": f"{oid}_end", "name": "End", "type": "end", "next_step_id": None},
    ]
    api("/api/orchestrations", "POST", {
        "id": oid, "name": "ui-smoke settle", "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "trigger": "manual", "state_schema": {}, "entry_step_id": steps[0]["id"], "steps": steps})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run now", exact=True).click()
        await page.get_by_text("Waiting for your input").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        run_id = await page.evaluate("() => (document.body.innerText.match(/Started run (\\S+)/) || [])[1] || null")
        # The strip's Answer opens the question in the step's setup card (M3a-2).
        await page.get_by_text("Waiting for your input").first.click()
        await asyncio.sleep(0.8)
        await page.locator(CARD_QUESTION).get_by_role("textbox", name="Your answer").fill("yes")
        await page.locator(CARD_QUESTION).get_by_role("button", name="Send answer", exact=True).click()
        await page.get_by_text("Done — status: completed").first.wait_for(timeout=60_000)
        await asyncio.sleep(1.5)
        live = await page.evaluate(NODE_FRAMES)
        if any(v != "border-success" for v in live.values()):
            raise AssertionError(f"after the resumed run completed, the canvas shows {live}")
        await page.goto(f"{APP}/workflows?run={run_id}", wait_until="domcontentloaded")
        await page.get_by_text("Done — status: completed").first.wait_for(timeout=60_000)
        await asyncio.sleep(SETTLE)
        again = await page.evaluate(NODE_FRAMES)
        if any(v != "border-success" for v in again.values()):
            raise AssertionError(f"reopened from its link, the completed run shows {again}")
        return "a resumed run that completes leaves every step done, live and reopened"
    finally:
        await ctx.close()
        drop_fixture(oid)


def review_dict_orchestration(oid: str, repaired: bool) -> dict:
    """F189's shape: a review step with one field whose key is named after it."""
    key = "final_summary"
    return {
        "id": oid, "name": "ui-smoke review dict", "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "trigger": "manual", "entry_step_id": f"{oid}_ask",
        "state_schema": {key: {"type": "dict" if repaired else "str", "default": {} if repaired else "", "description": ""}},
        "steps": [
            {"id": f"{oid}_ask", "name": "Review", "type": "human", "human_prompt": "Summarise the run",
             "position_x": 0, "position_y": 0,
             "human_fields": [{"name": key, "type": "text", "label": "Summary"}],
             "next_step_id": f"{oid}_print", "output_key": key},
            {"id": f"{oid}_print", "name": "Print", "type": "print", "next_step_id": None, "output_key": "out",
             "position_x": 0, "position_y": 320,
             "print_content": f"Final Summary: {{state.{key}.{key}}}" if repaired else f"Final Summary: {{state.{key}}}"},
        ],
    }


@check
async def check_humandictwarn(browser) -> str:
    """The editor warns where the builder's validator does: a fielded answer read as text (F189's twin).

    A human step with fields stores the mapping it was answered with, so a key
    the schema types as `str` holds a dict, and a Print of `{state.key}` shows
    a Python repr. The builder's validator catches both since B3; a flow made by
    hand in the editor got no word of either.
    """
    oid = f"orch_uismoke_dict_{int(time.time())}"
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    panel = page.locator('aside[aria-label="Step configuration"]')

    async def said(step_id: str) -> str:
        # By id: the Print's card carries the warning, which names "Review".
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        node = page.locator(f'.react-flow__node[data-id="{step_id}"]')
        await node.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await node.focus()
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.0)
        return await panel.inner_text() if await panel.count() else ""
    try:
        api("/api/orchestrations", "POST", review_dict_orchestration(oid, repaired=False))
        ask, show = await said(f"{oid}_ask"), await said(f"{oid}_print")
        if "holds a mapping" not in ask:
            raise AssertionError("a fielded review step whose key is typed str gets no warning")
        if "Python dict" not in show:
            raise AssertionError("a Print of a fielded answer's whole key gets no warning")
        api("/api/orchestrations", "POST", review_dict_orchestration(oid, repaired=True))
        ask, show = await said(f"{oid}_ask"), await said(f"{oid}_print")
        if "holds a mapping" in ask or "Python dict" in show:
            raise AssertionError("the repair the warnings name still warns")
        return "a fielded answer typed as text, or printed whole, is warned about, and its repair is not"
    finally:
        await ctx.close()
        drop_fixture(oid)


@check
async def check_badgecontrast(browser) -> str:
    """Status badges meet AA in both themes with the data that shows them (F223).

    `contrast` walks every screen, but these badges only render with a paused
    run, an n8n tool or MCP servers in the store, so a clean store passed while
    a used one read "Needs input" at 1.25:1 and the Active tab's count at 1.42:1
    (dark-palette yellow and blue kept on a light ground), n8n at 3.27:1 and
    MCP's Active and Disconnected at 3.82:1 and 3.78:1. The data is stubbed
    here, so the check sees them on any store.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    runs = [{"run_id": "run_uismoke_badge", "orchestration_id": "orch_uismoke_badge", "status": "paused",
             "waiting_for_human": True, "started_at": now, "ended_at": None, "current_step_id": "ask",
             "steps_completed": 1, "last_step_name": "Ask", "total_cost_usd": 0.0, "score": None,
             "score_regression": False, "session_id": None},
            {"run_id": "run_uismoke_badge2", "orchestration_id": "orch_uismoke_badge", "status": "running",
             "waiting_for_human": False, "started_at": now, "ended_at": None, "current_step_id": "a",
             "steps_completed": 0, "last_step_name": None, "total_cost_usd": 0.0, "score": None,
             "score_regression": False, "session_id": None}]
    tools = [{"name": "uismoke_n8n", "generalName": "Smoke n8n", "description": "an n8n workflow",
              "tool_type": "http", "url": "http://127.0.0.2:9/webhook/x", "method": "POST", "workflowId": "wf_1"}]
    servers = [{"name": "uismoke_on", "label": "Smoke on", "server_type": "remote", "status": "connected",
                "url": "http://127.0.0.2:9/mcp", "token_set": False},
               {"name": "uismoke_off", "label": "Smoke off", "server_type": "remote", "status": "disconnected",
                "url": "http://127.0.0.2:9/mcp", "token_set": False},
               {"name": "uismoke_auth", "label": "Smoke auth", "server_type": "remote", "status": "reauth_needed",
                "url": "http://127.0.0.2:9/mcp", "token_set": False}]
    # DB Configs' badges were raw 500s on a tint of themselves (F239).
    dbs = [{"id": f"db_smoke_{status}", "name": f"Smoke {status}", "db_type": "sqlite",
            "connection_string": "sqlite:///smoke.db", "status": status, "description": ""}
           for status in ("untested", "connected", "error")]
    bad: list[str] = []
    for theme in ("light", "dark"):
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
        def serving(body):
            # One argument only: Playwright hands a two-argument handler the request too.
            async def fulfil(route):
                if route.request.method != "GET":
                    return await route.continue_()
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return fulfil
        for pattern, body in (("**/api/orchestrations/runs", runs), ("**/api/tools/custom", tools),
                              ("**/api/mcp/servers", servers), ("**/api/db-configs", dbs)):
            await ctx.route(pattern, serving(body))
        page = await ctx.new_page()
        try:
            # Each fixture's own label: "n8n" alone also names a hidden control.
            for path, shows in (("/workflows", "Needs input"), ("/tools", "Smoke n8n"), ("/apps", "Smoke off"),
                                ("/settings/db_configs", "Smoke untested")):
                await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
                try:
                    await page.get_by_text(shows, exact=True).first.wait_for(timeout=20_000)
                except Exception:
                    raise AssertionError(f"{path} never showed {shows!r}, so this checks nothing")
                await asyncio.sleep(1.0)
                for row in await page.evaluate(CONTRAST):
                    bad.append(f"{theme} {path}: {row['ratio']}:1 {row['fg']} on {row['bg']} — {row['txt'][:20]!r}")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError(f"{len(bad)} under AA — " + "; ".join(bad[:6]))
    return "run, tool, MCP and DB Configs status badges meet AA in both themes"


@check
async def check_templatepath(browser) -> str:
    """Each template pack shows its workflows' paths by role, and says when a person approves (M2f, D211).

    UX1 found Templates a wall of text. Each pack's card now draws one path per
    workflow it holds, from the pack's bundle, and a workflow with a Human step
    says "You approve before it finishes" in the person ink.
    """
    packs = api("/api/examples")
    want_paths = want_person = 0
    for pack in packs:
        if not pack.get("orchestration_count"):
            continue
        for o in api(f"/api/examples/{pack['id']}").get("orchestrations", []):
            want_paths += 1
            want_person += any(st.get("type") == "human" for st in o.get("steps", []))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/templates", wait_until="domcontentloaded")
        await page.locator("[data-step-path]").first.wait_for(timeout=20_000)
        await asyncio.sleep(SETTLE)
        got = await page.evaluate(r"""() => {
          const paths = [...document.querySelectorAll('[data-step-path]')];
          const flags = [...document.querySelectorAll('main span')].filter(s => s.textContent.trim() === 'You approve before it finishes');
          const ink = getComputedStyle(document.documentElement).getPropertyValue('--person-ink').trim();
          const probe = document.createElement('span'); probe.style.color = ink; document.body.appendChild(probe);
          const inkRgb = getComputedStyle(probe).color; probe.remove();
          return { paths: paths.length, labelled: paths.filter(p => /^\d+ steps?/.test(p.getAttribute('aria-label') || '')).length,
                   flags: flags.length, inked: flags.filter(f => getComputedStyle(f).color === inkRgb).length };
        }""")
    finally:
        await ctx.close()
    bad = []
    if got["paths"] != want_paths:
        bad.append(f"{got['paths']} paths drawn for {want_paths} workflows")
    if got["labelled"] != got["paths"]:
        bad.append("a path has no words for a screen reader")
    if got["flags"] != want_person:
        bad.append(f"{got['flags']} 'You approve' notes for {want_person} workflows with a person")
    if got["inked"] != got["flags"]:
        bad.append("a 'You approve' note is not in the person ink")
    if bad:
        raise AssertionError("; ".join(bad))
    return f"{want_paths} workflow paths, {want_person} marked as approved by a person"


@check
async def check_appmark(browser) -> str:
    """An app's letter tile follows the theme and its letter meets AA (M2f).

    The tile was a fixed hsl(… 45% 22%) that stayed dark on white (UX1). It
    takes its lightness from --tile-* now: pale on light, deep on dark.
    """
    servers = [{"name": f"uismoke_tile{i}", "label": label, "server_type": "remote", "status": "connected",
                "url": "http://127.0.0.2:9/mcp", "token_set": False}
               for i, label in enumerate(("Atlas", "Bramble", "Cobalt", "Dune", "Ember", "Fjord"))]
    bad: list[str] = []
    seen = 0
    for theme in ("light", "dark"):
        ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
        await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
        async def fulfil(route):
            if route.request.method != "GET":
                return await route.continue_()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(servers))
        await ctx.route("**/api/mcp/servers", fulfil)
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/apps", wait_until="domcontentloaded")
            await page.locator('[data-mcp-row] [data-app-tile]').first.wait_for(timeout=20_000)
            await asyncio.sleep(1.0)
            tiles = await page.evaluate(r"""() => {
              const lum = (c) => { const m = c.match(/[\d.]+/g).map(Number); const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
                return 0.2126 * f(m[0]) + 0.7152 * f(m[1]) + 0.0722 * f(m[2]); };
              const toRgb = (c) => { const x = document.createElement('canvas').getContext('2d'); x.fillStyle = c; x.fillRect(0, 0, 1, 1);
                const d = x.getImageData(0, 0, 1, 1).data; return `rgb(${d[0]}, ${d[1]}, ${d[2]})`; };
              return [...document.querySelectorAll('[data-mcp-row] [data-app-tile]')].map(t => {
                const cs = getComputedStyle(t); const bg = lum(toRgb(cs.backgroundColor)), fg = lum(toRgb(cs.color));
                const [hi, lo] = bg > fg ? [bg, fg] : [fg, bg];
                return { txt: t.textContent, bg, ratio: Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100 };
              });
            }""")
        finally:
            await ctx.close()
        seen += len(tiles)
        for t in tiles:
            if t["ratio"] < 4.5:
                bad.append(f"{theme} {t['txt']}: {t['ratio']}:1")
            if theme == "light" and t["bg"] < 0.6:
                bad.append(f"light {t['txt']}: a dark tile on white")
            if theme == "dark" and t["bg"] > 0.2:
                bad.append(f"dark {t['txt']}: a pale tile on dark")
    if not seen:
        raise AssertionError("no letter tile rendered, so this checks nothing")
    if bad:
        raise AssertionError(f"{len(bad)} tiles — " + "; ".join(bad[:6]))
    return f"{seen} letter tiles follow the theme and meet AA"


@check
async def check_googlecard(browser) -> str:
    """Apps leads with Google Workspace, which goes to Integrations (M2f, Naveen's call)."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/apps", wait_until="domcontentloaded")
        card = page.locator("[data-google-card]")
        await card.get_by_role("link").first.wait_for(timeout=20_000)
        href = await card.get_by_role("link").first.get_attribute("href")
        first = await page.evaluate("() => { const c = document.querySelector('[data-google-card]'); const m = document.querySelector('main [data-mcp-list], main h2#mcp-connected-title');"
                                    " return !!(c && m && (c.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING)); }")
    finally:
        await ctx.close()
    if href != "/settings/workspace":
        raise AssertionError(f"the Google card leads to {href!r}, not Integrations")
    if not first:
        raise AssertionError("the Google card is not above the servers")
    return "the Google Workspace card leads Apps and goes to Integrations"


@check
async def check_toolsopen(browser) -> str:
    """Tools opens its first tool beside the list, and the page fits at 1600 and 400 (M2f).

    UX1 found the detail pane opening on nothing. The list and the editor sit
    side by side now, and the first tool opens by itself.
    """
    tools = [{"name": "uismoke_alpha", "generalName": "Smoke alpha", "description": "first", "tool_type": "http",
              "url": "http://127.0.0.2:9/a", "method": "GET", "inputSchema": {"type": "object", "properties": {}}},
             {"name": "uismoke_beta", "generalName": "Smoke beta", "description": "second", "tool_type": "http",
              "url": "http://127.0.0.2:9/b", "method": "POST", "inputSchema": {"type": "object", "properties": {}}}]
    bad: list[str] = []
    for width, height in (WIDE, PHONE):
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        async def fulfil(route):
            if route.request.method != "GET":
                return await route.continue_()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(tools))
        await ctx.route("**/api/tools/custom", fulfil)
        page = await ctx.new_page()
        try:
            await page.goto(f"{APP}/tools", wait_until="domcontentloaded")
            try:
                await page.get_by_role("heading", name=re.compile(r"Editing: uismoke_alpha")).wait_for(timeout=20_000)
            except Exception:
                bad.append(f"{width}: the first tool did not open")
                continue
            current = await page.locator('[data-tool-row] > button[aria-current="true"]').count()
            if current != 1:
                bad.append(f"{width}: {current} rows marked open")
            # Opening the second keeps the editor beside the list, with no question (nothing edited).
            await page.locator('[data-tool-row="uismoke_beta"] > button:first-child').click()
            try:
                await page.get_by_role("heading", name=re.compile(r"Editing: uismoke_beta")).wait_for(timeout=5_000)
            except Exception:
                bad.append(f"{width}: a row did not open its tool")
            fits = await page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            if not fits:
                bad.append(f"{width}: the page scrolls sideways")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the first tool opens beside the list, a row opens its tool, and the page fits at both widths"


@check
async def check_evalsactions(browser) -> str:
    """Evals does what it says: a backfill button, an honest run confirm, Stop that clears (F204).

    The empty state promised a backfill no button offered. A golden set on an
    orchestration with no rubric ran after a native confirm pricing judge calls
    that never happen, then finished saying nothing. A set started from a
    collapsed card kept its Stop button after it had ended, because only the
    open card was polled. The rubric's judge model was a free-text box. The
    backfill is stubbed, so no judge model is called; the set that runs has no
    rubric, so nothing is scored either.
    """
    stamp = int(time.time())
    scored, bare = f"orch_uismoke_evs_{stamp}", f"orch_uismoke_evu_{stamp}"
    rubric = api("/api/rubrics", "POST", {"name": f"Smoke rubric {stamp}", "criteria": [{"name": "Clear"}],
                                          "judge_model": "default"})
    api("/api/orchestrations", "POST", {**print_orchestration(scored, f"ui-smoke scored {stamp}"),
                                        "rubric_id": rubric["id"]})
    slow = print_orchestration(bare, f"ui-smoke unscored {stamp}")
    slow["steps"][0] = {"id": f"{bare}_a", "name": "Wait", "type": "transform", "output_key": "one",
                        "transform_code": "import time\ntime.sleep(4)\nresult = 1", "next_step_id": f"{bare}_end"}
    api("/api/orchestrations", "POST", slow)
    suite = api("/api/evals/suites", "POST", {"name": f"Smoke set {stamp}", "orchestration_id": bare,
                                              "cases": [{"name": "one", "input": "hi", "notes": ""}]})
    native: list[str] = []
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    page.on("dialog", lambda d: (native.append(d.message), asyncio.ensure_future(d.dismiss())))
    backfills: list[str] = []

    async def stub_backfill(route):
        backfills.append(route.request.post_data or "")
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"requested": 2, "scored": 2, "scores": []}))
    await page.route("**/api/evals/orchestrations/*/backfill", stub_backfill)
    try:
        await page.goto(f"{APP}/evals", wait_until="domcontentloaded")
        await page.get_by_text(f"Smoke set {stamp}", exact=True).wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        # M2h: a rubric opens on its own scores ("Scores for", its workflows first).
        await page.get_by_role("button", name=re.compile(f"^Smoke rubric {stamp}")).click()
        await asyncio.sleep(0.8)
        detail = page.locator('section[aria-label="Detail"]')
        await detail.get_by_role("combobox").first.click()
        await page.get_by_role("option", name=f"ui-smoke scored {stamp}").click()
        button = page.get_by_role("button", name=re.compile("score earlier runs", re.I))
        try:
            await button.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("a workflow with a rubric and nothing scored offers no backfill")
        await button.click()
        try:
            await page.get_by_text("Scored 2 of 2 runs.").wait_for(timeout=10_000)
        except Exception:
            raise AssertionError(f"no 'Scored 2 of 2 runs.' after the backfill ({len(backfills)} sent)")
        if not backfills:
            raise AssertionError("the backfill button sent nothing")

        # M2h: a golden set opens beside the list, with Run / Stop in its head.
        await page.get_by_role("button", name=re.compile(f"^Smoke set {stamp}")).click()
        row = detail
        await row.get_by_role("button", name="Run", exact=True).click()
        await asyncio.sleep(0.8)
        if native:
            raise AssertionError(f"running a set asks with a native dialog: {native[0][:80]!r}")
        dialog = page.get_by_role("dialog")
        text = await dialog.inner_text()
        if "not be scored" not in text or "judge call" in text:
            raise AssertionError(f"a set with no rubric is confirmed as {text[:160]!r}")
        await dialog.get_by_role("button", name="Run set").click()
        try:
            await row.get_by_role("button", name=re.compile("Stop")).wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("a running set shows no Stop")
        stopped = False
        for _ in range(30):
            await asyncio.sleep(1.0)
            if not await row.get_by_role("button", name=re.compile("Stop")).count():
                stopped = True
                break
        if not stopped:
            raise AssertionError("a set still shows Stop after it has ended")

        await page.get_by_role("button", name=re.compile(f"^Smoke rubric {stamp}")).click()
        judge = page.get_by_role("button", name="Judge model")
        if not await judge.count():
            raise AssertionError("the rubric's judge model is not a model picker")
        return "Evals backfills, confirms a set honestly in its own dialog, clears Stop, and picks the judge from the models"
    finally:
        await ctx.close()
        for path in (f"/api/evals/suites/{suite.get('id')}", f"/api/rubrics/{rubric.get('id')}"):
            try:
                api(path, "DELETE")
            except Exception:
                pass
        drop_fixture(scored)
        drop_fixture(bare)


@check
async def check_plurals(browser) -> str:
    """Counts agree with their nouns and stamps are in the reader's time (F205).

    Evals said "1 criteria"; its golden-set history and Runs & Logs printed UTC
    with no zone ("2026-09-25 10:08:03") beside local times everywhere else. The
    run-log stamps carry no zone, so a reader in IST saw them 5½ hours out. Read
    here from Asia/Kolkata, where UTC and local cannot be mistaken for each other.
    """
    stamp = int(time.time())
    oid = f"orch_uismoke_tz_{stamp}"
    rubric = api("/api/rubrics", "POST", {"name": f"Smoke tz {stamp}", "criteria": [{"name": "Clear"}]})
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke tz {stamp}"))
    run_id = run_to_end(oid)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, timezone_id="Asia/Kolkata")
    page = await ctx.new_page()
    hydration: list[str] = []
    page.on("console", lambda m: hydration.append(m.text) if "ydrat" in m.text else None)
    try:
        await page.goto(f"{APP}/evals", wait_until="domcontentloaded")
        item = page.get_by_role("button", name=re.compile(f"^Smoke tz {stamp}"))
        await item.wait_for(timeout=30_000)
        line = " ".join((await item.inner_text()).split())
        if "1 criterion " not in line + " ":
            raise AssertionError(f"a rubric with one criterion reads {line!r}")

        await page.goto(f"{APP}/logs", wait_until="domcontentloaded")
        tab = page.get_by_role("button", name=re.compile("^Workflow logs"))
        await tab.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await tab.click()
        row = page.locator(f'[data-log-row="{run_id}"]')
        await row.wait_for(timeout=20_000)
        await asyncio.sleep(1.0)
        shown = await row.inner_text()
        raw = next((log["started_at"] for log in api("/api/logs/orchestrations?limit=50") if log["run_id"] == run_id), None)
        if not raw:
            raise AssertionError("the fixture run wrote no log, so this checks nothing")
        if raw in shown:
            raise AssertionError(f"Logs prints the log's UTC stamp {raw!r} as it was written")
        # M2h: the row says when in UX1's words ("Today 3:30 PM"), the full instant on hover.
        stamp_at = await row.locator("time").first.get_attribute("datetime")
        same = await page.evaluate("([a, raw]) => Date.parse(a) === Date.parse(raw.replace(' ', 'T') + 'Z')", [stamp_at, raw])
        if not same:
            raise AssertionError(f"the log's time is {stamp_at!r} for {raw} UTC")
        local = await page.evaluate(
            "(s) => new Date(s.replace(' ', 'T') + 'Z').toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'})"
            ".replace(/\\s?[AP]M$/, '')", raw)
        if local not in shown:
            raise AssertionError(f"Logs shows {shown!r} for {raw} UTC, not the reader's {local!r}")
        if hydration:
            raise AssertionError(f"a local time broke hydration: {hydration[0][:120]}")
        return "counts agree with their nouns, and log and eval times are the reader's"
    finally:
        await ctx.close()
        drop_fixture(oid)
        for path in (f"/api/rubrics/{rubric.get('id')}", f"/api/logs/orchestrations/{run_id}"):
            try:
                api(path, "DELETE")
            except Exception:
                pass


#: Buttons in <main> a screen reader would announce as just "button".
UNNAMED_BUTTONS = """() => [...document.querySelectorAll('main button')]
  .filter(b => b.getBoundingClientRect().width > 0)
  .filter(b => !b.getAttribute('aria-label') && !b.getAttribute('aria-labelledby') && !b.textContent.trim() && !b.title)
  .length"""


@check
async def check_pricingnames(browser) -> str:
    """Every button on the pricing reference has a name (F180).

    `/usage` → Pricing Reference: each row's delete × was a button with no name
    (31 of them on a default rate card), and so was the add row's ✓. The
    spinbuttons beside them were already named after their model.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/usage", wait_until="domcontentloaded")
        await page.locator('input[aria-label$="input price per million tokens"]').first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        unnamed = await page.evaluate(UNNAMED_BUTTONS)
        if unnamed:
            raise AssertionError(f"/usage has {unnamed} unnamed buttons")
        await page.get_by_role("button", name=re.compile("Add model")).first.click()
        await asyncio.sleep(0.8)
        unnamed = await page.evaluate(UNNAMED_BUTTONS)
        if unnamed:
            raise AssertionError(f"with the add row open, /usage has {unnamed} unnamed buttons")
        return "every pricing row's remove button, and the add row's, has a name"
    finally:
        await ctx.close()


@check
async def check_skillhint(browser) -> str:
    """The skills.sh dialog's own example is one the CLI can install (F203).

    Its hint offered `vercel-labs/agent-skills@react-best-practices`; the skill
    is `vercel-react-best-practices` now, so the example failed with the CLI's
    banner and spinner frames above "No matching skills found". The backend now
    strips those (`tests/unit/test_skills.py`); this checks the example.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/skills", wait_until="domcontentloaded")
        opener = page.get_by_role("button", name=re.compile(r"skills\.sh", re.I)).first
        await opener.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await opener.click()
        dialog = page.get_by_role("dialog")
        await dialog.wait_for(timeout=5_000)
        text = await dialog.inner_text()
        if "agent-skills@react-best-practices" in text:
            raise AssertionError("the dialog's example names a skill the CLI no longer has")
        if "agent-skills@vercel-react-best-practices" not in text:
            raise AssertionError(f"the dialog's example is not the current one: {text[:200]!r}")
        return "the skills.sh dialog's example names a skill the CLI has"
    finally:
        await ctx.close()


@check
async def check_agentname(browser) -> str:
    """The chat page names the active agent, whichever request answers first (F229).

    The page set its agent name from `/api/status` (the active agent) and from
    `/api/settings` (the workspace's own name, "Synapse"). Both wrote the name,
    so when status answered first the header and the welcome line said
    "Synapse". Here status is served at once and settings late, which is the
    order that lost. The status is stubbed so the install's own active agent is
    left alone.
    """
    aid = f"agent_uismoke_named_{int(time.time())}"
    name = f"Named {aid[-6:]}"
    api("/api/agents", "POST", _agent(aid, name))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()

    async def active(route):
        response = await route.fetch()
        status = await response.json()
        status["active_agent_id"] = aid
        status.setdefault("agents", {})[aid] = {"name": name}
        await route.fulfill(response=response, json=status)

    async def late(route):
        await asyncio.sleep(1.5)
        await route.continue_()
    await page.route("**/api/status", active)
    await page.route("**/api/settings", late)
    try:
        await page.goto(f"{APP}/", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 3.0)
        text = await page.locator("main").inner_text()
        if name.lower() not in text.lower():
            workspace = api("/api/settings").get("agent_name")
            raise AssertionError(f"with {name!r} active, the chat page names {workspace!r} once settings answers")
        return "the chat page names the active agent, even when settings answers last"
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass


@check
async def check_morerows(browser) -> str:
    """The F219 pattern, on the screens no batch owned, opens from the keyboard (F236).

    Messaging channels, Repos, DB Configs, Scale's runs, Import's drop zone and
    section rows, and chat's History rows were `div`s and `tr`s with a click
    handler: out of the tab order, or (Messaging) with actions that did not even
    exist until hovered. The History drawer had no way out but a mouse. Each
    list is stubbed, so a real install gains no channel, repo or config.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    stubs = {
        "**/api/messaging/channels": [{"id": "ch_smoke", "name": "Smoke channel", "platform": "telegram",
                                       "status": "stopped", "agent_ids": [], "credentials": {}}],
        "**/api/repos": [{"id": "repo_smoke", "name": "Smoke repo", "path": "/tmp/smoke", "status": "indexed",
                          "file_count": 1, "description": ""}],
        "**/api/db-configs": [{"id": "db_smoke", "name": "Smoke db", "db_type": "sqlite",
                               "connection_string": "sqlite:///smoke.db", "status": "untested", "description": ""}],
        "**/api/scale/runs?limit=20": [{"run_id": "run_smoke_scale", "status": "completed", "orchestration_id": "o",
                                        "started_at": now, "ended_at": now, "total_cost_usd": 0.0}],
        "**/api/sessions": [{"session_id": "sess_smoke", "agent_id": "agent_smoke", "last_updated": now,
                             "first_user_message": "smoke question", "last_response": "smoke answer", "turn_count": 1}],
    }
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})

    def serving(body):
        async def fulfil(route):
            if route.request.method != "GET":
                return await route.continue_()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return fulfil
    for pattern, body in stubs.items():
        await ctx.route(pattern, serving(body))
    page = await ctx.new_page()

    async def reach(path: str, name: str, why: str):
        await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
        control = page.get_by_role("button", name=re.compile(name)).first
        try:
            await control.wait_for(timeout=30_000)
        except Exception:
            raise AssertionError(f"{path}: {why} is not a control a keyboard can reach")
        await asyncio.sleep(SETTLE)
        return control

    async def next_stop() -> dict:
        await page.keyboard.press("Tab")
        return await page.evaluate(
            "() => ({label: document.activeElement.getAttribute('aria-label'),"
            " opacity: getComputedStyle(document.activeElement).opacity})")
    try:
        row = await reach("/channels", "Smoke channel", "a channel")
        await row.focus()
        after = await next_stop()
        if after["label"] != "Start Smoke channel" or after["opacity"] != "1":
            raise AssertionError(f"after a channel, Tab reaches {after}, not its named Start action")

        for path, name, action in (("/settings/repos", "Smoke repo", "Delete Smoke repo"),
                                   ("/settings/db_configs", "Smoke db", "Refresh the schema of Smoke db")):
            control = await reach(path, f"^{name}$", "an item's name")
            await control.focus()
            stops = [await next_stop() for _ in range(3)]
            if not any(s["label"] == action and s["opacity"] == "1" for s in stops):
                raise AssertionError(f"{path}: Tab from {name!r} never reaches a visible {action!r}: {stops}")

        await reach("/settings/scale", "^Open run run_smoke_scale$", "a recent run")
        if not await page.get_by_role("button", name=re.compile("^Copy run id")).count():
            raise AssertionError("/settings/scale: a run's copy button has no name")

        await page.goto(f"{APP}/settings/import_export", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        tab = page.get_by_role("button", name=re.compile("^Import$"))
        if await tab.count():
            await tab.first.click()
        zone = page.get_by_role("button", name=re.compile("Drop your export file here"))
        try:
            await zone.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("Import's drop zone is not a control a keyboard can reach")
        bundle = {"synapse_export": True, "version": "1.0", "exported_at": now, "has_python_tools": False,
                  "orchestrations": [], "agents": [{"id": "agent_smoke_import", "name": "Smoke import agent",
                                                    "description": "d", "tools": [], "system_prompt": "p"}],
                  "custom_tools": [], "mcp_servers": []}
        await page.locator("input[type=file]").set_input_files(
            files=[{"name": "export.json", "mimeType": "application/json", "buffer": json.dumps(bundle).encode()}])
        box = page.get_by_role("checkbox", name="Smoke import agent")
        try:
            await box.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("an item to import is not a checkbox a keyboard can tick")
        before = await box.get_attribute("aria-checked")
        await box.focus()
        await page.keyboard.press(" ")
        if await box.get_attribute("aria-checked") == before:
            raise AssertionError("Space on an item to import does not change it")

        await page.goto(f"{APP}/", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        # History is on the rail since TABS1; its drawer is the kit's.
        await page.get_by_role("button", name=re.compile("history", re.I)).first.click()
        drawer = page.get_by_role("dialog", name="History")
        try:
            await drawer.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("the chat History drawer is not a named dialog")
        await asyncio.sleep(0.3)
        inside = await page.evaluate(
            "() => !!document.activeElement && !!document.activeElement.closest('[role=dialog]')")
        if not inside:
            focused = await page.evaluate("() => document.activeElement && document.activeElement.getAttribute('aria-label')")
            raise AssertionError(f"opening History leaves focus on {focused!r}, not in the drawer")
        if not await drawer.get_by_role("button", name=re.compile("smoke question")).count():
            raise AssertionError("a History row is not a control a keyboard can reach")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        if await drawer.count():
            raise AssertionError("Escape does not close the History drawer")
        return "channels, repos, DB configs, Scale runs, Import and chat History all work from the keyboard"
    finally:
        await ctx.close()


# ── the pre-C2 UI round (U1) ─────────────────────────────────────────────────

@check
async def check_menuclose(browser) -> str:
    """A kit menu closes when an item is picked, and a dialog it opens keeps focus (U58).

    `MenuItem` called `preventDefault` on Radix's select, so every kit menu
    stayed open after a pick until Escape. The editor's narrow-screen menu is
    the case with both kinds of item: Deploy acts in place, Delete opens a
    confirm dialog. What letting the menu close could break is that dialog:
    Radix hands focus back to the menu's trigger on close, and a dropdown that
    closes under a dialog can leave `pointer-events: none` on the body.
    """
    oid = f"orch_uismoke_menu_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke menu {oid[-6:]}"))
    ctx = await browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]})
    page = await ctx.new_page()
    deploys: list[str] = []

    async def fake_deploy(route):
        # Answered here: a real deploy would leave an agent in the store.
        deploys.append(route.request.method)
        await route.fulfill(json={"agent_id": f"orch_agent_{oid}"})

    await page.route(f"**/api/orchestrations/{oid}/deploy", fake_deploy)
    open_menus = "() => document.querySelectorAll('[role=menu]').length"
    try:
        bad: list[str] = []
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        more = page.get_by_role("button", name="More actions", exact=True)

        await more.click()
        await page.get_by_role("menuitem", name="Deploy as Agent").click()
        await asyncio.sleep(0.8)
        if deploys != ["POST"]:
            bad.append(f"Deploy sent {deploys}, not one POST")
        if await page.evaluate(open_menus):
            bad.append("the menu is still open after Deploy")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)

        await page.evaluate("""() => { window.__focusins = [];
          document.addEventListener('focusin', (e) => window.__focusins.push(
            e.target.closest('[role=dialog],[role=alertdialog]') ? 'dialog'
              : (e.target.getAttribute('aria-label') || e.target.getAttribute('role') || e.target.tagName)), true); }""")
        await more.click()
        await page.get_by_role("menuitem", name="Delete").click()
        dialog = page.get_by_role("alertdialog").or_(page.get_by_role("dialog"))
        await dialog.first.wait_for(timeout=5_000)
        await asyncio.sleep(0.5)
        if await page.evaluate(open_menus):
            bad.append("the menu is still open under the Delete dialog")
        inside = await page.evaluate(
            "() => !!document.activeElement?.closest('[role=alertdialog],[role=dialog]')")
        if not inside:
            bad.append("focus is not inside the Delete dialog")
        # The item's action runs once the menu has handed focus back to its
        # button, so the dialog opens from there (and a dialog that restores
        # focus on close, F310, would land on it).
        order = await page.evaluate("() => window.__focusins")
        before_dialog = order[:order.index("dialog")] if "dialog" in order else order
        if not before_dialog or before_dialog[-1] != "More actions":
            bad.append(f"the dialog did not open from the menu's button (focus went {order})")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        if await dialog.count():
            bad.append("Escape leaves the Delete dialog open")
        back_on = await page.evaluate(
            "() => document.activeElement?.getAttribute('aria-label') || document.activeElement?.tagName")
        if back_on != "More actions":
            bad.append(f"closing the Delete dialog left focus on {back_on}, not the menu's button (F310)")
        if await page.evaluate(open_menus):
            bad.append("the menu is still open once the Delete dialog closes")
        stuck = await page.evaluate("() => getComputedStyle(document.body).pointerEvents")
        if stuck == "none":
            bad.append("the page stays unclickable (body pointer-events: none) after the dialog")
        if not await more.count():
            # An open modal menu hides the rest of the page from the
            # accessibility tree; a second Escape is what a person had to find.
            bad.append("the menu's trigger is hidden from the accessibility tree after the dialog")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        await more.click(timeout=5_000)
        if not await page.get_by_role("menuitem", name="Delete").count():
            bad.append("the menu does not open again after the dialog")
        await page.keyboard.press("Escape")

        await page.set_viewport_size({"width": WIDE[0], "height": WIDE[1]})
        await page.goto(f"{APP}/kitchen-sink", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        row_menu = page.get_by_role("button", name="More actions", exact=True)
        if await row_menu.count():
            await row_menu.first.click()
            await page.get_by_role("menuitem", name="Open").click()
            await asyncio.sleep(0.5)
            if await page.evaluate(open_menus):
                bad.append("the kitchen sink's row menu is still open after a pick")
        else:
            bad.append("no row menu on /kitchen-sink (is ENABLE_KITCHEN_SINK set?)")
        if bad:
            raise AssertionError("; ".join(bad))
        return "Deploy and a row pick close the menu; Delete's dialog opens with focus, and the page stays usable"
    finally:
        await ctx.close()
        drop_fixture(oid)


@check
async def check_dialogfocus(browser) -> str:
    """A kit dialog gives focus back to whatever opened it when it closes (F310, F382).

    `Modal`, `ConfirmDialog` and `Drawer` are opened from state, with no
    `Dialog.Trigger`, and Radix only hands focus back to its own trigger: every
    close dropped it on `<body>`, so a keyboard or screen-reader user lost their
    place after each confirm. Opened from the keyboard here, as that user would,
    and closed each way a dialog closes: Escape, a footer button, the X. The
    drawer was missed by F310 (F382) and is what the help panel is built on.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    focused = """() => { const el = document.activeElement;
      return el === document.body ? 'BODY' : (el?.getAttribute('aria-label') || el?.textContent?.trim()); }"""
    try:
        bad: list[str] = []
        await page.goto(f"{APP}/kitchen-sink", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        cases = [
            ("Open modal", "dialog", "Escape", None),
            ("Open modal", "dialog", None, "Save"),
            ("Open modal", "dialog", None, "Close"),
            ("Delete something", "dialog", "Escape", None),
            ("Delete something", "dialog", None, "Cancel"),
            ("Open drawer", "dialog", "Escape", None),
            ("Open drawer", "dialog", None, "Done"),
            ("Open drawer", "dialog", None, "Close"),
        ]
        for opener, role, key, button in cases:
            await page.get_by_role("button", name=opener, exact=True).focus()
            await page.keyboard.press("Enter")
            dialog = page.get_by_role(role)
            try:
                await dialog.first.wait_for(timeout=5_000)
            except Exception:
                bad.append(f"Enter on {opener!r} opens no dialog")
                continue
            await asyncio.sleep(0.3)
            if key:
                await page.keyboard.press(key)
            else:
                await dialog.get_by_role("button", name=button, exact=True).click()
            await asyncio.sleep(0.5)
            how = key or f"its {button} button"
            if await dialog.count():
                bad.append(f"{how} leaves {opener!r}'s dialog open")
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                continue
            back_on = await page.evaluate(focused)
            if back_on != opener:
                bad.append(f"closing {opener!r}'s dialog with {how} left focus on {back_on!r}")
        if bad:
            raise AssertionError("; ".join(bad))
        return ("a Modal, a ConfirmDialog and a Drawer hand focus back to their opener on Escape, a footer "
                "button and the X")
    finally:
        await ctx.close()


async def _wheel_scrolls(page, scroller) -> str | None:
    """None when the wheel over an open list scrolls it and the list fits the window; else what is wrong."""
    try:
        await scroller.wait_for(timeout=5_000)
    except Exception:
        return "its list never opened"
    box = await scroller.bounding_box()
    height = page.viewport_size["height"]
    if not box:
        return "its list has no box"
    if box["y"] < -1 or box["y"] + box["height"] > height + 1:
        return f"its list runs off the window ({box['y']:.0f}..{box['y'] + box['height']:.0f} of {height}px)"
    if await scroller.evaluate("e => e.scrollHeight <= e.clientHeight"):
        return "its list is not taller than its box, so it cannot be measured"
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await page.mouse.wheel(0, 400)
    await asyncio.sleep(0.5)
    if not await scroller.evaluate("e => e.scrollTop"):
        return "the wheel does not scroll its list"
    return None


#: The kitchen sink's long dropdowns (F700, F701): (what, how to open it, the element that scrolls).
_LONG_LISTS = (
    ("Combobox", lambda d: d.get_by_role("button", name="Long combobox").click(), "[cmdk-list]"),
    ("Select", lambda d: d.get_by_role("combobox", name="Long select").click(), "[data-radix-select-viewport]"),
    ("Menu", lambda d: d.get_by_role("button", name="Long menu").click(), "[role=menu]"),
    ("template suggestions", lambda d: d.get_by_role("textbox", name="Long template").fill("{{ st"),
     "[role=listbox][aria-label=Suggestions]"),
)


@check
async def check_dropdownscroll(browser) -> str:
    """Every kind of dropdown scrolls under the wheel inside a Modal and a Drawer, and fits the window (F700, F701).

    A dialog locks the page's scroll and lets only its own content scroll. A Combobox's or a template field's list
    is portalled out of the dialog, so the lock threw away every wheel and touch over it: the Evaluate dialog's
    judge model could be reached only with the arrow keys. A long Menu had no height of its own and ran off the
    screen. Measured on the kitchen sink's sixty-item lists, then on the product's own model pickers in the
    Evaluate dialog and the Knowledge settings drawer, with sixty models.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    many = [f"gpt-smoke-{n:02d}" for n in range(1, 61)]
    models = _answer({"providers": {"openai": {"available": True, "models": many,
                                                "embedding_models": ["text-embedding-3-small"]}}})
    for pattern, handler in _SMOKE_KB_EVAL_ROUTES + _SMOKE_KB_CONTEXTUAL_ROUTES[len(_SMOKE_KB_ROUTES):]:
        await ctx.route(pattern, handler)
    await ctx.route("**/api/models", models)
    page = await ctx.new_page()
    try:
        bad: list[str] = []
        await page.goto(f"{APP}/kitchen-sink", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        for opener in ("Open modal", "Open drawer"):
            for what, open_it, selector in _LONG_LISTS:
                await page.get_by_role("button", name=opener, exact=True).click()
                dialog = page.get_by_role("dialog")
                await dialog.first.wait_for(timeout=5_000)
                await asyncio.sleep(0.3)
                await open_it(dialog.first)
                wrong = await _wheel_scrolls(page, page.locator(selector).first)
                if wrong:
                    bad.append(f"{what} in {opener.removeprefix('Open ')}: {wrong}")
                # Counted in the DOM: an open Select hides the dialog from the accessibility tree.
                for _ in range(4):
                    if not await page.locator("[role=dialog]").count():
                        break
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)

        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile("^Evaluate")).wait_for(timeout=30_000)
        await _kb_eval(page)
        await page.get_by_role("button", name="Run", exact=True).click()
        run = page.get_by_role("dialog", name="Run Smoke questions")
        await run.wait_for(timeout=10_000)
        await run.get_by_role("checkbox", name="Grade answers").click()
        await run.get_by_role("button", name="Judge model").click()
        wrong = await _wheel_scrolls(page, page.locator("[cmdk-list]").first)
        if wrong:
            bad.append(f"the Evaluate dialog's Judge model: {wrong}")
        for _ in range(2):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)

        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Settings", exact=True).wait_for(timeout=30_000)
        await _kb_settings_contextual(page)
        await page.get_by_role("button", name="Context model", exact=True).click()
        wrong = await _wheel_scrolls(page, page.locator("[cmdk-list]").first)
        if wrong:
            bad.append(f"the Knowledge settings drawer's Context model: {wrong}")
        if bad:
            raise AssertionError("; ".join(bad))
        return ("a Combobox, a Select, a Menu and template suggestions scroll under the wheel in a Modal and a "
                "Drawer, and so do the judge and context model pickers")
    finally:
        await ctx.close()


def _png(width: int = 4, height: int = 3) -> bytes:
    """A small solid-red PNG, built here so the check needs no file on disk."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@check
async def check_lightbox(browser) -> str:
    """An image attached in chat opens full size when its thumbnail is pressed (F238).

    The thumbnail called `window.open` on the image's `data:` URL, and Chrome
    will not open a data URL in a new tab: a click (and Enter, since F236) did
    nothing at all. It opens in a dialog on the page now, drawing the same
    `data:` URL, so opening it costs no request and no new tab.
    """
    status = {**_status(True), "agents": {
        "agent_alpha": {"name": "Alpha", "status": "online", "can_see_images": True},
    }, "active_agent_id": "agent_alpha"}
    ctx, page = await _chat_page(browser, "uismoke-lightbox", extra_init=SERVE_STATUS % json.dumps(status))
    try:
        bad: list[str] = []
        await page.locator("input[type=file]").first.set_input_files(
            {"name": "red.png", "mimeType": "image/png", "buffer": _png()})
        await asyncio.sleep(0.5)
        await page.locator("main textarea").last.fill("what colour is this?")
        await page.keyboard.press("Enter")
        await _stream_open(page, "post")
        thumb = page.get_by_role("button", name="Open attached image 1")
        await thumb.wait_for(timeout=10_000)
        sent = [c for c in await page.evaluate("() => window.__calls") if "/api/chat/stream" in c["url"]]
        if len(sent) != 1:
            raise AssertionError(f"the turn was not sent once: {sent}")

        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        pages_before = len(ctx.pages)
        await thumb.focus()
        await page.keyboard.press("Enter")
        dialog = page.get_by_role("dialog", name="Attached image 1")
        try:
            await dialog.wait_for(timeout=5_000)
        except Exception:
            bad.append("pressing the thumbnail opens no dialog")
        else:
            src = await dialog.get_by_role("img", name="Attached image 1").get_attribute("src") or ""
            if not src.startswith("data:image/png;base64,"):
                bad.append(f"the dialog draws {src[:40]!r}, not the attached image")
            if not await page.evaluate("() => !!document.activeElement?.closest('[role=dialog]')"):
                bad.append("focus is not inside the image's dialog")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            if await dialog.count():
                bad.append("Escape leaves the image open")
            back_on = await page.evaluate("() => document.activeElement?.getAttribute('aria-label')")
            if back_on != "Open attached image 1":
                bad.append(f"closing the image left focus on {back_on}, not its thumbnail (F310)")
        if len(ctx.pages) != pages_before:
            bad.append(f"opening the image opened {len(ctx.pages) - pages_before} new page(s)")
        fetched = [u for u in requests if not u.startswith("data:")]
        if fetched:
            bad.append(f"opening the image made requests: {fetched[:3]}")
        if bad:
            raise AssertionError("; ".join(bad))
        return "Enter on a thumbnail opens the image in a named dialog, with no request and no new tab"
    finally:
        await ctx.close()


@check
async def check_workflowheader(browser) -> str:
    """A workflow agent's chat header says it is a workflow, not a model it never uses (U64).

    A workflow (orchestrator) agent has no model of its own — each step runs on
    the one set on it — so `/api/status` resolved the workspace default for it
    and the header read "Model: <default>". The status now names each agent's
    type and workflow, and the header reads "Workflow · N steps" from the
    orchestration itself.
    """
    oid = f"orch_uismoke_wf_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke flow {oid[-6:]}"))
    flow = f"orch_agent_{oid}"
    status = {**_status(True), "model": "ollama.default-model", "agents": {
        flow: {"name": "Flow", "status": "online", "can_see_images": True,
               "type": "orchestrator", "orchestration_id": oid},
    }, "active_agent_id": flow}
    ctx, page = await _chat_page(browser, "uismoke-wfheader", extra_init=SERVE_STATUS % json.dumps(status))
    try:
        header = ""
        for _ in range(30):
            # The composer's model slot since M2c (it was the page header's right side).
            header = await page.evaluate("() => document.querySelector('[data-composer-model]')?.innerText || ''")
            if re.search(r"workflow\s*·\s*3 steps", header, re.I):
                break
            await asyncio.sleep(0.2)
        read = await page.evaluate(
            f"() => performance.getEntriesByType('resource').map(e => e.name)"
            f".filter(n => n.endsWith('/api/orchestrations/{oid}')).length")
    finally:
        await ctx.close()
        drop_fixture(oid)
    bad = []
    if not re.search(r"workflow\s*·\s*3 steps", header, re.I):
        bad.append(f"the header reads {header!r}, not 'Workflow · 3 steps'")
    if "default-model" in header.lower():
        bad.append("the header still names the workspace default model")
    if read != 1:
        bad.append(f"the page read the workflow {read} times, not once")
    if bad:
        raise AssertionError("; ".join(bad))
    return "a workflow agent's header reads 'Workflow · 3 steps' from its orchestration, and names no model"


@check
async def check_seesattachments(browser) -> str:
    """An agent step can be marked "Sees attachments", and the mark goes when it stops being one (F313).

    `pass_images` had no control: chat's images could reach a workflow's step
    only through the builder's own bundle. The engine refuses it on any other
    type, so a step changed to LLM with it still set would be refused on save.
    """
    stamp = int(time.time())
    aid, oid = f"agent_uismoke_eyes_{stamp}", f"orch_uismoke_sees_{stamp}"
    api("/api/agents", "POST", _agent(aid, "UI smoke Eyes"))
    orch = laid_out_orchestration(oid, 1)
    orch["steps"][0].update({"type": "agent", "name": "Look", "agent_id": aid, "prompt_template": "Describe it."})
    api("/api/orchestrations", "POST", orch)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    sent: list[dict] = []
    answered: list[int] = []
    saving = lambda r: r.request.method == "POST" and r.url.endswith("/api/orchestrations")  # noqa: E731
    page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/orchestrations") else None)
    page.on("response", lambda r: answered.append(r.status) if saving(r) else None)
    try:
        await open_step(page, oid, "Look")
        box = page.get_by_role("checkbox", name="Sees attachments")
        try:
            await box.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("an agent step has no 'Sees attachments' box")
        await box.click()
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.5)
        ticked = (sent[-1]["steps"][0] if sent else {}).get("pass_images", "absent")
        await page.get_by_role("combobox", name="Step type").click()
        await page.get_by_role("option", name=re.compile(r"^LLM")).click()
        await page.get_by_role("button", name="Change type").click()
        await asyncio.sleep(0.5)
        await page.get_by_role("button", name=re.compile(r"^Save")).first.click()
        await asyncio.sleep(1.5)
        after = sent[-1]["steps"][0] if sent else {}
    finally:
        await ctx.close()
        drop_fixture(oid)
        api(f"/api/agents/{aid}", "DELETE")
    bad = []
    if ticked is not True:
        bad.append(f"ticking it saved pass_images={ticked!r}, not true")
    if after.get("type") != "llm":
        bad.append(f"the type change did not save (type {after.get('type')!r})")
    if "pass_images" in after:
        bad.append(f"the step saved as LLM still carries pass_images={after['pass_images']!r}")
    if answered and answered[-1] != 200:
        bad.append(f"saving it as LLM answered {answered[-1]}")
    if bad:
        raise AssertionError("; ".join(bad))
    return "ticking saves pass_images: true; changing the step to LLM drops it, and that save is 200"


# A 1×1 PNG: what a drop and a paste carry in the attach checks.
_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

DROP_AND_PASTE = """async (b64) => {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const file = () => new File([bytes], 'pic.png', {type: 'image/png'});
  const form = document.querySelector('main form');
  const dropped = new DataTransfer(); dropped.items.add(file());
  form.dispatchEvent(new DragEvent('drop', {dataTransfer: dropped, bubbles: true, cancelable: true}));
  const pasted = new DataTransfer(); pasted.items.add(file());
  form.querySelector('textarea').dispatchEvent(
    new ClipboardEvent('paste', {clipboardData: pasted, bubbles: true, cancelable: true}));
}"""

WORKFLOW_REFUSES = ("No step of this workflow takes attachments. Turn on Sees attachments on the agent step "
                    "that should read them.")


async def _try_attaching(browser, oid: str, sees: bool, problem: str | None) -> dict:
    flow = f"orch_agent_{oid}"
    status = {**_status(True), "agents": {
        flow: {"name": "Flow", "status": "online", "can_see_images": sees, "attach_problem": problem,
               "type": "orchestrator", "orchestration_id": oid},
    }, "active_agent_id": flow}
    ctx, page = await _chat_page(browser, f"uismoke-wfattach-{int(sees)}",
                                 extra_init=SERVE_STATUS % json.dumps(status))
    try:
        button = page.get_by_role("button", name="Attach images")
        await button.wait_for(timeout=10_000)
        seen = {"disabled": await button.is_disabled(), "title": await button.get_attribute("title")}
        await page.evaluate(DROP_AND_PASTE, _PNG_B64)
        await asyncio.sleep(1.0)
        seen["chips"] = await page.locator('main form [aria-label^="Remove "]').count()
        seen["said"] = " ".join((await page.locator('main [role="status"]').first.inner_text()).split())
        return seen
    finally:
        await ctx.close()


@check
async def check_workflowattach(browser) -> str:
    """A workflow agent's Attach follows its steps, and a drop or a paste is refused as the button is (F313).

    Attach followed the workspace default model, which no step runs on; a drop
    or a paste was never refused at all, only the button.
    """
    oid = f"orch_uismoke_wfattach_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke flow {oid[-6:]}"))
    try:
        takes = await _try_attaching(browser, oid, True, None)
        refused = await _try_attaching(browser, oid, False, WORKFLOW_REFUSES)
    finally:
        drop_fixture(oid)
    bad = []
    if takes["disabled"] or takes["chips"] != 2:
        bad.append(f"with a step that takes them, Attach was disabled={takes['disabled']} and a drop and a "
                   f"paste made {takes['chips']} chips, not 2 (the control)")
    if not refused["disabled"] or refused["title"] != WORKFLOW_REFUSES:
        bad.append(f"refused, the button was disabled={refused['disabled']} with title {refused['title']!r}")
    if refused["chips"]:
        bad.append(f"refused, a drop and a paste still made {refused['chips']} chips")
    if WORKFLOW_REFUSES not in refused["said"]:
        bad.append(f"refused, the composer said {refused['said']!r}, not why")
    if bad:
        raise AssertionError("; ".join(bad))
    return "a step that takes them: drop and paste attach; none: the button, a drop and a paste all say why"


@check
async def check_workflowready(browser) -> str:
    """A workflow agent that cannot answer says which step, not "Model not ready" (F313)."""
    oid = f"orch_uismoke_wfready_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke flow {oid[-6:]}"))
    flow = f"orch_agent_{oid}"
    problem = "Step 'Research' (gemini-2.5-flash): No Gemini key is saved."
    status = {**_status(False, problem), "agents": {
        flow: {"name": "Flow", "status": "online", "can_see_images": False, "attach_problem": WORKFLOW_REFUSES,
               "type": "orchestrator", "orchestration_id": oid},
    }, "active_agent_id": flow}
    ctx, page = await _chat_page(browser, "uismoke-wfready", extra_init=SERVE_STATUS % json.dumps(status))
    try:
        greeting = {}
        for _ in range(30):
            greeting = await page.evaluate(GREETING)
            if greeting.get("label") and "ready" in greeting["label"].lower():
                break
            await asyncio.sleep(0.2)
        shown = await _on_screen(page, "No Gemini key is saved.")
        # The model's dot sits by the composer since TABS1 (the tabs took the page header's place).
        dot = await page.evaluate("(p) => !!document.querySelector(`[data-model-dot][title=\"${p}\"]`)", problem)
    finally:
        await ctx.close()
        drop_fixture(oid)
    bad = []
    label = greeting.get("label") or ""
    if "A step is not ready" not in label:
        bad.append(f"the greeting reads {label!r}, not '… · A step is not ready'")
    if not shown:
        bad.append("the step's problem is not on the page")
    if not dot:
        bad.append("the model's dot does not carry the step's problem as its title")
    if bad:
        raise AssertionError("; ".join(bad))
    return "the greeting says a step is not ready, names it, and so does the model's dot"


def judged_question_orchestration(oid: str) -> dict:
    """A label written by one step, then a person asked with fields: the stress pass's shape (U65)."""
    return {
        "id": oid, "name": "ui-smoke judged question", "description": "Temporary fixture created by scripts/ui-smoke.py.",
        "trigger": "manual", "state_schema": {}, "entry_step_id": f"{oid}_judge",
        "steps": [
            {"id": f"{oid}_judge", "name": "Judge", "type": "print", "print_content": "pass",
             "next_step_id": f"{oid}_ask", "output_key": "category"},
            {"id": f"{oid}_ask", "name": "Review", "type": "human", "human_prompt": "Send the reply?",
             "human_fields": [{"name": "notes", "type": "textarea", "label": "Notes"},
                              {"name": "send", "type": "boolean", "label": "Send it"}],
             "next_step_id": None, "output_key": "review"},
        ],
    }


@check
async def check_humancontext(browser) -> str:
    """The question's card says what its context box holds, and names every field (U65).

    Under the title sat a box reading "pass" (the previous step's output, an
    evaluator's label) with nothing saying what it was. The question now names
    the state key it came from, and the box is captioned with it. The stress
    pass also reported the Notes field as unnamed; this check reads the name
    the browser computes for every field.
    """
    oid = f"orch_uismoke_ask_{int(time.time())}"
    api("/api/orchestrations", "POST", judged_question_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run now", exact=True).click()
        strip = page.get_by_role("button", name=re.compile("Waiting for your input"))
        await strip.wait_for(timeout=30_000)
        await strip.click()
        # Answered in the waiting step's setup card since M3a-2 (the dialog's place).
        dialog = page.locator(CARD_QUESTION)
        await dialog.wait_for(timeout=5_000)
        await asyncio.sleep(0.5)
        caption = await dialog.evaluate(
            "d => d.querySelector('figcaption')?.innerText || null")
        context = await dialog.evaluate(
            "d => (d.querySelector('figure') || d.querySelector('.bg-surface-2'))?.innerText || ''")
        fields = await ax_fields(page)
    finally:
        await ctx.close()
        drop_fixture(oid)
    # The same question answered in chat, where the stress pass typed into the
    # composer because it found no named field.
    asked = judged_question_orchestration(oid)["steps"][1]
    ctx, page = await _chat_page(browser, "uismoke-humancontext")
    try:
        await page.locator("main textarea").last.fill("start the review")
        await page.keyboard.press("Enter")
        await _stream_open(page, "post")
        await page.evaluate(f"() => window.__streams.post.push({json.dumps(_frame({
            'type': 'human_input_required', 'orch_step_id': asked['id'], 'run_id': 'run_uismoke_ask',
            'prompt': asked['human_prompt'], 'fields': asked['human_fields'],
            'agent_context': 'pass', 'context_key': 'category'}))})")
        await page.get_by_text("Human Input Required").first.wait_for(timeout=10_000)
        await asyncio.sleep(0.5)
        chat_fields = await ax_fields(page)
    finally:
        await ctx.close()
    bad = []
    chat_names = [f["name"] for f in chat_fields if f["role"] == "textbox"]
    if "Notes" not in chat_names:
        bad.append(f"chat's Notes field is not named Notes (text fields: {chat_names})")
    if caption != "From category":
        bad.append(f"the context box is captioned {caption!r}, not 'From category'")
    if "pass" not in context:
        bad.append(f"the context box reads {context!r}, not the judge's 'pass'")
    names = [f["name"] for f in fields if f["role"] == "textbox"]
    if "Notes" not in names:
        bad.append(f"the Notes field is not named Notes (text fields: {names})")
    if bad:
        raise AssertionError("; ".join(bad))
    return "the context box reads 'From category' over 'pass', and the Notes field is named"


#: A tab in the background that has granted browser notifications, with the OS
#: popup recorded instead of shown.
OS_NOTIFICATIONS = """
localStorage.setItem('synapseBrowserNotifs', '1');
Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
window.__os = [];
window.Notification = class {
  static permission = 'granted';
  static requestPermission() { return Promise.resolve('granted'); }
  constructor(title, opts) { window.__os.push({title, body: (opts || {}).body}); }
  close() {}
};
"""


@check
async def check_bellplain(browser) -> str:
    """The bell and the OS notification show a question's words, not its markdown.

    A Human step's prompt is markdown and becomes the notification's body; the
    bell printed it as typed ("Approve **vault_write** …"), and so did the OS
    popup. Both bell rows (a question, and an approval with its buttons) and
    the popup read it as plain text now.
    """
    body = "Approve **vault_write** for `notes.md`?\n\n- [the draft](https://example.com/d)"
    words = "Approve vault_write for notes.md? the draft"
    items = [
        {"id": 900001, "ts": time.time(), "kind": "human_input", "title": "Flow needs your input",
         "body": body, "run_id": "run_uismoke_bell", "data": {}, "resolved": False},
        {"id": 900002, "ts": time.time(), "kind": "tool_approval", "title": "An agent asks to run a tool",
         "body": body, "run_id": None, "data": {"tool_name": "vault_write", "tool_args": {"path": "notes.md"}},
         "resolved": False},
    ]
    live = {"id": 900003, "ts": time.time(), "kind": "human_input", "title": "Flow needs you again",
            "body": body, "run_id": "run_uismoke_bell2", "data": {}, "resolved": False}
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(OS_NOTIFICATIONS)

    async def listing(route):
        await route.fulfill(json={"notifications": items, "last_id": 900002})

    async def stream(route):
        await route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                            body=f"data: {json.dumps(live)}\n\n")

    await ctx.route("**/api/notifications?after=0*", listing)
    await ctx.route("**/api/notifications/stream*", stream)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        await page.locator('button[title="Notifications"]').click()
        panel = page.get_by_text("Browser notifications when tab is in background") \
            .locator("xpath=ancestor::div[contains(@class, 'w-80')][1]")
        await panel.wait_for(timeout=5_000)
        shown = await panel.inner_text()
        popups = await page.evaluate("() => window.__os")
    finally:
        await ctx.close()
    bad = []
    for mark in ("**", "`", "](", "- ["):
        if mark in shown:
            bad.append(f"the bell shows the markdown mark {mark!r}")
    if shown.count(words) < 2:
        bad.append(f"the bell does not read {words!r} on both rows")
    if not popups:
        bad.append("no OS notification for the live question")
    elif popups[0]["body"] != words:
        bad.append(f"the OS notification reads {popups[0]['body']!r}")
    if bad:
        raise AssertionError("; ".join(bad))
    return "both bell rows and the OS popup read the question's words, with no markdown marks"


@check
async def check_evaluatoronce(browser) -> str:
    """The run log prints an evaluator's decision once (U66).

    The engine sends two events for every evaluator — the step's own
    `evaluator_decision`, then the router's `routing_decision` — and the log
    printed both ("Evaluator chose pass (…)" and "Evaluator routed → pass (…)").
    The step's line stays: it is there even when the label is not in the route
    map and the router says nothing. An if/else's and a switch's routing lines,
    and an archived run's (no `step_type`), are unchanged.
    """
    oid = f"orch_uismoke_eval_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke eval {oid[-6:]}"))
    events = [
        {"type": "evaluator_decision", "orch_step_id": "judge", "tool_name": "route_pass",
         "arguments": {"label": "pass", "reasoning": "the draft answers the question"}},
        {"type": "routing_decision", "step_type": "evaluator", "orch_step_id": "judge", "decision": "pass",
         "target_step_id": "send", "reasoning": "the draft answers the question"},
        {"type": "evaluator_decision", "orch_step_id": "judge2", "tool_name": "route_other",
         "arguments": {"label": "other", "reasoning": "not in the map"}},
        {"type": "routing_decision", "step_type": "if_else", "orch_step_id": "gate", "decision": True,
         "target_step_id": "yes"},
        {"type": "routing_decision", "orch_step_id": "old", "decision": "archived", "reasoning": "from before"},
    ]
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(RUNLOG_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run now", exact=True).click()
        for _ in range(30):
            if await page.evaluate("() => !!window.__run.raw"):
                break
            await asyncio.sleep(0.1)
        for e in events:
            await page.evaluate(f"() => window.__run.raw({json.dumps(e)})")
        await asyncio.sleep(1.0)
        text = await page.evaluate("() => document.body.innerText")
    finally:
        await ctx.close()
        drop_fixture(oid)
    bad = []
    chose = text.count("Evaluator chose pass (the draft answers the question)")
    routed = text.count("Evaluator routed → pass")
    if chose != 1 or routed:
        bad.append(f"the evaluator's decision is printed {chose + routed} times "
                   f"({chose} 'chose', {routed} 'routed'), not once")
    if "Evaluator chose other (not in the map)" not in text:
        bad.append("a label the route map does not hold is not printed")
    if "Branch: true → yes" not in text:
        bad.append("an if/else's routing line is gone")
    if "Evaluator routed → archived (from before)" not in text:
        bad.append("an archived run's routing line (no step_type) is gone")
    if bad:
        raise AssertionError("; ".join(bad))
    return "one line per evaluator decision; if/else and archived routing lines unchanged"


@check
async def check_reopenpanels(browser) -> str:
    """A reopened answer shows its Thinking and Reasoning panels again (U60).

    A reopened chat restored the text and, since U60's first step, the tools
    used — but not what the model reasoned or thought (0 of 14 turns in the
    stress pass). The saved turn now keeps both (`reasoning`, `thoughts`), and
    the page draws them as it does live: the last thought goes when it only
    repeats the answer, and a thought that is a tool call reads "→ Called".
    """
    history = {
        "turns": [{
            "user": "what day is it?", "assistant": "It is today.", "tools": ["get_datetime"], "timestamp": "",
            "calls": [{"tool": "get_datetime", "args": {}, "result": "2026-09-30"}],
            "reasoning": ["I need the date.", "The tool gave the date."],
            "thoughts": ['{"tool": "get_datetime", "arguments": {}}', "It is today."],
        }],
        "pending_approval": None, "running": None,
    }
    ctx, page = await _chat_page(browser, "uismoke-reopenpanels", history)
    try:
        if not await _on_screen(page, "It is today."):
            raise AssertionError("the reopened answer never appeared")
        thinking = page.get_by_role("button", name=re.compile(r"^Thinking"))
        reasoning = page.get_by_role("button", name=re.compile(r"Reasoning"))
        found = (await thinking.count(), await reasoning.count())
        labels = [await thinking.first.inner_text() if found[0] else None,
                  await reasoning.first.inner_text() if found[1] else None]
        if found[0]:
            await thinking.first.click()
        if found[1]:
            await reasoning.first.click()
        await asyncio.sleep(0.5)
        text = await page.evaluate("() => document.querySelector('main').innerText")
    finally:
        await ctx.close()
    bad = []
    if not found[0]:
        bad.append("no Thinking panel on the reopened answer")
    elif "(2 turns)" not in labels[0].lower():
        bad.append(f"the Thinking panel reads {labels[0]!r}, not two turns")
    elif "I need the date." not in text or "The tool gave the date." not in text:
        bad.append("the Thinking panel does not show what was reasoned")
    if not found[1]:
        bad.append("no Reasoning panel on the reopened answer")
    elif "(1 turn)" not in labels[1].lower():
        bad.append(f"the Reasoning panel reads {labels[1]!r} — the thought that repeats the answer is not dropped")
    elif "→ Called: get_datetime" not in text:
        bad.append("a tool-call thought does not read '→ Called'")
    if bad:
        raise AssertionError("; ".join(bad))
    return "a reopened answer draws its Thinking (2) and Reasoning (1, the echo dropped) panels"


@check
async def check_mcpadd(browser) -> str:
    """Adding an MCP server says what happened, in the server's words (U61).

    Adding a public no-auth remote took 15 s and answered "Config saved. Use
    Retry to reconnect." although it connected moments later. The add now says
    which of three things happened (E1, 3514382): connected; still connecting,
    which the screen now watches until the row is Active; or not connected,
    with the reason. Both answers are stubbed here, so it needs no server.
    """
    base = {"server_type": "remote", "token_set": False, "command": "", "args": [], "env": {}}
    connecting = {**base, "name": "smoke_conn", "label": "Smoke conn", "url": "http://127.0.0.2:9/a",
                  "status": "connecting"}
    dead = {**base, "name": "smoke_dead", "label": "Smoke dead", "url": "http://127.0.0.2:9/b",
            "status": "disconnected"}
    answers = {
        "smoke_conn": {"status": "saved", "connected": False, "config": connecting,
                       "message": "Saved — still connecting. It will show as connected when it does."},
        "smoke_dead": {"status": "saved", "connected": False, "config": dead,
                       "message": "Saved, but it did not connect: All connection attempts failed. "
                                  "Retry once it is reachable."},
    }
    listed = {"servers": []}

    async def servers(route):
        req = route.request
        if req.method == "POST":
            name = json.loads(req.post_data or "{}").get("name")
            answer = answers[name]
            listed["servers"] = [s for s in listed["servers"] if s["name"] != name] + [
                {**answer["config"], "status": "connected"} if name == "smoke_conn" else answer["config"]]
            return await route.fulfill(json=answer)
        await route.fulfill(json=listed["servers"])

    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/mcp/servers", servers)
    page = await ctx.new_page()
    notes: list[str] = []
    try:
        await page.goto(f"{APP}/apps", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)

        async def add(name: str, url: str) -> str:
            # A saved add resets the form to its default type (stdio).
            await page.get_by_role("button", name=re.compile(r"By web address \(URL\)")).click()
            await page.get_by_label("Short id (no spaces)").fill(name)
            await page.get_by_label("Server URL").fill(url)
            await page.get_by_role("button", name="Connect", exact=True).click()
            await asyncio.sleep(1.0)
            return await page.evaluate("() => document.querySelector('main').innerText")

        first = await add("smoke_conn", "http://127.0.0.2:9/a")
        notes.append(first)
        became = False
        for _ in range(16):          # the screen polls every 5 s
            await asyncio.sleep(0.5)
            text = await page.evaluate("() => document.querySelector('main').innerText")
            if "smoke_conn connected" in text:
                became = True
                break
        second = await add("smoke_dead", "http://127.0.0.2:9/b")
    finally:
        await ctx.close()
    bad = []
    if "still connecting" not in first:
        bad.append("a server still connecting is not said to be connecting")
    if "Use Retry" in first:
        bad.append("a server still connecting is told to Retry")
    if not became:
        bad.append("the screen never saw the still-connecting server connect")
    if "did not connect: All connection attempts failed" not in second:
        bad.append("a server that did not connect does not say why")
    if bad:
        raise AssertionError("; ".join(bad))
    return "still connecting is said and watched until connected; a failure says why; no stray 'Use Retry'"


# ── F101: the states `contrast` cannot open ─────────────────────────────────

def _answer(body, status: int = 200, methods: tuple[str, ...] = ("GET",),
            content_type: str = "application/json", hold: float = 0.0):
    """A route that answers `methods` itself and lets anything else through."""
    async def fulfil(route):
        if route.request.method not in methods:
            return await route.fallback()       # to an earlier route for this URL, or the network
        if hold:
            await asyncio.sleep(hold)
        text = body if isinstance(body, str) else json.dumps(body)
        await route.fulfill(status=status, content_type=content_type, body=text)
    return fulfil


def _held_after_first(first, then_hold: float):
    """GET answered at once the first time, then held: keeps a 'saved' message up."""
    seen = {"n": 0}

    async def fulfil(route):
        if route.request.method != "GET":
            return await route.fallback()
        seen["n"] += 1
        if seen["n"] > 1:
            await asyncio.sleep(then_hold)
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(first))
    return fulfil


_NOW = "2026-09-30T10:00:00Z"
_SMOKE_ORCH = {"id": "orch_smoke_c", "name": "Smoke contrast flow", "description": "", "trigger": "manual",
               "state_schema": {}, "entry_step_id": "s1", "max_total_turns": 100, "timeout_minutes": 30,
               "steps": [{"id": "s1", "name": "Extract", "type": "extract_json", "next_step_id": "s_end",
                          "output_key": "out"},
                         {"id": "s_end", "name": "End", "type": "end", "next_step_id": None}]}
_SMOKE_RUNS = [
    {"run_id": "run_smoke_fail", "orchestration_id": "orch_smoke_c", "status": "failed", "started_at": _NOW,
     "ended_at": _NOW, "current_step_id": None, "steps_completed": 1, "last_step_name": "Extract",
     "waiting_for_human": False, "total_cost_usd": 0.0012, "session_id": None, "score": 8.2,
     "score_regression": True},
    {"run_id": "run_smoke_low", "orchestration_id": "orch_smoke_c", "status": "completed", "started_at": _NOW,
     "ended_at": _NOW, "steps_completed": 2, "total_cost_usd": 0, "session_id": None, "score": 3.1,
     "score_regression": False}]
_SMOKE_RUN_STREAM = "".join([
    _frame({"type": "step_start", "orch_step_id": "s1", "step_name": "Extract", "step_type": "extract_json"}, "1"),
    _frame({"type": "final", "orch_step_id": "s1",
            "response": '{"name": "Ada", "age": 36, "active": true, "team": null}'}, "2"),
    _frame({"type": "step_complete", "orch_step_id": "s1", "step_name": "Extract", "duration_seconds": 0.4}, "3"),
    _frame({"type": "stream_complete"})])
_SMOKE_RUN_ROUTES = [
    ("**/api/orchestrations", _answer([_SMOKE_ORCH])),
    ("**/api/orchestrations/runs", _answer(_SMOKE_RUNS)),
    ("**/api/orchestrations/runs/run_smoke_fail", _answer(
        {"run_id": "run_smoke_fail", "orchestration_id": "orch_smoke_c", "status": "failed",
         "step_history": [], "waiting_for_human": False})),
    ("**/api/orchestrations/runs/run_smoke_fail/events/stream*",
     _answer(_SMOKE_RUN_STREAM, content_type="text/event-stream")),
    ("**/api/orchestrations/runs/run_smoke_fail/children", _answer(
        [{"run_id": "run_smoke_fail__c1", "orchestration_id": "orch_smoke_child", "status": "failed",
          "steps_completed": 1, "total_cost_usd": 0, "error": "Child step failed: API returned 500"}])),
]
_SMOKE_USAGE = [
    ("**/api/usage/summary", _answer({
        "total_cost": 0.05, "total_input_tokens": 60000, "total_output_tokens": 2000, "total_tokens": 62000,
        "total_requests": 10,
        "by_model": [{"model": "anthropic.claude-3-haiku", "provider": "bedrock", "requests": 10,
                      "input_tokens": 60000, "output_tokens": 2000, "total_tokens": 62000, "estimated_cost": 0.05}],
        "by_session": [{"session_id": "sess_smoke_usage", "agent_id": "agent_smoke", "requests": 2,
                        "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200, "context_chars": 8192,
                        "estimated_cost": 0.001, "models_used": ["anthropic.claude-3-haiku"], "first_ts": _NOW,
                        "last_ts": _NOW, "source": "chat"}],
        "by_schedule": []})),
    ("**/api/usage/pricing", _answer({"anthropic.claude-3-haiku": {
        "provider": "bedrock", "input_per_1m": 0.25, "output_per_1m": 1.25, "_new": True}})),
    ("**/api/usage/cache_summary", _answer({
        "total_estimated_savings": 0.0123, "total_requests": 10, "total_response_cache_hits": 2,
        "response_cache_hit_rate": 0.2, "total_cache_read_tokens": 50000, "total_cache_write_tokens": 12000,
        "by_model": [{"model": "claude-sonnet-4", "requests": 6, "cache_read_tokens": 50000,
                      "cache_write_tokens": 12000, "estimated_savings": 0.0123, "estimated_cost": 0.05,
                      "response_cache_hits": 2}],
        "by_run": [{"run_id": "run_smoke_cache", "requests": 4, "cache_read_tokens": 20000, "cache_write_tokens": 0,
                    "estimated_savings": 0.005, "estimated_cost": 0.02, "response_cache_hits": 0}]})),
]


# ── M2g: Usage and Settings on the redesign ────────────────────────────────────

#: A raw palette utility in a class list: the redesign puts every colour on a token (UX1, M2g).
_RAW_COLOUR = re.compile(r"\b(?:bg|text|border|ring|from|to|via|fill|stroke)-(?:zinc|gray|slate|neutral|stone|red|green|"
                         r"emerald|amber|yellow|blue|sky|cyan|orange|purple|violet|indigo|teal|lime|rose|pink|fuchsia)-\d|"
                         r"\b(?:bg|text)-(?:white|black)\b")


def _usage_sessions(n: int) -> list:
    return [{"session_id": f"sess_smoke_{i:02d}", "agent_id": "agent_smoke", "requests": 1, "input_tokens": 10,
             "output_tokens": 5, "total_tokens": 15, "context_chars": 100, "estimated_cost": 0.0001,
             "models_used": ["anthropic.claude-3-haiku"], "first_ts": _NOW, "last_ts": _NOW, "source": "chat"}
            for i in range(n)]


async def _usage_page(browser, routes, theme="light"):
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
    for pattern, handler in routes:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    await page.goto(f"{APP}/usage", wait_until="domcontentloaded")
    await page.locator("[data-usage-hero]").wait_for(timeout=30_000)
    await page.wait_for_timeout(800)
    return ctx, page


@check
async def check_usage_hero(browser) -> str:
    """Usage leads with one figure (UX1, M2g): Total cost, then the calls and tokens as one strip, not four tiles, in
    plain words (GUIDE1, F921: "Model calls", "Words in", "Words out"); its
    two actions sit beside the figure and Clear logs asks first in the product's own dialog, sending nothing on Cancel."""
    deletes: list[str] = []
    ctx, page = await _usage_page(browser, _SMOKE_USAGE)
    try:
        page.on("request", lambda r: deletes.append(r.url) if r.method == "DELETE" else None)
        hero = " ".join((await page.locator("[data-usage-hero]").inner_text()).split())
        if not hero.startswith("Total cost $0.05"):
            raise AssertionError(f"the hero says {hero!r}")
        strip = " ".join((await page.locator("dl[aria-label='Usage so far']").inner_text()).split())
        for want in ("Model calls 10", "Words in 60.0K input tokens", "Words out 2.0K output tokens"):
            if want not in strip:
                raise AssertionError(f"the strip says {strip!r}, not {want!r}")
        await page.get_by_role("button", name="Refresh").wait_for(timeout=5_000)
        await page.get_by_role("button", name="Clear logs").click()
        dialog = page.get_by_role("dialog", name="Delete all usage logs?")
        await dialog.wait_for(timeout=5_000)
        await dialog.get_by_role("button", name="Cancel").click()
        await page.wait_for_timeout(500)
        if deletes:
            raise AssertionError(f"Cancel still sent {deletes}")
    finally:
        await ctx.close()
    return "Total cost, a strip of model calls and words in and out, Clear logs asks and Cancel sends nothing"


@check
async def check_usage_bars(browser) -> str:
    """Cost by model is one hue (`--machine`) with the figure beside each bar (M2g): a provider's own colour meant
    nothing but "a different provider" (UX1). An unpriced model keeps "No price · add one" leading to #pricing."""
    routes = [r for r in _SMOKE_USAGE if r[0] != "**/api/usage/summary"] + [("**/api/usage/summary", _answer({
        **USAGE_STUB, "by_model": USAGE_STUB["by_model"] + [
            {"model": "smoke-unpriced", "provider": "openai_compatible", "requests": 1, "input_tokens": 1,
             "output_tokens": 1, "total_tokens": 2, "estimated_cost": 0, "priced": False}]}))]
    ctx, page = await _usage_page(browser, routes)
    try:
        rows = page.locator("ul[aria-label='Cost by model'] [data-bar-row]")
        if await rows.count() != 3:
            raise AssertionError(f"{await rows.count()} bars for 3 models")
        fills = await page.evaluate("""() => {
            const m = getComputedStyle(document.documentElement).getPropertyValue('--machine').trim();
            const probe = document.createElement('i'); probe.style.color = m; document.body.append(probe);
            const want = getComputedStyle(probe).color; probe.remove();
            return [...document.querySelectorAll("ul[aria-label='Cost by model'] [data-bar-row] > span[aria-hidden] > span")]
                .map(e => getComputedStyle(e).backgroundColor === want);
        }""")
        if not fills or not all(fills):
            raise AssertionError(f"a bar is not the machine colour: {fills}")
        said = " ".join((await rows.nth(0).inner_text()).split())
        if "$0.003829" not in said:
            raise AssertionError(f"the first bar says {said!r}")
        link = rows.nth(2).get_by_role("link", name="add one")
        if await link.get_attribute("href") != "#pricing":
            raise AssertionError("the unpriced model's 'add one' does not lead to #pricing")
    finally:
        await ctx.close()
    return "three bars on --machine, each with its figure; the unpriced one links #pricing"


@check
async def check_usage_sessions(browser) -> str:
    """Sessions are the runs list's rows (M2g): a row opens in place to its per-turn table, and Load more shows the
    next 15."""
    routes = [r for r in _SMOKE_USAGE if r[0] != "**/api/usage/summary"] + [
        ("**/api/usage/summary", _answer({**USAGE_STUB, "by_session": _usage_sessions(20)})),
        ("**/api/usage/logs*", _answer({"logs": []}))]
    ctx, page = await _usage_page(browser, routes)
    try:
        rows = page.locator("[data-usage-session]")
        if await rows.count() != 15:
            raise AssertionError(f"{await rows.count()} rows before Load more, not 15")
        row = page.get_by_role("button", name="sess_smoke_00")
        await row.click()
        if await row.get_attribute("aria-expanded") != "true":
            raise AssertionError("the row did not open")
        await page.get_by_text("Per-turn breakdown").first.wait_for(timeout=5_000)
        await page.get_by_role("button", name="Load more (5 remaining)").click()
        await page.wait_for_function("() => document.querySelectorAll('[data-usage-session]').length === 20",
                                     timeout=5_000)
    finally:
        await ctx.close()
    return "15 rows, a row opens to its turns, Load more shows the other 5"


#: Every Settings tab M2g redesigned, and Usage.
_M2G_PAGES = ("/usage", "/settings/general", "/settings/models", "/settings/personal_details", "/settings/variables",
              "/settings/memory", "/settings/workspace", "/settings/repos", "/settings/db_configs",
              "/settings/api_keys", "/settings/import_export", "/settings/scale", "/settings/support",
              "/settings/datalab")


@check
async def check_settings_plain(browser) -> str:
    """Usage and every Settings tab are on tokens and plain sections (M2g): no raw palette utility in the page, and a
    tab's groups are headed sections on the page, not boxes."""
    raw: list[str] = []
    boxed: list[str] = []
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        for path in _M2G_PAGES:
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await page.locator("main h1").first.wait_for(timeout=30_000)
            await page.wait_for_timeout(SETTLE * 1000)
            classes = await page.evaluate(
                "() => [...document.querySelectorAll('main *')].map(e => e.getAttribute('class') || '')")
            hits = sorted({m.group(0) for c in classes for m in [_RAW_COLOUR.search(c)] if m})
            if hits:
                raw.append(f"{path}: {hits[:6]}")
            heads = await page.locator("main section > header h2").count()
            if path != "/settings/import_export" and heads == 0:
                boxed.append(path)
    finally:
        await ctx.close()
    problems = []
    if raw:
        problems.append("raw colour — " + " | ".join(raw))
    if boxed:
        problems.append(f"no plain section heads on {boxed}")
    if problems:
        raise AssertionError("; ".join(problems))
    return f"{len(_M2G_PAGES)} pages on tokens with plain section heads"


@check
async def check_general_switches(browser) -> str:
    """General's settings that are on or off are the kit's switch, each named as before (M2g), and a switch moves
    when pressed; Settings' collapse button sits on its first group's line (Naveen at review)."""
    names = ("Knowledge", "Large response handling", "Auto context compaction", "Database write access",
             "Code repository indexing", "Run Transform steps on the host", "Require login")
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/general", wait_until="domcontentloaded")
        for name in names:
            await page.get_by_role("switch", name=name, exact=True).wait_for(timeout=30_000)
        sw = page.get_by_role("switch", name="Database write access", exact=True)
        was = await sw.get_attribute("aria-checked")
        await sw.click()
        if await sw.get_attribute("aria-checked") == was:
            raise AssertionError("Database write access did not move")
        nav = page.locator('nav[aria-label="Settings"]')
        btn = nav.get_by_role("button", name="Collapse settings menu")
        head = nav.get_by_text("Workspace", exact=True)
        b, h = await btn.bounding_box(), await head.bounding_box()
        if abs((b["y"] + b["height"] / 2) - (h["y"] + h["height"] / 2)) > 6:
            raise AssertionError(f"the collapse button is not on Workspace's line ({b['y']:.0f} vs {h['y']:.0f})")
    finally:
        await ctx.close()
    return f"{len(names)} switches by name, one moves; the collapse button on Workspace's line"


@check
async def check_rerankers(browser) -> str:
    """With Knowledge on, Models lists the rerankers as provider cards (Naveen, M2g, as cloud): Cohere, Voyage AI,
    Jina AI and a rerank server, each opening to its key field."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/settings", lambda route: route.continue_() if route.request.method != "GET" else
                    _patch_json(route, {"knowledge_enabled": True}))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/models", wait_until="domcontentloaded")
        for name in ("Cohere", "Voyage AI", "Jina AI", "Rerank server (TEI or Infinity)"):
            await page.locator(f'[data-provider-card="{name}"]').wait_for(timeout=30_000)
        await page.locator('[data-provider-card="Cohere"] button').first.click()
        await page.get_by_label("Cohere API key").wait_for(timeout=5_000)
        await page.locator('[data-provider-card="Rerank server (TEI or Infinity)"] button').first.click()
        await page.get_by_label("Rerank server address").wait_for(timeout=5_000)
    finally:
        await ctx.close()
    return "four reranker cards; Cohere opens to its key, the server to its address"


#: `GET /api/models/rerank` as the pickers read it (BF9, D293): Cohere's key lists two, Voyage's was refused.
_SMOKE_RERANK_LISTINGS = {"providers": [
    {"provider": "cohere", "label": "Cohere", "key": "cohere_key", "error": None, "note": None,
     "models": [{"id": "cohere.rerank-v3.5", "label": "rerank-v3.5"},
                {"id": "cohere.rerank-multilingual-v3.0", "label": "rerank-multilingual-v3.0"}]},
    {"provider": "voyage", "label": "Voyage AI", "key": "voyage_key", "error": "Voyage AI refused this key (401)",
     "note": None, "models": []},
]}


@check
async def check_rerankpicker(browser) -> str:
    """The rerank pickers list what the saved keys can use, and each reranker card says so (F702, F703).

    Settings → Models' Rerank model and a base's settings drawer were free text over a hard-coded list (Voyage's
    rerank-2.5 when rerank-3 was current), so a model no key could call was saved and failed only in a search's
    trace. Now both are pickers of `/api/models/rerank`'s models, under their providers; a refused key lists
    nothing, says so on its card, and dims it. The list is answered in the browser."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/settings", lambda route: route.continue_() if route.request.method != "GET" else
                    _patch_json(route, {"knowledge_enabled": True, "cohere_key_set": True, "cohere_key_hint": "1234",
                                        "voyage_key_set": True, "voyage_key_hint": "5678"}))
    await ctx.route("**/api/models/rerank*", _answer(_SMOKE_RERANK_LISTINGS))
    page = await ctx.new_page()
    bad: list[str] = []
    try:
        await page.goto(f"{APP}/settings/models", wait_until="domcontentloaded")
        cohere = page.locator('[data-provider-card="Cohere"]')
        await cohere.wait_for(timeout=30_000)
        await page.wait_for_timeout(1500)
        if "2 rerank models" not in await cohere.inner_text():
            bad.append(f"the Cohere card does not count its models: {await cohere.inner_text()!r}")
        voyage = await page.locator('[data-provider-card="Voyage AI"]').inner_text()
        if "Voyage AI refused this key (401)" not in voyage:
            bad.append(f"the Voyage card does not say its key was refused: {voyage!r}")
        await page.get_by_role("button", name="Rerank model", exact=True).click()
        offered = await page.get_by_role("option").evaluate_all("els => els.map(e => e.textContent.trim())")
        if offered != ["None: no rerank unless a base names one", "rerank-v3.5", "rerank-multilingual-v3.0"]:
            bad.append(f"Settings → Models' rerank picker offers {offered}")
        await page.keyboard.press("Escape")

        for pattern, handler in _SMOKE_KB_ROUTES:
            await page.route(pattern, handler)
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Settings", exact=True).click()
        drawer = page.get_by_role("dialog", name=re.compile("^Settings for"))
        await drawer.wait_for(timeout=10_000)
        await page.wait_for_timeout(1500)
        await drawer.get_by_role("button", name="Rerank model", exact=True).click()
        offered = await page.get_by_role("option").evaluate_all("els => els.map(e => e.textContent.trim())")
        if offered[1:] != ["rerank-v3.5", "rerank-multilingual-v3.0"] or not offered[0].startswith("Workspace default"):
            bad.append(f"the drawer's rerank picker offers {offered}")
        if bad:
            raise AssertionError("; ".join(bad))
        return ("both rerank pickers offer the Cohere key's two models and nothing of Voyage's refused key; "
                "the cards count and say so")
    finally:
        await ctx.close()


@check
async def check_hosted_providers(browser) -> str:
    """Settings → Models on cloud's card (PROV1, D245), Providers first (GUIDE1, F912; Naveen 2026-10-23: a new
    workspace has nothing for Defaults to pick until a key is in): Providers in four groups, then Defaults and
    Knowledge, the form's Save on the right at their foot. A card with no key says "Add key" before it is opened. A provider card saves and removes its own key and nothing else:
    Groq's Save posts only `groq_key`, its Remove asks first and posts only `clear_secrets`. Both POSTs are answered
    here, so nothing is stored."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    posts: list[dict] = []

    async def _settings(route):
        if route.request.method != "POST":
            return await route.fallback()
        body = json.loads(route.request.post_data or "{}")
        posts.append(body)
        saved = "groq_key" in body
        await route.fulfill(json={"groq_key": "", "groq_key_set": saved, "groq_key_hint": "abcd" if saved else ""})

    await ctx.route("**/api/settings", _settings)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/models", wait_until="domcontentloaded")
        groups = page.locator("[data-provider-group]")
        await groups.first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)   # the page's first loads redraw the cards; type after they land
        names = [await g.get_attribute("data-provider-group") for g in await groups.all()]
        if names != ["Model makers", "Hosts", "Your own machine or server", "CLI models"]:
            raise AssertionError(f"the provider groups are {names}")
        heads = await page.evaluate("() => [...document.querySelectorAll('main h2')].map(h => h.textContent.trim())")
        if heads[:2] != ["Providers", "Defaults"]:
            raise AssertionError(f"the page does not lead with Providers, then Defaults: {heads}")
        save = page.get_by_role("button", name="Save changes", exact=True)
        defaults_head = page.locator("main h2", has_text="Defaults")
        sb, db = await save.bounding_box(), await defaults_head.bounding_box()
        column = await page.locator("[data-provider-group]").first.bounding_box()
        if not sb or not db or sb["y"] < db["y"]:
            raise AssertionError("Save changes is not at the foot of Defaults")
        if abs((sb["x"] + sb["width"]) - (column["x"] + column["width"])) > 4:
            raise AssertionError("Save changes is not on the right of the form's column")
        for name in ("Mistral", "Moonshot (Kimi)", "Qwen", "Z.ai (GLM)", "MiniMax", "OpenRouter", "Groq", "Cerebras",
                     "Together AI", "Fireworks", "NVIDIA"):
            card = page.locator(f'[data-provider-card="{name}"]')
            if await card.count() != 1:
                raise AssertionError(f"no {name} card")
            if await card.locator("img").count() != 1:
                raise AssertionError(f"{name} has no logo")
        groq = page.locator('[data-provider-card="Groq"]')
        if not await groq.get_by_text("Add key", exact=True).count():
            raise AssertionError("Groq, with no key, does not say \"Add key\"")
        await groq.locator("button").first.click()
        await groq.get_by_label("Groq API key").fill("gsk_smoke_not_a_key")
        await groq.get_by_role("button", name="Save", exact=True).click()
        await groq.get_by_text("A key is stored").wait_for(timeout=10_000)
        await groq.get_by_role("button", name="Remove the Groq key").click()
        await page.get_by_role("dialog").get_by_role("button", name="Remove").click()
        await asyncio.sleep(1.5)
    finally:
        await ctx.close()
    if [sorted(p) for p in posts] != [["groq_key"], ["clear_secrets"]] or posts[1].get("clear_secrets") != ["groq_key"]:
        raise AssertionError(f"the card's POSTs were {[sorted(p) for p in posts]}")
    return ("Providers first in four groups, then Defaults with Save on the right at its foot, eleven hosted cards "
            "with logos; Groq says Add key, and saves and removes only its own key")


async def _patch_json(route, extra: dict):
    """Answer with the real response, `extra` merged in."""
    res = await route.fetch()
    body = await res.json()
    await route.fulfill(response=res, json={**body, **extra})

_SMOKE_BUNDLE = {
    "synapse_export": True, "version": "1.0", "exported_at": _NOW, "has_python_tools": False,
    "orchestrations": [{"id": "orch_smoke_imp", "name": "Smoke import flow", "description": "d",
                        "steps": [{"id": "s1", "name": "Draft", "type": "agent", "agent_id": "agent_smoke_mail",
                                   "model": "gpt-4o-mini"}]}],
    "agents": [{"id": "agent_smoke_mail", "name": "Smoke mail agent", "description": "d",
                "tools": ["search_gmail_messages"], "system_prompt": "p", "model": "claude-sonnet-4"}],
    "custom_tools": [], "mcp_servers": []}
_SMOKE_IMPORT_ROUTES = [("**/api/config", _answer({"has_credentials": False, "is_connected": False}))]


async def _open_import(page):
    await page.get_by_role("button", name=re.compile(r"^Import$")).first.click()
    await page.locator("input[type=file]").first.set_input_files(files=[{
        "name": "export.json", "mimeType": "application/json", "buffer": json.dumps(_SMOKE_BUNDLE).encode()}])
    await page.get_by_text("Models Used in This Bundle").first.wait_for(timeout=15_000)
    show = page.get_by_role("button", name=re.compile(r"^Show$"))
    if await show.count():
        await show.first.click()


async def _import_now(page):
    await _open_import(page)
    await page.get_by_role("button", name=re.compile(r"^Import Now")).first.click()


async def _send_and_fail(page):
    await page.locator("main textarea").last.fill("a question that fails")
    await page.keyboard.press("Enter")
    await _stream_open(page, "post")
    await page.evaluate("(f) => window.__streams.post.push(f)", _frame(
        {"type": "error", "message": "Provider returned 429: rate limited", "kind": "model"}, "e1"))


async def _python_test(page):
    await page.locator('[data-tool-row] > button:first-child', has_text="Smoke python tool").first.click()
    await page.get_by_role("button", name="Run", exact=True).click()


async def _openapi_preview(page):
    await page.get_by_role("button", name=re.compile("Import OpenAPI")).first.click()
    await page.get_by_placeholder(re.compile(r'^\{"openapi"')).fill('{"openapi": "3.0.0", "paths": {}}')
    await page.get_by_role("button", name=re.compile("Preview tools")).click()


async def _paste_credentials(page):
    await page.get_by_role("button", name=re.compile("re-upload credentials", re.I)).first.click()
    await page.get_by_placeholder(re.compile(r'^\{"installed"')).fill(
        '{"installed": {"client_id": "x", "project_id": "y"}}')


async def _vault_error(page):
    # The tree's row (M2h-2: the open file's head names it too, and the first file opens by itself).
    async with page.expect_response(lambda r: "/api/vault/file?" in r.url):
        await page.get_by_role("button", name="note.txt", exact=True).first.click()
    editor = page.get_by_placeholder("Start typing...")
    # The file's text lands after the editor does; typing first is overwritten
    # by it, and Save stays disabled (a clean file has nothing to save).
    for _ in range(40):
        if await editor.count() and await editor.input_value() == "hello":
            break
        await asyncio.sleep(0.25)
    await editor.fill("hello, edited")
    await page.get_by_role("button", name=re.compile("^save$", re.I)).first.click()


#: Knowledge switched on, one base, and a document in every status (D157); its settings (B2b, D167).
_SMOKE_KB = {"id": "kb_00000000smoke", "name": "Smoke contracts", "description": "", "folder": "smoke_kb",
             "status": "active", "language": "simple", "visitor_access": False, "budget_monthly_usd": 5.0,
             "chunking": {"mode": "structure", "target_tokens": 450, "max_tokens": 800, "overlap_pct": 0.0},
             "embedding": {"model": "", "dims": None, "profile": None},
             "rerank": {"enabled": None, "model": None, "top_n": 8, "candidates": 50},
             "fusion": {"method": "rrf", "rrf_k": 60, "weights": {"lexical": 1.0, "vector": 1.0}, "overfetch": 50},
             "retrieval": {"top_k": 8, "score_threshold": None, "parent_expansion": "off", "neighbor_window": 1,
                           "max_context_tokens": 6000},
             "enrichment": {"mode": "prefix", "contextual_model": None, "extract_metadata": False},
             "revision": 1, "created_at": _NOW, "updated_at": _NOW,
             "web": {"max_depth": 3, "max_pages": 500, "rps_per_host": 1.0, "recrawl_days": 7, "sources": [
                 {"id": "kws_00000smoke1", "url": "https://docs.example.com/guide/", "kind": "site", "include": [],
                  "exclude": []}]},
             "state": {"counts_by_status": {"indexed": 1, "failed": 1}, "active_snapshot": "ksnap_smoke",
                       "building_snapshot": None, "month_spend_usd": 1.25, "paused_reason": None, "build": None}}
#: A base's eval (V1, D168): one question set, a finished run scored four ways with a judge, and drafts.
_SMOKE_EVAL_SCORES = {"cases": 2, "hit_at": {"1": 0.5, "3": 1.0, "5": 1.0, "10": 1.0}, "mrr": 0.75,
                      "ndcg_at_10": 0.82, "recall_at_10": 1.0, "complete_at_10": 1.0}
_SMOKE_EVAL_RESULTS = [
    {"key": f"ksnap_smoke:{config}", "snapshot_id": "ksnap_smoke", "config": config, "label": label,
     "available": config != "vector", "reason": "Not available: nothing in this index is embedded."
     if config == "vector" else None, "weights": None, "rrf_k": None, "agent_sees": 8 if config == "served" else None,
     "scores": _SMOKE_EVAL_SCORES, "by_kind": {"direct": _SMOKE_EVAL_SCORES}}
    for config, label in (("lexical", "Keyword only"), ("vector", "Meaning only"), ("hybrid", "Hybrid (fused)"),
                          ("served", "As served"))]
_SMOKE_EVAL_RUN_SUMMARY = {
    "id": "krun_smoke", "kb_id": "kb_00000000smoke", "set_id": "kset_smoke", "status": "completed",
    "started_by": "person", "case_count": 2, "done_count": 2, "compared_cases": 2, "stale_cases": 0,
    "snapshots": [{"snapshot_id": "ksnap_smoke", "status": "active", "docs_done": None, "docs_total": None,
                   "comparable": True, "embed_model": None, "chunker_version": "structure-1", "settings": {}}],
    "results": _SMOKE_EVAL_RESULTS, "notes": [], "est_usd": 0.01, "cost_usd": 0.0123, "error": None,
    "judge": {"answer_model": "gpt-4o-mini", "judge_model": "claude-haiku-4-5", "rubric_id": "rub_kgrounded",
              "rubric_version": 1, "scale_max": 10.0, "mean_score": 8.5, "judged": 2, "failed": 0, "quotes": 2,
              "quotes_matched": 2, "unresolved_labels": 0, "warning": None},
    "created_at": _NOW, "started_at": _NOW, "finished_at": _NOW}
_SMOKE_EVAL_EVIDENCE = {"doc_id": "kdoc_smoke1", "path": "smoke_kb/acme.md", "attachment": None,
                        "doc_sha256": "a" * 64, "char_start": 10, "char_end": 60,
                        "quote": "The hosting fee is 1,200 a month, billed in advance."}
_SMOKE_EVAL_CASES = [
    {"id": "case_smoke1", "kind": "direct", "question": "What does hosting cost each month?",
     "reference_answer": "1,200.", "evidence": [_SMOKE_EVAL_EVIDENCE], "query_id": None, "overlap": 0.6,
     "created_at": _NOW},
    {"id": "case_smoke2", "kind": "paraphrase", "question": "How much do we pay to keep the site online?",
     "reference_answer": "1,200 a month.", "evidence": [_SMOKE_EVAL_EVIDENCE], "query_id": None, "overlap": 0.0,
     "created_at": _NOW},
]
_SMOKE_EVAL_SET_SUMMARY = {"id": "kset_smoke", "kb_id": "kb_00000000smoke", "name": "Smoke questions", "revision": 1,
                           "case_count": 2, "counts_by_kind": {"direct": 1, "paraphrase": 1}, "created_at": _NOW,
                           "updated_at": _NOW, "last_run": _SMOKE_EVAL_RUN_SUMMARY}
_SMOKE_EVAL_SET = {**_SMOKE_EVAL_SET_SUMMARY, "cases": _SMOKE_EVAL_CASES}
_SMOKE_EVAL_RUN = {**_SMOKE_EVAL_RUN_SUMMARY, "cases": [
    {"case_id": c["id"], "kind": c["kind"], "question": c["question"], "stale": False, "trimmed": False,
     "ranks": {"ksnap_smoke:served": 1, "ksnap_smoke:hybrid": 2, "ksnap_smoke:lexical": 1},
     "found": {"ksnap_smoke:served": 1}, "query_ids": {"ksnap_smoke": "kq_smoke"},
     "top": [{"path": "smoke_kb/acme.md", "snippet": "The hosting fee…", "relevant": True}],
     "answer": 'Hosting costs "1,200 a month" [K1].', "quote_check": {"quotes": 1, "matched": 1, "unresolved": []},
     "verdict": {"score": 8.5, "reasoning": "Supported by its passage.", "per_criterion": {}, "error": None}}
    for c in _SMOKE_EVAL_CASES]}
_SMOKE_EVAL_DRAFTS = {"model": "gpt-4o-mini", "dropped": {"quote_not_found": 1}, "saved": False, "drafts": [
    {"kind": "multi_hop", "question": "Which team sets the hosting fee and issues refunds?",
     "reference_answer": "The billing team.", "evidence": [_SMOKE_EVAL_EVIDENCE, {**_SMOKE_EVAL_EVIDENCE,
                                                                                  "path": "smoke_kb/refunds.md"}],
     "overlap": 0.2}]}
_SMOKE_EVAL_ESTIMATE = {"lines": [{"part": "drafts", "model": "gpt-4o-mini", "calls": 10, "input_tokens": 9000,
                                   "output_tokens": 1200, "est_usd": 0.0021, "counts_toward_budget": False}],
                        "total_est_usd": 0.0021, "unpriced": [], "budget_remaining_usd": None}
#: The same base rebuilding its index after a change to how it is built (B2b).
_SMOKE_KB_REBUILDING = {**_SMOKE_KB, "state": {
    **_SMOKE_KB["state"], "building_snapshot": "ksnap_smoke2",
    "build": {"snapshot_id": "ksnap_smoke2", "status": "building", "docs_total": 2, "docs_done": 1,
              "docs_failed": 0, "started_at": _NOW, "paused_reason": None, "error": None,
              # CCR-12 (PG1): its passages, the stage of the document being built, the time left
              "passages_done": 120, "passages_total": 357, "stage": "embed", "eta_s": 480, "waiting": None}}}
_SMOKE_KB_DOCS = [
    {"id": f"kdoc_smoke{i:02d}", "kb_id": _SMOKE_KB["id"], "path": f"smoke_kb/{s}.md", "source": "upload",
     "source_url": None, "status": s, "status_reason": r, "error": "The model is down." if s == "failed" else None,
     "version": 1, "sha256": None, "size_bytes": 1200, "mime": "text/markdown", "title": None, "doc_date": None,
     "metadata": {}, "pages": None, "chunks": 3, "tokens": 90, "created_at": _NOW, "updated_at": _NOW,
     "indexed_at": _NOW}
    for i, (s, r) in enumerate((("indexed", None), ("partial", "truncated_size"), ("failed", "embed_failed"),
                                ("skipped", "unsupported_format"), ("paused", "budget_cap"), ("queued", None),
                                ("indexing", None), ("blocked", "egress_blocked")))] + [
    {"id": "kdoc_smokeweb", "kb_id": _SMOKE_KB["id"], "path": "smoke_kb/_web/docs.example.com/guide/install.md",
     "source": "web", "source_url": "https://docs.example.com/guide/install", "status": "indexed",
     "status_reason": None, "error": None, "version": 1, "sha256": "f" * 64, "size_bytes": 900,
     "mime": "text/markdown", "title": "Install", "doc_date": None, "metadata": {}, "pages": None, "chunks": 2,
     "tokens": 60, "created_at": _NOW, "updated_at": _NOW, "indexed_at": _NOW}]
#: The working document's progress (CCR-12, PG1): contexts being written, a rate limit waited out.
_SMOKE_KB_DOCS[6]["progress"] = {
    "stage": "enrich", "done": 12, "total": 357, "unit": "passages", "eta_s": 480, "updated_at": _NOW,
    "waiting": {"reason": "rate_limited", "provider": "OpenAI", "provider_id": "openai", "until": _NOW}}
#: A queued document that gave its slot back to the platform's rate limit (KL1, CCR-14): where it got to, and until
#: when.
_SMOKE_KB_DOCS[5]["progress"] = {
    "stage": "embed", "done": 96, "total": 400, "unit": "passages", "eta_s": None, "updated_at": _NOW,
    "waiting": {"reason": "rate_limited", "provider": "model gateway", "provider_id": "model_gateway",
                "until": _NOW}}
#: The base's one web source and its latest crawl (S1, CCR-4): stopped at its page cap.
_SMOKE_KB_CRAWLS = [{
    "source_id": "kws_00000smoke1", "url": "https://docs.example.com/guide/", "kind": "site", "status": "capped",
    "crawl_id": "kcr_smoke", "started_at": _NOW, "finished_at": _NOW, "pages": 500,
    "counts": {"fetched": 500, "unchanged": 480, "written": 20, "deleted": 0, "blocked": 3, "failed": 0, "skipped": 1,
               "not_followed": 42},
    "next_due_at": _NOW, "last_error": None}]
#: The same source between the slices a runner runs a crawl in (KL1, F353): queued, 12 pages fetched so far.
_SMOKE_KB_CRAWLS_SLICED = [{
    **_SMOKE_KB_CRAWLS[0], "status": "queued", "finished_at": None, "pages": 12,
    "counts": {**_SMOKE_KB_CRAWLS[0]["counts"], "fetched": 12, "unchanged": 10, "written": 2, "not_followed": 0}}]
#: The same source while its crawl runs (F422, C8): its counts move as it works.
_SMOKE_KB_CRAWLS_RUNNING = [{**_SMOKE_KB_CRAWLS_SLICED[0], "status": "running"}]
_SMOKE_KB_REPORT = {
    **_SMOKE_KB_DOCS[2], "report": {
        "report_version": 1, "doc_id": "kdoc_smoke02", "path": "smoke_kb/failed.md", "sha256": None, "attempt": 2,
        "started_at": _NOW, "finished_at": _NOW, "outcome": "failed", "status_reason": "embed_failed",
        "stages": [{"stage": "parse", "status": "ok", "started_at": _NOW, "duration_ms": 12, "detail": {},
                    "error": None},
                   {"stage": "index", "status": "failed", "started_at": _NOW, "duration_ms": 40, "detail": {},
                    "error": {"code": "embed_failed", "message": "The model is down.", "retryable": True}}],
        "pages": {"total": 0, "with_text": 0, "no_text_layer": 0, "ocr_applied": 0, "ocr_failed": 0, "skipped": []},
        "chunks": {"count": 0, "by_kind": {}, "tokens": 0},
        "warnings": [{"code": "zero_width_removed", "message": "2 invisible character(s) removed.", "page": None,
                      "block_id": None, "count": 2}],
        "cost": {"est_usd": 0.0, "charged_microusd": None, "by_stage": {}},
        "parser": {"name": "text", "version": "1", "options_hash": ""}, "snapshot_id": None,
        "retry": {"allowed": True, "hint": "The embedding model failed. Retrying usually helps."}}}
_SMOKE_KB_COSTS = [{"key": "embed", "est_usd": 1.2, "charged_microusd": None, "input_tokens": 60_000_000, "units": 0.0},
                   {"key": "rerank", "est_usd": 0.05, "charged_microusd": 50_000, "input_tokens": 0, "units": 25.0}]
_SMOKE_KB_PREVIEW = {"docs": 2, "pages": 0, "total_est_usd": 0.0012, "budget_remaining_usd": 3.75, "stages": [
    {"stage": "embed", "model": "text-embedding-3-small", "units": 60000, "unit": "tokens", "est_usd": 0.0012,
     "cost_source": "byok"}]}
_SMOKE_KB_PREVIEW_CONTEXTUAL = {"docs": 2, "pages": 0, "total_est_usd": 0.0102, "budget_remaining_usd": 3.75,
                                "stages": [
    {"stage": "embed", "model": "text-embedding-3-small", "units": 63000, "unit": "tokens", "est_usd": 0.00126,
     "cost_source": "byok"},
    {"stage": "enrich", "model": "gpt-4o-mini", "units": 40, "unit": "requests", "est_usd": 0.0089,
     "cost_source": "byok"}]}
_SMOKE_KB_ROUTES = [
    ("**/api/settings", _answer({"knowledge_enabled": True, "embedding_model": "text-embedding-3-small"})),
    ("**/api/models", _answer({"providers": {"openai": {"available": True, "models": [],
                                                        "embedding_models": ["text-embedding-3-small"]}}})),
    ("**/api/knowledge/bases", _answer([_SMOKE_KB])),
    ("**/api/knowledge/bases/*/documents*", _answer(_SMOKE_KB_DOCS)),
    ("**/api/knowledge/bases/*/crawls", _answer(_SMOKE_KB_CRAWLS)),
    ("**/api/knowledge/bases/*/costs*", _answer(_SMOKE_KB_COSTS)),
    ("**/api/knowledge/bases/*/cost-preview", _answer(_SMOKE_KB_PREVIEW, methods=("POST",))),
    ("**/api/knowledge/documents/*", _answer(_SMOKE_KB_REPORT)),
    ("**/api/knowledge/bases/*/eval/sets", _answer([_SMOKE_EVAL_SET_SUMMARY])),
    ("**/api/knowledge/bases/*/eval/sets/*", _answer(_SMOKE_EVAL_SET)),
    ("**/api/knowledge/bases/*/eval/runs*", _answer([_SMOKE_EVAL_RUN_SUMMARY])),
    ("**/api/knowledge/bases/*/eval/runs/*", _answer(_SMOKE_EVAL_RUN)),
    ("**/api/knowledge/bases/*/eval/drafts", _answer(_SMOKE_EVAL_DRAFTS, methods=("POST",))),
    ("**/api/knowledge/bases/*/eval/estimate", _answer(_SMOKE_EVAL_ESTIMATE, methods=("POST",))),
]
#: The eval's dialogs start from the workspace's model.
_SMOKE_KB_EVAL_ROUTES = _SMOKE_KB_ROUTES + [
    ("**/api/settings", _answer({"knowledge_enabled": True, "model": "gpt-4o-mini", "judge_model": ""})),
    ("**/api/models", _answer({"providers": {"openai": {"available": True, "models": ["gpt-4o-mini"],
                                                        "embedding_models": ["text-embedding-3-small"]}}})),
]
#: Contextual Retrieval's picker lists the workspace's chat models; its rebuild is priced with a context line.
_SMOKE_KB_CONTEXTUAL_ROUTES = _SMOKE_KB_ROUTES + [
    ("**/api/settings", _answer({"knowledge_enabled": True, "model": "gpt-4o-mini",
                                 "embedding_model": "text-embedding-3-small"})),
    ("**/api/models", _answer({"providers": {"openai": {"available": True, "models": ["gpt-4o-mini", "gpt-4.1-nano"],
                                                        "embedding_models": ["text-embedding-3-small"]}}})),
    ("**/api/knowledge/bases/*/cost-preview", _answer(_SMOKE_KB_PREVIEW_CONTEXTUAL, methods=("POST",))),
]


def _smoke_citation(n: int, page: int, matched: bool | None) -> dict:
    return {"label": f"[K{n}]", "kb_id": _SMOKE_KB["id"], "doc_id": "kdoc_smoke00", "chunk_id": f"kch_smoke{n}",
            "path": "smoke_kb/contract.md", "source": "upload", "source_url": None, "title": "Master Services Agreement",
            "page_start": page, "page_end": page + 1, "char_start": 0, "char_end": 40,
            "heading_path": ["Agreement", "Fees"], "snippet": "Service | Monthly fee Hosting | 1,200 Support | 300",
            "indexed_at": _NOW, "snapshot_id": "ksnap_smoke",
            "quote_check": None if matched is None else {"quote": "Hosting | 1,200" if matched else "support is free",
                                                         "matched": matched, "method": "exact"}}


#: A reopened answer with its Sources (B1b): a quote found, one not found, and a label nothing resolved.
_SMOKE_SOURCES_HISTORY = {"turns": [{
    "user": "what does hosting cost?", "tools": [], "timestamp": "",
    "assistant": 'Hosting is "Hosting | 1,200" [K1] a month; "support is free" [K3]; see also [K2].',
    "citations": [_smoke_citation(1, 4, True), _smoke_citation(3, 7, False)],
    "unresolved_citations": ["[K2]"]}], "pending_approval": None, "running": None}

#: The Knowledge screen's test search (B1b): two passages and their trace.
_SMOKE_KB_SEARCH = {
    "query_id": "kq_smoke", "degraded": ["vector_unavailable"],
    "hits": [{"label": f"[K{n}]", "chunk_id": f"kch_smoke{n}", "doc_id": "kdoc_smoke00", "kb_id": _SMOKE_KB["id"],
              "content": "Service | Monthly fee\nHosting | 1,200\nSupport | 300", "context": "", "score": 4.258 / n,
              "scores": {"lexical": 4.258 / n}, "metadata": {}, "citation": _smoke_citation(n, 4, None)}
             for n in (1, 2)],
    "trace": {"query_id": "kq_smoke", "degraded": [{"code": "vector_unavailable", "detail": None}],
              "legs": [{"leg": "lexical", "requested": 8, "returned": 2, "filtered": False, "exact": True,
                        "latency_ms": 4}],
              "cuts": [{"stage": "threshold", "dropped_count": 1, "rule": "score < 1.0"}],
              "timings_ms": {"lexical": 4, "total": 9}},
}


#: A run with a Retrieve and an Ingest step (R1): the run log's retrieval row, opened, draws its trace.
_SMOKE_KB_RUN_ORCH = {
    "id": "orch_smoke_kb", "name": "Smoke knowledge flow", "description": "", "trigger": "manual",
    "state_schema": {}, "entry_step_id": "s1", "max_total_turns": 100, "timeout_minutes": 30,
    "steps": [{"id": "s1", "name": "Index tickets", "type": "ingest", "next_step_id": "s2", "output_key": "kept",
               "ingest": {"kb_id": _SMOKE_KB["id"], "records_from": "tickets"}},
              {"id": "s2", "name": "Find clauses", "type": "retrieve", "next_step_id": "s_end", "output_key": "found",
               "retrieve": {"kb_ids": [_SMOKE_KB["id"]], "query": "{state.user_input}"}},
              {"id": "s_end", "name": "End", "type": "end", "next_step_id": None}]}
_SMOKE_KB_TRACE = {
    **_SMOKE_KB_SEARCH["trace"],
    "pipeline": {_SMOKE_KB["id"]: {"snapshot_id": "ksnap_smoke", "embed_model": None, "rerank_model": None}},
    "fusion": None, "rerank": None,
    "hits": [{"label": "[K1]", "path": "contracts/acme.pdf", "page_start": 4, "page_end": 4, "score": 4.258}],
    "costs": {"total_est_usd": 0.0, "total_charged_microusd": None, "rows": []},
}
_SMOKE_KB_RUN_STREAM = "".join([
    _frame({"type": "step_start", "orch_step_id": "s1", "step_name": "Index tickets", "step_type": "ingest"}, "1"),
    _frame({"type": "knowledge_ingest", "orch_step_id": "s1", "step_name": "Index tickets", "kb_id": _SMOKE_KB["id"],
            "documents": [{"doc_id": "kdoc_smoke01", "record_id": "T-1", "status": "queued"}]}, "2"),
    _frame({"type": "step_complete", "orch_step_id": "s1", "step_name": "Index tickets", "duration_seconds": 0.1}, "3"),
    _frame({"type": "step_start", "orch_step_id": "s2", "step_name": "Find clauses", "step_type": "retrieve"}, "4"),
    _frame({"type": "knowledge_retrieve", "orch_step_id": "s2", "step_name": "Find clauses", "query_id": "kq_smoke",
            "snapshot_ids": ["ksnap_smoke"], "hit_count": 1, "cost_usd": 0.0, "latency_ms": 9,
            "degraded": ["vector_unavailable"]}, "5"),
    _frame({"type": "step_complete", "orch_step_id": "s2", "step_name": "Find clauses", "duration_seconds": 0.1}, "6"),
    _frame({"type": "stream_complete"})])
_SMOKE_KB_RUN_ROUTES = [
    ("**/api/orchestrations", _answer([_SMOKE_KB_RUN_ORCH])),
    ("**/api/orchestrations/runs", _answer([{**_SMOKE_RUNS[1], "run_id": "run_smoke_kb",
                                              "orchestration_id": "orch_smoke_kb"}])),
    ("**/api/orchestrations/runs/run_smoke_kb", _answer(
        {"run_id": "run_smoke_kb", "orchestration_id": "orch_smoke_kb", "status": "completed",
         "step_history": [], "waiting_for_human": False})),
    ("**/api/orchestrations/runs/run_smoke_kb/events/stream*",
     _answer(_SMOKE_KB_RUN_STREAM, content_type="text/event-stream")),
    ("**/api/orchestrations/runs/run_smoke_kb/children", _answer([])),
    ("**/api/knowledge/queries/kq_smoke", _answer(_SMOKE_KB_TRACE)),
]


async def _kb_settings_rebuild(page):
    """Open the base's settings and change how its passages are cut: the drawer prices the rebuild."""
    await page.get_by_role("button", name="Settings", exact=True).click()
    await page.get_by_role("combobox", name="Passages", exact=True).click()
    await page.get_by_role("option", name="One per page").click()


async def _kb_settings_contextual(page):
    """Open the base's settings and choose Contextual Retrieval: the Context model picker and its price show."""
    await page.get_by_role("button", name="Settings", exact=True).click()
    await page.get_by_role("combobox", name="Context for each passage", exact=True).click()
    await page.get_by_role("option", name="Contextual Retrieval: a model writes it").click()
    await page.get_by_role("button", name="Context model", exact=True).wait_for(timeout=10_000)
    # The drawer's price line ("Writing contexts: …"), not a working document's stage ("Writing contexts · …", PG1)
    await page.get_by_role("dialog", name=re.compile("^Settings for")).get_by_text("Writing contexts:").wait_for(
        timeout=10_000)


async def _kb_eval(page):
    await page.get_by_role("button", name=re.compile("^Evaluate")).click()
    await page.get_by_text("Hit@5").first.wait_for(timeout=20_000)


async def _kb_eval_drafts(page):
    await _kb_eval(page)
    await page.get_by_role("button", name="Draft questions").click()
    await page.get_by_role("button", name=re.compile("^Draft 10 questions")).click()


async def _kb_test_search(page):
    # Test search starts open under the base's numbers (M2e): its field is there without a click.
    await page.get_by_role("searchbox", name="Search Smoke contracts").fill("what does hosting cost?")
    await page.get_by_role("button", name="Search", exact=True).click()


#: (name, path, init scripts, routes, action, a text proving the state is on screen)
LIGHT_STATES = (
    ("chat sources", "/", [CHAT_STUB, "window.__fakeHistory = " + json.dumps(_SMOKE_SOURCES_HISTORY) + ";"], [],
     None, "Not from a search in this answer"),
    ("knowledge test search", "/knowledge", [],
     _SMOKE_KB_ROUTES + [("**/api/knowledge/search", _answer(_SMOKE_KB_SEARCH, methods=("POST",)))],
     _kb_test_search, "score 4.258"),
    ("knowledge statuses", "/knowledge", [], _SMOKE_KB_ROUTES, None, "Partly indexed"),
    ("knowledge web sources", "/knowledge", [], _SMOKE_KB_ROUTES, None, "stopped at the page limit"),
    ("knowledge add web pages", "/knowledge", [], _SMOKE_KB_ROUTES,
     lambda p: p.get_by_role("button", name="Add web pages").click(), "What to fetch"),
    ("a Retrieve step's trace", "/workflows?run=run_smoke_kb", [], _SMOKE_KB_RUN_ROUTES,
     lambda p: p.get_by_text(re.compile(r"^Retrieve · Find clauses")).first.click(), "ksnap_smoke"),
    ("knowledge settings", "/knowledge", [], _SMOKE_KB_ROUTES, _kb_settings_rebuild, "Saving rebuilds the index"),
    ("knowledge contextual retrieval", "/knowledge", [], _SMOKE_KB_CONTEXTUAL_ROUTES, _kb_settings_contextual,
     "Writing contexts:"),
    ("knowledge rebuilding", "/knowledge", [],
     _SMOKE_KB_ROUTES + [("**/api/knowledge/bases", _answer([_SMOKE_KB_REBUILDING]))], None,
     "Rebuilding the index: 1 of 2 documents"),
    ("knowledge rebuild passages", "/knowledge", [],
     _SMOKE_KB_ROUTES + [("**/api/knowledge/bases", _answer([_SMOKE_KB_REBUILDING]))], None,
     "120 of 357 passages · now embedding · about 8 min left"),
    ("knowledge working document", "/knowledge", [], _SMOKE_KB_ROUTES, None,
     "Writing contexts · 12 of 357 passages"),
    ("knowledge rate limit waited out", "/knowledge", [], _SMOKE_KB_ROUTES, None,
     "Waiting for OpenAI's rate limit until"),
    ("knowledge parked document", "/knowledge", [], _SMOKE_KB_ROUTES, None,
     "Waiting for the platform's rate limit until"),
    ("knowledge crawl between slices", "/knowledge", [],
     _SMOKE_KB_ROUTES + [("**/api/knowledge/bases/*/crawls", _answer(_SMOKE_KB_CRAWLS_SLICED))], None,
     "Waiting for its turn · 12 pages fetched so far"),
    ("knowledge crawl running", "/knowledge", [],
     _SMOKE_KB_ROUTES + [("**/api/knowledge/bases/*/crawls", _answer(_SMOKE_KB_CRAWLS_RUNNING))], None,
     "Fetching now · 12 pages fetched"),
    ("knowledge costs", "/knowledge", [], _SMOKE_KB_ROUTES,
     lambda p: p.get_by_role("button", name=re.compile("^Costs")).click(), "Reranking"),
    ("knowledge report", "/knowledge", [], _SMOKE_KB_ROUTES,
     lambda p: p.get_by_role("button", name="Report for failed.md").click(), "What happened"),
    ("knowledge eval results", "/knowledge", [], _SMOKE_KB_EVAL_ROUTES, _kb_eval, "Grounded in cited sources"),
    ("knowledge eval suggestions", "/knowledge", [], _SMOKE_KB_EVAL_ROUTES, _kb_eval_drafts,
     "Suggestions only — nothing is saved until you add them."),
    ("chat tip 5", "/", [CHAT_STUB], [],
     lambda p: p.get_by_role("button", name="Show tip 5: Automate with schedules").click(),
     "Automate with schedules"),
    ("chat failed turn", "/", [CHAT_STUB], [], _send_and_fail, "The model couldn't answer."),
    ("chat Thinking turns", "/", [CHAT_STUB, "window.__fakeHistory = " + json.dumps({
        "turns": [{"user": "q", "assistant": "a", "tools": [], "timestamp": "", "reasoning": ["one", "two"]}],
        "pending_approval": None, "running": None}) + ";"], [],
     lambda p: p.get_by_role("button", name=re.compile(r"^Thinking")).first.click(), "Turn 2"),
    ("orchestration runs", "/orchestrations", [], [("**/api/orchestrations/runs", _answer(_SMOKE_RUNS))],
     lambda p: p.get_by_role("button", name="Recent", exact=True).click(), "3.1"),
    ("a failed run", "/workflows?run=run_smoke_fail", [], _SMOKE_RUN_ROUTES, None, "Nested runs (1)"),
    ("a step's full response", "/workflows?run=run_smoke_fail", [], _SMOKE_RUN_ROUTES,
     lambda p: p.get_by_title("View full response").first.click(), "— full response"),
    ("Recent Runs' Resume", "/workflows?id=orch_smoke_c", [], _SMOKE_RUN_ROUTES,
     lambda p: p.get_by_role("button", name=re.compile(r"^Recent Runs")).first.click(), "Resume"),
    ("usage", "/usage", [], _SMOKE_USAGE, None, "Top workflow runs by savings"),
    ("usage compaction row", "/usage", [], _SMOKE_USAGE + [("**/api/usage/logs*", _answer({"logs": [
        {"timestamp": _NOW, "model": "anthropic.claude-3-haiku", "provider": "bedrock",
         "session_id": "sess_smoke_usage", "agent_id": "agent_smoke", "source": "chat", "input_tokens": 500,
         "output_tokens": 100, "total_tokens": 600, "context_chars": 40960, "estimated_cost": 0.0005,
         "latency_seconds": 1.2},
        {"timestamp": _NOW, "event_type": "compaction", "stage": "llm_summary", "chars_before": 40960,
         "chars_after": 8192, "reduction_pct": 80, "context_chars": 8192, "agent_id": "agent_smoke", "model": "",
         "provider": "", "session_id": "sess_smoke_usage", "source": "chat", "input_tokens": 0,
         "output_tokens": 0, "total_tokens": 0, "estimated_cost": 0, "latency_seconds": 0}]}))],
     lambda p: p.get_by_role("button", name=re.compile("sess_smoke_usage")).first.click(), "context compact"),
    ("usage load error", "/usage", [], [("**/api/usage/summary", _answer({}, status=500))], None,
     "Summary fetch failed: 500"),
    ("import preview", "/settings/import_export", [], _SMOKE_IMPORT_ROUTES, _open_import,
     "Google Workspace Not Connected"),
    ("import result", "/settings/import_export", [], _SMOKE_IMPORT_ROUTES + [("**/api/import", _answer(
        {"results": {"agents": [{"id": "agent_smoke_mail", "name": "Smoke mail agent", "status": "imported"}],
                     "mcp_servers": [{"name": "smoke_oauth", "label": "Smoke OAuth", "status": "oauth_pending",
                                      "auth_url": "https://example.invalid/authorize"}]}}, methods=("POST",)))],
     _import_now, "Needs authorization"),
    ("integrations connected", "/settings/workspace", [], [("**/api/config", _answer(
        {"has_credentials": True, "client_id": "123-smoke.apps.googleusercontent.com",
         "project_id": "smoke-project", "is_connected": True, "user_email": "smoke@example.com"}))],
     None, "Authenticated"),
    ("integrations saved", "/settings/workspace", [], [
        ("**/api/config", _held_after_first(
            {"has_credentials": True, "client_id": "123-smoke.apps.googleusercontent.com",
             "project_id": "smoke-project", "is_connected": True, "user_email": "smoke@example.com"}, 8.0)),
        ("**/api/setup/google-credentials", _answer({}, methods=("POST",)))],
     _paste_credentials, "Credentials saved"),
    ("repo badges", "/settings/repos", [], [("**/api/repos", _answer([
        {"id": f"r{i}", "name": f"Smoke {s}", "path": f"/tmp/{s}", "description": "", "included_patterns": [],
         "excluded_patterns": [], "last_indexed": None, "status": s, "file_count": 3}
        for i, s in enumerate(("indexed", "indexing", "stopping"))]))], None, "Smoke stopping"),
    ("python tool test", "/tools", [], [
        ("**/api/tools/custom", _answer([{"name": "uismoke_py", "generalName": "Smoke python tool",
                                          "description": "d", "tool_type": "python", "code": "print('hi')\n",
                                          "inputSchema": {"type": "object", "properties": {}},
                                          "schemaParams": [], "globals": []}])),
        ("**/api/tools/python/test", _answer({"stdout": '{"output": "ok"}\n',
                                              "stderr": "Traceback (most recent call last):\nValueError: boom\n",
                                              "exit_code": 1}, methods=("POST",)))],
     _python_test, "Exit code: 1"),
    ("openapi preview", "/tools", [], [("**/api/tools/import/openapi", _answer(
        {"tools": [{"name": "delete_pet", "method": "DELETE", "url": "https://api.example.com/pets/{id}"},
                   {"name": "get_pet", "method": "GET", "url": "https://api.example.com/pets/{id}"},
                   {"name": "create_pet", "method": "POST", "url": "https://api.example.com/pets"}],
         "group": {"name": "Smoke Pets"}}, methods=("POST",)))],
     _openapi_preview, "3 operations found"),
    ("messaging last error", "/channels", [], [("**/api/messaging/channels", _answer([
        {"id": "ch_smoke_err", "name": "Smoke broken channel", "platform": "telegram", "status": "error",
         "last_error": "Unauthorized: bot token rejected (401)", "agent_ids": [], "multi_agent_mode": False,
         "enabled": True, "credentials": {}}]))],
     lambda p: p.get_by_role("button", name=re.compile("Smoke broken channel")).first.click(), "Last error"),
    ("a compacted log", "/logs", [], [
        ("**/api/logs/agents*", _answer([{"run_id": "run_smoke_log", "agent_name": "Smoke log agent",
                                          "source": "chat", "session_id": "sess_smoke",
                                          "started_at": "2026-09-30 10:00:00", "file_size_kb": 2}])),
        ("**/api/logs/agents/run_smoke_log", _answer(
            "USER: hi\n==========\nCONTEXT COMPACTED — trim: 40960 → 8192 chars\n==========\nASSISTANT: ok\n",
            content_type="text/plain"))],
     lambda p: p.get_by_role("button", name=re.compile("^Smoke log agent")).first.click(), "CONTEXT COMPACTED"),
    ("schedule run toast", "/schedules", [], [
        ("**/api/schedules", _answer([{"id": "sched_smoke", "name": "Smoke schedule", "enabled": True,
                                       "created_at": _NOW, "target_type": "agent", "target_id": "agent_x",
                                       "prompt": "p", "schedule_type": "interval", "interval_value": 30,
                                       "interval_unit": "minutes", "next_run_at": None, "last_run_at": None}])),
        ("**/api/schedules/sched_smoke/run", _answer({}, methods=("POST",)))],
     lambda p: p.get_by_role("button", name="Run Smoke schedule now").first.click(), "Schedule triggered successfully"),
    ("data lab running", "/settings/datalab", [], [
        ("**/api/synthetic/status", _answer({"status": "generating", "completed": 3, "total": 10})),
        ("**/api/synthetic/datasets", _answer([]))], None, "Running: 3/10"),
    ("examples error", "/settings/import_export?tab=examples", [],
     [("**/api/examples", _answer({}, status=500))], None, "Failed to load examples"),
    ("vault save error", "/vault", [], [
        ("**/api/vault/tree*", _answer({"tree": [{"name": "note.txt", "path": "note.txt", "type": "file",
                                                   "ext": ".txt"}]})),
        ("**/api/vault/file*", _answer({"content": "hello"})),
        ("**/api/vault/file*", _answer({}, status=500, methods=("PUT",)))],
     _vault_error, "Could not save note.txt"),
    ("settings saved toast", "/settings/personal_details", [],
     [("**/api/personal-details", _answer({}, methods=("POST",)))],
     lambda p: p.get_by_role("button", name=re.compile("Save changes")).first.click(), "Personal details saved"),
)


@check
async def check_lightstates(browser) -> str:
    """Text meets AA in the states `contrast` cannot open, in both themes (F101).

    The light theme inverts the dark ramp one (family, shade) at a time, and a
    shade it lacks keeps its dark value on a light ground. `contrast` walks
    every screen on a clean store, so the uses that sit in states — a failed
    run, a connected integration, an import preview, a repo indexing, a test's
    output, a toast — were never measured (33 pairs, 109 uses, 16 files at
    `4bcf8b8`). Each is put on screen here from stubbed answers.

    Not measured (named limits): hover colours, SVG icons, gradients, and the
    Vault's "saved" button (disabled, which WCAG exempts).
    """
    bad: list[str] = []
    missing: list[str] = []
    for theme in ("light", "dark"):
        for name, path, inits, routes, act, shows in LIGHT_STATES:
            ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
            await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
            await ctx.add_init_script(
                f"try{{localStorage.setItem('synapseSessionId','uismoke-light-{int(time.time() * 1000)}')}}"
                "catch(e){}")
            for script in inits:
                await ctx.add_init_script(script)
            for pattern, handler in routes:
                await ctx.route(pattern, handler)
            page = await ctx.new_page()
            try:
                await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
                await asyncio.sleep(SETTLE)
                if act:
                    await act(page)
                try:
                    await page.get_by_text(re.compile(re.escape(shows), re.I)).first.wait_for(timeout=15_000)
                except Exception:
                    missing.append(f"{theme} {name}")
                    continue
                await asyncio.sleep(0.6)
                for row in await page.evaluate(CONTRAST):
                    if any(row["fg"] == fg and row["bg"] == bg for fg, bg, _ in CONTRAST_WAIVED):
                        continue
                    bad.append(f"{theme} {name}: {row['ratio']}:1 (needs {row['need']}) "
                               f"{row['fg']} on {row['bg']} — {row['txt'][:24]!r} [{row['cls'][:48]}]")
            except Exception as exc:
                missing.append(f"{theme} {name} ({str(exc).splitlines()[0][:80]})")
            finally:
                await ctx.close()
    out = []
    if missing:
        out.append(f"never on screen, so unmeasured: {missing}")
    if bad:
        out.append(f"{len(bad)} under AA — " + " | ".join(bad))
    if out:
        raise AssertionError("; ".join(out))
    return f"{len(LIGHT_STATES)} states, both themes, all text at AA"



# ── help (H1) ────────────────────────────────────────────────────────────────

HELP_BUTTON = "Help for this page"
HELP_FALLBACK = "Getting around"
DOCS_ORIGIN = "https://docs.synapseorch.com"

#: The docs site's pictures, stood in for: a 1×1 WebP and a one-second 16×10 VP9 WebM, both valid, so an `<img>`
#: loads and a `<video>` plays exactly as HC0's would. Nothing here reaches the docs site.
HELP_WEBP = b"UklGRh4AAABXRUJQVlA4TBEAAAAvH8AEAAfQsrZ0rf+BiOh/AAA="
HELP_WEBM = (
    b"GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAIkEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZU"
    b"rmtTrIHYTbuMU6uEElTDZ1OsggEiTbuMU6uEHFO7a1OsggIO7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1M"
    b"YXZmNTguNzYuMTAwV0GNTGF2ZjU4Ljc2LjEwMESJiECPQAAAAAAAFlSua8WuAQAAAAAAADzXgQFzxYivzfcwQ2AlOpyBACK1nIN1"
    b"bmSGhVZfVlA5g4EBI+ODhB3NZQDgAQAAAAAAAAmwgRC6gQqagQISVMNnQJ5zcwEAAAAAAAAnY8CAZ8gBAAAAAAAAGkWjh0VOQ09E"
    b"RVJEh41MYXZmNTguNzYuMTAwc3MBAAAAAAAAY2PAi2PFiK/N9zBDYCU6Z8gBAAAAAAAAJkWjh0VOQ09ERVJEh5lMYXZjNTguMTM0"
    b"LjEwMCBsaWJ2cHgtdnA5Z8iiRaOIRFVSQVRJT05Eh5QwMDowMDowMS4wMDAwMDAwMDAAAB9DtnXD54EAo6mBAACAgkmDQgAA8ACW"
    b"ADgkHBhCAAAwcAAASqf/+L4Bv/5hB//+rwcAAKOTgQH0AIYAQJKcAElAAAMgAABCQBxTu2uRu4+zgQC3iveBAfGCAcbwgQM="
)


async def _help_context(browser, *, media: str = "ok", reduced: bool = False, routes=()):
    """A context whose requests to the docs site are answered here and written down.

    `media`: "ok" serves the stand-in files, "404" answers every picture as missing (the docs site before HC0's
    media is published). Every request for the docs origin lands in the returned list, answered or not.
    """
    import base64
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]},
                                    reduced_motion="reduce" if reduced else "no-preference")
    seen: list[str] = []
    files = {".webp": ("image/webp", base64.b64decode(HELP_WEBP)), ".webm": ("video/webm", base64.b64decode(HELP_WEBM))}

    async def docs(route):
        url = route.request.url
        seen.append(url)
        kind = next((v for k, v in files.items() if url.split("?")[0].endswith(k)), None)
        if media != "ok" or kind is None:
            return await route.fulfill(status=404, body="")
        await route.fulfill(status=200, content_type=kind[0], body=kind[1])

    await ctx.route(f"{DOCS_ORIGIN}/**", docs)
    for pattern, handler in routes:
        await ctx.route(pattern, handler)
    return ctx, seen


TOUR_POPOVER = "#driver-popover-content"


async def _no_tour(page, path: str) -> None:
    """`path` loads and no tour opens: past the provider's 1.5 s settle, with room to draw."""
    await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
    await page.get_by_role("button", name=HELP_BUTTON, exact=True).first.wait_for(timeout=30_000)
    await asyncio.sleep(SETTLE + 2)
    if await page.locator(TOUR_POPOVER).is_visible():
        raise AssertionError(f"{path}: a tour opened again")


@check
async def check_tour(browser) -> str:
    """A page's guided tour opens by itself once, on the first visit, and never the help panel (GUIDE1, F919/F1028).

    Naveen (2026-10-24): guides open once on their own, the panel only on a click. A fresh browser: Home's tour
    opens with no panel behind it; Esc puts it away and a reload doesn't bring it back; the panel's "Take the
    tour" runs it again. A section's tab guide shows on its first tab and not on the section's next one.
    """
    ctx = await browser.fresh_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/", wait_until="domcontentloaded")
        popover = page.locator(TOUR_POPOVER)
        await popover.wait_for(state="visible", timeout=30_000)
        if await page.locator(f"[role=dialog]:not({TOUR_POPOVER})").count():
            raise AssertionError("/: a dialog other than the tour opened by itself")
        await page.keyboard.press("Escape")
        await popover.wait_for(state="hidden", timeout=5_000)
        await _no_tour(page, "/")

        _, dialog = await _open_help(page)
        await dialog.get_by_role("button", name="Take the tour").click()
        await popover.wait_for(state="visible", timeout=10_000)
        await page.keyboard.press("Escape")
        await popover.wait_for(state="hidden", timeout=5_000)

        await page.goto(f"{APP}/runs", wait_until="domcontentloaded")
        await popover.wait_for(state="visible", timeout=30_000)
        title = (await page.locator("#driver-popover-title").inner_text()).strip()
        if not title.startswith("Runs ›"):
            raise AssertionError(f"/runs: the first visit's guide is {title!r}, not the section's tabs")
        await page.keyboard.press("Escape")
        await popover.wait_for(state="hidden", timeout=5_000)
        await _no_tour(page, "/logs")
    finally:
        await ctx.close()
    return "a first visit shows the page's tour once, never the panel; the panel replays it; a section's tabs once"


@check
async def check_firsttour(browser) -> str:
    """A first visit's tour waits for the first-run model step, then shows once (ONB3, D326, F1060).

    Before, the tour's countdown ran from the page's load and looked for a dialog once: a model step whose readiness
    answered late opened on top of a running tour, and one already up made the tour give up until the next page. A
    fresh browser over an install whose model cannot answer, with the answer 3 s late: no tour while the step is
    still to open or is open; Escape closes the step and Home's tour follows; a reload shows neither.
    """
    state = {"closed": False, "posts": []}
    ctx, page = await _firstrun_page(browser, "/dashboard", state, fresh=True, late=3)
    popover = page.locator(TOUR_POPOVER)
    dialog = page.get_by_test_id("model-setup")
    bad: list[str] = []
    try:
        deadline = time.monotonic() + 3 + SETTLE + 3
        while time.monotonic() < deadline:
            if await popover.is_visible():
                bad.append("a tour showed before the model step had opened, or over it")
                break
            await asyncio.sleep(0.25)
        if not await dialog.is_visible():
            bad.append("the model step never opened")
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="hidden", timeout=5_000)
        try:
            await popover.wait_for(state="visible", timeout=15_000)
        except Exception:
            bad.append("Home's tour did not follow the model step")
        else:
            await page.keyboard.press("Escape")
            await popover.wait_for(state="hidden", timeout=5_000)
        await page.reload(wait_until="domcontentloaded")
        await page.locator("main h1").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE + 2)
        if await dialog.count() or await popover.is_visible():
            bad.append("a reload brought the step or the tour back")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "no tour while the model step is to open or open; the page's tour once after it closes; a reload shows neither"


async def _open_help(page, how: str = "click"):
    """Press the page's "?" and wait for its panel. Returns (button, dialog)."""
    button = page.get_by_role("button", name=HELP_BUTTON, exact=True)
    await button.first.wait_for(timeout=30_000)
    if await button.count() != 1:
        raise AssertionError(f"{page.url}: {await button.count()} help buttons, not one")
    if how == "keyboard":
        await button.focus()
        await page.keyboard.press("Enter")
    else:
        await button.click()
    dialog = page.get_by_role("dialog")
    await dialog.wait_for(timeout=10_000)
    await asyncio.sleep(0.4)                      # the slide
    return button, dialog


_FOCUSED_LABEL = """() => { const el = document.activeElement;
  return el === document.body ? 'BODY' : (el?.getAttribute('aria-label') || el?.textContent?.trim()); }"""


@check
async def check_help(browser) -> str:
    """The "?" opens a panel about the page, only when pressed, and gives focus back when it closes (H1).

    Naveen: the panel opens "when clicking". A page's guided tour is the one thing that opens by itself on a first
    visit (his call, 2026-10-24, replacing "never as a tour") and is `tour`'s; this browser has seen every tour, so
    pages load and nothing opens, and nothing is fetched from the docs site either. Then the panel: a dialog named
    for the page, closed by Escape, its ✕ and the scrim, each handing focus back to the "?" that opened it (F382's
    drawer).
    """
    ctx, seen = await _help_context(browser)
    page = await ctx.new_page()
    try:
        for path in ("/", "/agents", "/vault"):
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            if await page.get_by_role("dialog").count():
                raise AssertionError(f"{path}: a dialog is open on a first visit")
        if seen:
            raise AssertionError(f"a first visit fetched from the docs site: {seen[:3]}")

        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        # Read before opening: an open panel hides the page behind it from the accessibility tree.
        first = page.get_by_role("button", name=HELP_BUTTON, exact=True)
        await first.wait_for(timeout=30_000)
        if await first.get_attribute("aria-haspopup") != "dialog":
            raise AssertionError("the \"?\" does not say it opens a dialog")
        button, dialog = await _open_help(page, "keyboard")
        name = (await dialog.get_by_role("heading").first.inner_text()).strip()
        if name != "Agents":
            raise AssertionError(f"/agents: the panel is headed {name!r}")
        inside = await page.evaluate("() => !!document.activeElement?.closest('[role=dialog]')")
        if not inside:
            raise AssertionError("opening the panel left focus outside it")
        guide = dialog.get_by_role("link", name=re.compile("read the guide", re.I))
        href, target, rel = (await guide.get_attribute("href"), await guide.get_attribute("target"),
                             await guide.get_attribute("rel"))
        if not (href or "").startswith(f"{DOCS_ORIGIN}/guides/") or target != "_blank" or "noopener" not in (rel or ""):
            raise AssertionError(f"\"Read the guide\" is {href!r} target={target!r} rel={rel!r}")

        bad: list[str] = []
        for how in ("Escape", "the ✕", "the scrim"):
            if how != "Escape":
                button, dialog = await _open_help(page, "keyboard")
            if how == "Escape":
                await page.keyboard.press("Escape")
            elif how == "the ✕":
                await dialog.get_by_role("button", name="Close", exact=True).click()
            else:
                await page.mouse.click(40, WIDE[1] // 2)
            await asyncio.sleep(0.5)
            if await page.get_by_role("dialog").count():
                bad.append(f"{how} leaves the panel open")
                await page.keyboard.press("Escape")
                continue
            back_on = await page.evaluate(_FOCUSED_LABEL)
            if back_on != HELP_BUTTON:
                bad.append(f"closing with {how} left focus on {back_on!r}")
        if bad:
            raise AssertionError("; ".join(bad))

        # Chat draws its own header, so its "?" is placed by hand.
        await page.goto(APP, wait_until="domcontentloaded")
        _, dialog = await _open_help(page)
        name = (await dialog.get_by_role("heading").first.inner_text()).strip()
        if name != "Chat":
            raise AssertionError(f"/: the panel is headed {name!r}")
        return ("nothing opens or loads on a first visit; the \"?\" opens a panel named for the page, and Escape, "
                "the ✕ and the scrim close it with focus back on the \"?\"")
    finally:
        await ctx.close()


@check
async def check_help_media(browser) -> str:
    """The panel's picture comes from the docs site only when it can be shown, and the words never depend on it.

    Online, a clip plays (with a pause control) and a still loads; under reduced motion a clip is its still and the
    clip is never fetched; offline, nothing is drawn or fetched and the steps are all there; a picture the docs site
    does not have (before HC0's are published) takes its frame away and leaves the words.
    """
    kb = list(_SMOKE_KB_ROUTES)              # a base, so Knowledge shows the topic with a clip
    out: list[str] = []

    async def opened(path: str, **kw):
        ctx, seen = await _help_context(browser, routes=kb, **kw)
        page = await ctx.new_page()
        await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
        if path == "/knowledge":                  # the clip's topic is the open base, shown once the bases load
            await page.get_by_role("button", name="Upload files").wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        return ctx, seen, page

    async def steps(dialog) -> int:
        return await dialog.locator("ol > li").count()

    # A clip, online.
    ctx, seen, page = await opened("/knowledge")
    try:
        _, dialog = await _open_help(page)
        video = dialog.locator("video")
        await video.wait_for(timeout=10_000)
        src = await video.get_attribute("src") or ""
        if not src.startswith(f"{DOCS_ORIGIN}/media/") or not src.endswith(".webm"):
            raise AssertionError(f"the clip's src is {src!r}")
        for attr in ("autoplay", "muted", "loop", "playsinline"):
            if await video.get_attribute(attr) is None and not await video.evaluate(f"v => !!v.{attr if attr != 'playsinline' else 'playsInline'}"):
                raise AssertionError(f"the clip is not {attr}")
        await asyncio.sleep(1.0)
        if not any(u.endswith(".webm") for u in seen) or not any(u.endswith(".webp") for u in seen):
            raise AssertionError(f"the clip and its poster were not both fetched: {seen}")
        pause = dialog.get_by_role("button", name="Pause the clip")
        await pause.click()
        await asyncio.sleep(0.3)
        if not await video.evaluate("v => v.paused"):
            raise AssertionError("Pause the clip did not pause it")
        if not await dialog.get_by_role("button", name="Play the clip").count():
            raise AssertionError("a paused clip offers no Play")
        out.append("a clip plays and pauses")
    finally:
        await ctx.close()

    # A still, online.
    ctx, seen, page = await opened("/vault")
    try:
        _, dialog = await _open_help(page)
        img = dialog.locator("figure img")
        await img.wait_for(timeout=10_000)
        await asyncio.sleep(0.6)
        alt, w, h = await img.get_attribute("alt"), await img.get_attribute("width"), await img.get_attribute("height")
        if not alt or not w or not h:
            raise AssertionError(f"the still has alt={alt!r} width={w!r} height={h!r}")
        if not await img.evaluate("i => i.complete && i.naturalWidth > 0"):
            raise AssertionError("the still never loaded")
        if any(u.endswith(".webm") for u in seen):
            raise AssertionError("a still's topic fetched a clip")
        out.append("a still loads with its alt and size")
    finally:
        await ctx.close()

    # Reduced motion: the clip's topic shows its still and never asks for the clip.
    ctx, seen, page = await opened("/knowledge", reduced=True)
    try:
        _, dialog = await _open_help(page)
        await dialog.locator("figure img").wait_for(timeout=10_000)
        await asyncio.sleep(1.0)
        if await dialog.locator("video").count():
            raise AssertionError("under reduced motion the panel still drew a <video>")
        if any(u.endswith(".webm") for u in seen):
            raise AssertionError(f"under reduced motion the clip was fetched: {seen}")
        out.append("reduced motion shows the poster and never fetches the clip")
    finally:
        await ctx.close()

    # Offline: the words, no picture, no request.
    ctx, seen, page = await opened("/knowledge")
    try:
        await ctx.set_offline(True)
        seen.clear()
        _, dialog = await _open_help(page)
        await asyncio.sleep(1.0)
        if await dialog.locator("figure, img, video").count():
            raise AssertionError("offline, the panel still drew its picture")
        if seen:
            raise AssertionError(f"offline, the panel asked the docs site for {seen}")
        n = await steps(dialog)
        if not 3 <= n <= 5:
            raise AssertionError(f"offline, the panel shows {n} steps")
        out.append("offline shows every step and no picture")
    finally:
        await ctx.close()

    # Missing on the docs site: the frame goes, the words stay — for a still and for a clip (whose still is missing too).
    for path in ("/vault", "/knowledge"):
        ctx, seen, page = await opened(path, media="404")
        try:
            _, dialog = await _open_help(page)
            await asyncio.sleep(1.5)
            if await dialog.locator("figure").count():
                raise AssertionError(f"{path}: a picture the docs site does not have left its frame")
            if not seen:
                raise AssertionError(f"{path}: the picture was never asked for, so this checks nothing")
            if not 3 <= await steps(dialog) <= 5:
                raise AssertionError(f"{path}: a missing picture took the steps with it")
        finally:
            await ctx.close()
    out.append("a missing picture leaves only the words")
    return "; ".join(out)


#: Every screen in the rail and Settings (H1). Each panel must be the page's own: headed like the page's `<h1>` —
#: or, on a hub's tab (D209, M1a), like the current tab, since the `<h1>` is the hub's — never the fallback, with
#: three to five steps. Chat is named: it was checked before its header had an `<h1>` of its own.
HELP_PAGES = (
    "/dashboard", "/runs", "/logs", "/evals", "/usage", "/workflows", "/schedules", "/templates", "/agents",
    "/skills", "/vault", "/knowledge", "/apps", "/tools", "/channels", "/channels/phone", "/widgets",
    "/settings/general", "/settings/models", "/settings/personal_details", "/settings/variables", "/settings/memory",
    "/settings/workspace", "/settings/repos", "/settings/db_configs",
    "/settings/api_keys", "/settings/import_export", "/settings/scale", "/settings/support", "/settings/datalab",
)


@check
async def check_help_pages(browser) -> str:
    """Every page has a "?" whose panel is about that page (H1): the walk is the coverage test OSS has no unit
    runner for. A page with no topic shows the fallback, which fails here by name."""
    bad: list[str] = []

    async def walk(page, path: str, expect: str | None, ready=None) -> None:
        await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
        try:
            if ready is not None:                 # the state loads after the page: open the panel on it, not before
                await ready(page).wait_for(timeout=30_000)
            if expect is None and path == "/dashboard":
                # Home's h1 greets the reader (M2a); its panel is the page the menu names.
                await page.locator("main h1").first.wait_for(timeout=30_000)
                expect = "Home"
            if expect is None:
                await page.locator("main h1").first.wait_for(timeout=30_000)
                expect = (await page.locator("main h1").first.inner_text()).strip()
                # A hub's tab: the panel is about the tab shown, not the hub.
                tab = page.locator('main header nav a[aria-current="page"]')
                if await tab.count():
                    expect = (await tab.first.inner_text()).strip()
            _, dialog = await _open_help(page)
        except Exception as exc:
            bad.append(f"{path}: {str(exc).splitlines()[0][:90]}")
            return
        name = (await dialog.get_by_role("heading").first.inner_text()).strip()
        n = await dialog.locator("ol > li").count()
        # Knowledge opens its first base by itself, so with one the panel is about working in it.
        allowed = {expect} | ({"Working in a knowledge base"} if path == "/knowledge" else set())
        if name == HELP_FALLBACK:
            bad.append(f"{path}: no topic (the fallback)")
        elif name not in allowed:
            bad.append(f"{path}: the panel is {name!r}, the page {expect!r}")
        if not 3 <= n <= 5:
            bad.append(f"{path}: {n} steps")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

    ctx, _ = await _help_context(browser)
    page = await ctx.new_page()
    try:
        await walk(page, "/", "Chat")
        for path in HELP_PAGES:
            await walk(page, path, None)
    finally:
        await ctx.close()

    # The states a path does not name: an orchestration open in the editor, a knowledge base open.
    oid = f"orch_uismoke_help_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx, _ = await _help_context(browser, routes=list(_SMOKE_KB_ROUTES))
    page = await ctx.new_page()
    try:
        await walk(page, f"/workflows?id={oid}", "Editing a workflow",
                   lambda p: p.locator(".react-flow").first)
        await walk(page, "/knowledge", "Working in a knowledge base",
                   lambda p: p.get_by_role("button", name="Upload files"))
    finally:
        await ctx.close()
        drop_fixture(oid)
    if bad:
        raise AssertionError(f"{len(bad)} — " + "; ".join(bad))
    return f"{len(HELP_PAGES) + 3} pages and states each have a \"?\" whose panel is their own, with 3–5 steps"

# ── the shell (M1a): hubs, moved addresses, ⌘K, one title place, the phone menu ─────────────────────────────

#: D209's hubs with tabs: the title every tab page shows, and its tabs in order.
HUB_TABS = {
    "Runs": [("/runs", "Runs"), ("/logs", "Logs"), ("/evals", "Evals"), ("/usage", "Usage")],
    "Workflows": [("/workflows", "Workflows"), ("/schedules", "Schedules"), ("/templates", "Templates")],
    "Agents": [("/agents", "Agents"), ("/skills", "Skills")],
    "Apps": [("/apps", "Apps"), ("/tools", "Tools")],
    "Channels": [("/channels", "Channels"), ("/channels/phone", "Phone"), ("/widgets", "Widgets")],
}
#: The rail group each hub sits under: the eyebrow over a hub's tab named as the hub (`AppScreen`).
HUB_GROUPS = {"Runs": "Overview", "Workflows": "Build", "Agents": "Build", "Apps": "Connect", "Channels": "Connect"}

#: The small line over a page's title (`PageHeader`): the block just before the title's row.
_EYEBROW = "() => document.querySelector('main h1')?.parentElement?.previousElementSibling?.textContent?.trim() ?? null"


@check
async def check_hubtabs(browser) -> str:
    """Each page of a hub is titled by its own tab, with the hub's name over it (GUIDE1, F914: Evals reads "Runs /
    Evals", not "Runs"; a tab named as its hub keeps its rail group), the hub's tab bar with its own tab current, the
    rail lights the hub, and a tab is a client-side link (D209, M1a). Messaging is on in the smoke store, so Channels
    shows both tabs."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    bad: list[str] = []
    try:
        for hub, tabs in HUB_TABS.items():
            for href, label in tabs:
                await page.goto(f"{APP}{href}", wait_until="domcontentloaded")
                await page.locator("main h1").first.wait_for(timeout=30_000)
                await asyncio.sleep(0.8)
                h1 = (await page.locator("main h1").first.inner_text()).strip()
                eyebrow = await page.evaluate(_EYEBROW)
                bar = page.locator(f'main header nav[aria-label="{hub}"]')
                labels = [t.strip() for t in await bar.locator("a").all_inner_texts()]
                current = [t.strip() for t in await bar.locator('a[aria-current="page"]').all_inner_texts()]
                # The name only: Workflows carries its unseen-run count beside it.
                lit = [t.strip().splitlines()[0] for t in
                       await page.locator('nav[aria-label="Primary"] a[aria-current="page"]').all_inner_texts()]
                if h1 != label:
                    bad.append(f"{href}: the title is {h1!r}, not its tab's {label!r}")
                over = hub if label != hub else HUB_GROUPS[hub]
                if eyebrow != over:
                    bad.append(f"{href}: over the title is {eyebrow!r}, not {over!r}")
                if labels != [t[1] for t in tabs]:
                    bad.append(f"{href}: the tabs are {labels}")
                if current != [label]:
                    bad.append(f"{href}: the current tab is {current}")
                if lit != [hub]:
                    bad.append(f"{href}: the rail lights {lit}")
        # A tab is a link the app follows itself: the page is not reloaded.
        await page.goto(f"{APP}/runs", wait_until="domcontentloaded")
        await page.locator("main h1").first.wait_for(timeout=30_000)
        await page.evaluate("() => { window.__m1aMark = 1; }")
        await page.locator('main header nav[aria-label="Runs"] a', has_text="Evals").click()
        await page.wait_for_url("**/evals", timeout=10_000)
        if not await page.evaluate("() => window.__m1aMark === 1"):
            bad.append("a tab reloaded the page instead of moving within it")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return (f"{sum(len(t) for t in HUB_TABS.values())} tab pages under {len(HUB_TABS)} hubs: their own title under "
            "the hub's name, bar, current tab, rail")


#: Addresses that moved (D209, M1a), with the query that must ride along. The first seven are 308s at the routing
#: layer; a rail page's id under /settings is sent to its own page by the settings route.
MOVED_ADDRESSES = [
    ("/orchestrations?run=run_x&x=1", "/workflows?run=run_x&x=1", True),
    ("/mcp-servers", "/apps", True),
    ("/settings/orchestrations?id=o", "/workflows?id=o", True),
    ("/settings/mcp_servers", "/apps", True),
    ("/settings/logs", "/logs", True),
    ("/settings/messaging", "/channels", True),
    ("/settings/widgets", "/widgets", True),
    ("/settings/evals", "/evals", False),
]


@check
async def check_redirects(browser) -> str:
    """Every old address lands on its new page with its query, permanently where the routing layer answers (M1a)."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    bad: list[str] = []
    try:
        for old, new, permanent in MOVED_ADDRESSES:
            if permanent:
                r = await ctx.request.get(f"{APP}{old}", max_redirects=0)
                where = r.headers.get("location", "")
                if r.status != 308 or not where.endswith(new):
                    bad.append(f"{old} answers {r.status} → {where!r}, not 308 → {new}")
            await page.goto(f"{APP}{old}", wait_until="domcontentloaded")
            await page.wait_for_url(f"**{new}", timeout=15_000)
            await page.locator("main h1").first.wait_for(timeout=30_000)
    except Exception as exc:
        bad.append(str(exc).splitlines()[0][:120])
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return f"{len(MOVED_ADDRESSES)} old addresses land on their new pages, query kept"


@check
async def check_cmdk(browser) -> str:
    """⌘K: Ctrl+K opens it with the field focused, words filter it, Enter goes, Escape closes with focus back where
    it was, Ctrl+K closes it again, the rail's Jump to… row opens it, New workflow opens a blank draft, an agent by
    name opens that agent, and Ctrl+K inside the markdown editor is still the editor's link (M1a)."""
    aid = f"agent_uismoke_cmdk_{int(time.time())}"
    api("/api/agents", "POST", _agent(aid, "UI smoke Kestrel"))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    bad: list[str] = []
    palette = page.locator("[cmdk-input]")

    async def open_palette():
        await page.keyboard.press("Control+k")
        await palette.wait_for(timeout=5_000)

    try:
        await page.goto(f"{APP}/dashboard", wait_until="domcontentloaded")
        await page.locator("main h1").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await open_palette()
        if not await page.evaluate("() => document.activeElement?.hasAttribute('cmdk-input')"):
            bad.append("the field is not focused on open")
        await page.keyboard.type("logs")
        await asyncio.sleep(0.4)
        first = (await page.locator("[cmdk-item][data-selected=true]").inner_text()).splitlines()[0]
        if first != "Logs":
            bad.append(f"'logs' highlights {first!r} first")
        await page.keyboard.press("Enter")
        await page.wait_for_url("**/logs", timeout=10_000)
        if await palette.count():
            bad.append("the palette stayed open after Enter")

        await page.get_by_role("button", name=HELP_BUTTON).first.focus()
        await open_palette()
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)
        if await palette.count() or await page.evaluate("() => document.activeElement?.getAttribute('aria-label')") != HELP_BUTTON:
            bad.append("Escape did not close it with focus back on what opened it")
        await open_palette()
        await page.keyboard.press("Control+k")
        await asyncio.sleep(0.4)
        if await palette.count():
            bad.append("Ctrl+K inside the palette does not close it")
        # A blank workflow, before the agent steps leave an unsaved edit that would hold the page.
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await page.locator("main h1").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await open_palette()
        await page.keyboard.type("new workflow")
        await asyncio.sleep(0.4)
        await page.keyboard.press("Enter")
        name = page.locator("input[aria-label='Workflow name']")
        await name.wait_for(timeout=15_000)
        if await name.input_value() != "New workflow" or "new=" in page.url:
            bad.append(f"New workflow did not open a blank draft ({await name.input_value()!r}, {page.url})")
        await page.get_by_role("button", name=re.compile(r"^Jump to")).click()
        await palette.wait_for(timeout=5_000)
        await page.keyboard.type("kestrel")
        await asyncio.sleep(0.6)
        names = [t.splitlines()[0] for t in await page.locator("[cmdk-item]").all_inner_texts()]
        if names != ["UI smoke Kestrel"]:
            bad.append(f"'kestrel' lists {names}")
        await page.keyboard.press("Enter")
        await page.wait_for_url("**/agents**", timeout=10_000)
        await page.locator("button[aria-current=true]", has_text="UI smoke Kestrel").wait_for(timeout=15_000)
        await asyncio.sleep(0.6)
        if "id=" in page.url:
            bad.append(f"the agents address kept the request: {page.url}")

        # The prompt editor's own Ctrl+K: a link round the selection, and no palette.
        tid = await page.locator("[role=toolbar][aria-label=Formatting]").first.get_attribute("aria-controls")
        editor = page.locator(f"textarea[id='{tid}']")
        await editor.click()
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Control+k")
        await asyncio.sleep(0.4)
        if await palette.count() or not (await editor.input_value()).startswith("["):
            bad.append("Ctrl+K in the markdown editor opened the palette instead of making a link")
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    if bad:
        raise AssertionError("; ".join(bad))
    return "⌘K opens, filters, goes, closes with focus back; Jump row, an agent by name, New workflow; the editor keeps its Ctrl+K"


#: Every page whose title the page frame places (M1a): one x and one y each, at both widths.
TITLE_PAGES = HELP_PAGES + ("/", "/kitchen-sink")


@check
async def check_titlepos(browser) -> str:
    """The title sits at one x and one y on every page and settings tab, at the wide and the phone width (UX1
    measured six x positions in OSS; M1a's page frame never centres its column)."""
    seen: dict[tuple, set] = {}
    bad: list[str] = []
    for width, height in (WIDE, PHONE):
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        page = await ctx.new_page()
        try:
            boxes = {}
            for path in TITLE_PAGES:
                await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
                try:
                    await page.locator("main h1").first.wait_for(timeout=30_000)
                except Exception:
                    bad.append(f"{path} at {width}: no title")
                    continue
                await asyncio.sleep(0.6)
                box = await page.locator("main h1").first.bounding_box()
                boxes[path] = (round(box["x"]), round(box["y"]))
            xs = {b[0] for b in boxes.values()}
            ys = {b[1] for b in boxes.values()}
            seen[(width, "x")], seen[(width, "y")] = xs, ys
            if len(xs) != 1 or len(ys) != 1:
                odd = {p: b for p, b in boxes.items() if b != max(set(boxes.values()), key=list(boxes.values()).count)}
                bad.append(f"at {width}px the titles sit at x {sorted(xs)}, y {sorted(ys)}: {odd}")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return (f"{len(TITLE_PAGES)} pages: x {seen[(WIDE[0], 'x')].pop()} y {seen[(WIDE[0], 'y')].pop()} at {WIDE[0]}px, "
            f"x {seen[(PHONE[0], 'x')].pop()} y {seen[(PHONE[0], 'y')].pop()} at {PHONE[0]}px")


@check
async def check_phonemenu(browser) -> str:
    """On a phone the rail is a drawer behind the top bar's menu button (M1a): it opens with focus inside, a link
    goes and closes it, Escape closes it with focus back on the button, and the page holds one bell."""
    ctx = await browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]})
    page = await ctx.new_page()
    bad: list[str] = []
    try:
        await page.goto(f"{APP}/dashboard", wait_until="domcontentloaded")
        await page.locator("main h1").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        if await page.locator('nav[aria-label="Primary"]').is_visible():
            bad.append("the rail shows beside the page on a phone")
        if await page.locator('button[title="Notifications"]').count() != 1:
            bad.append(f"{await page.locator('button[title=Notifications]').count()} bells in the page")
        menu = page.get_by_role("button", name="Open the menu")
        await menu.click()
        drawer = page.locator('[role=dialog] nav[aria-label="Primary"]')
        await drawer.wait_for(timeout=5_000)
        if not await page.evaluate("() => !!document.activeElement?.closest('[role=dialog]')"):
            bad.append("focus did not move into the menu")
        await drawer.get_by_role("link", name="Agents").click()
        await page.wait_for_url("**/agents", timeout=10_000)
        await asyncio.sleep(0.6)
        if await drawer.count():
            bad.append("the menu stayed open after a link")
        await menu.click()
        await drawer.wait_for(timeout=5_000)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        if await drawer.count() or await page.evaluate("() => document.activeElement?.getAttribute('aria-label')") != "Open the menu":
            bad.append("Escape did not close the menu with focus back on its button")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the phone's menu opens with focus inside, a link closes it, Escape hands focus back; one bell"



# ── M2b: the runs list and the run page as a path ────────────────────────────
# Stubbed runs, so the checks are the same on every install and call no model.

def _m2b_at(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1790762400 + seconds))  # 2026-09-30 10:00 UTC


_M2B_REVIEW = {
    "id": "orch_smoke_m2b", "name": "Smoke supplier review", "description": "", "trigger": "manual",
    "entry_step_id": "gather", "max_total_turns": 100, "timeout_minutes": 30, "updated_at": _m2b_at(-86400),
    "result_key": "review", "state_schema": {"review": {"type": "string", "default": "", "description": "The review"}},
    "steps": [
        {"id": "gather", "name": "Gather", "type": "parallel", "parallel_branches": [["score"], ["find"]],
         "next_step_id": "combine"},
        {"id": "score", "name": "Score suppliers", "type": "agent", "output_key": "scorecard"},
        {"id": "find", "name": "Find issues", "type": "llm", "output_key": "issues"},
        {"id": "combine", "name": "Combine", "type": "merge", "next_step_id": "write", "output_key": "inputs"},
        {"id": "write", "name": "Write the review", "type": "llm", "output_key": "review", "next_step_id": "end"},
        {"id": "end", "name": "End", "type": "end"}]}
_M2B_TRIAGE = {
    "id": "orch_smoke_m2b_triage", "name": "Smoke invoice triage", "description": "", "trigger": "manual",
    "entry_step_id": "check", "max_total_turns": 100, "timeout_minutes": 30, "updated_at": _m2b_at(-86400),
    "state_schema": {},
    "steps": [
        {"id": "check", "name": "Check totals", "type": "transform", "next_step_id": "review", "output_key": "triage"},
        {"id": "review", "name": "Needs review?", "type": "if_else", "if_true_step_id": "approve",
         "if_false_step_id": "draft"},
        {"id": "approve", "name": "Accounts payable approval", "type": "human", "next_step_id": "draft",
         "output_key": "approval"},
        {"id": "draft", "name": "Draft payment note", "type": "llm", "output_key": "note"}]}


def _m2b_entry(step_id, name, kind, start, seconds, status="completed", **extra):
    return {"step_id": step_id, "step_name": name, "step_type": kind, "status": status,
            "started_at": _m2b_at(start), "ended_at": _m2b_at(start + seconds), "duration_seconds": seconds, **extra}


_M2B_TRACEBACK = ('RuntimeError: Transform error: Transform failed (exit 1): Traceback (most recent call last):\n'
                  '  File "/tmp/tmpsmoke/script.py", line 19, in <module>\n'
                  '    total = round(sum(line.qty * line.unit_price for line in inv.lines), 2)\n'
                  '                                                          ^^^^^^^^^\n  File "/tmp/tmp')
_M2B_RUNS = {
    "run_smoke_m2b_done": {
        "run_id": "run_smoke_m2b_done", "orchestration_id": "orch_smoke_m2b", "status": "completed",
        "session_id": None, "started_at": _m2b_at(0), "ended_at": _m2b_at(2), "current_step_id": "write",
        "waiting_for_human": False, "total_tokens_used": 1440, "total_cost_usd": 0, "tasks": [], "error": None,
        "step_history": [
            _m2b_entry("score", "Score suppliers", "agent", 0, 0.6, output_key="scorecard"),
            _m2b_entry("find", "Find issues", "llm", 0, 0.8, output_key="issues"),
            _m2b_entry("gather", "Gather", "parallel", 0, 0.9),
            _m2b_entry("combine", "Combine", "merge", 1, 0.0, output_key="inputs"),
            _m2b_entry("write", "Write the review", "llm", 1, 0.7, output_key="review")],
        "shared_state": {"scorecard": "Saltmarsh: 6/10", "issues": "Two late deliveries.",
                         "inputs": {"a": 1}, "review": "The smoke review for the operations team."}},
    "run_smoke_m2b_fail": {
        "run_id": "run_smoke_m2b_fail", "orchestration_id": "orch_smoke_m2b_triage", "status": "failed",
        "session_id": None, "started_at": _m2b_at(0), "ended_at": _m2b_at(1), "current_step_id": "check",
        "waiting_for_human": False, "total_tokens_used": 0, "total_cost_usd": 0, "tasks": [],
        "error": _M2B_TRACEBACK, "shared_state": {},
        "step_history": [_m2b_entry("check", "Check totals", "transform", 0, 0.4, "failed", error=_M2B_TRACEBACK)]},
    "run_smoke_m2b_turns": {
        "run_id": "run_smoke_m2b_turns", "orchestration_id": "orch_smoke_m2b", "status": "failed",
        "session_id": None, "started_at": _m2b_at(0), "ended_at": _m2b_at(30), "current_step_id": "score",
        "waiting_for_human": False, "total_tokens_used": 9000, "total_cost_usd": 0.0123, "tasks": [],
        "error": "Global turn limit (100) exceeded", "shared_state": {}, "step_history": []},
    "run_smoke_m2b_wait": {
        "run_id": "run_smoke_m2b_wait", "orchestration_id": "orch_smoke_m2b_triage", "status": "paused",
        "session_id": None, "started_at": _m2b_at(0), "ended_at": None, "current_step_id": "approve",
        "waiting_for_human": True, "total_tokens_used": 0, "total_cost_usd": 0, "tasks": [], "error": None,
        "human_prompt": "Invoice INV-1047 totals **$2,310.00**. Approve payment?",
        "human_fields": [{"name": "approved", "type": "boolean", "label": "Approve payment"},
                         {"name": "note", "type": "textarea", "label": "Note for the ledger"}],
        "shared_state": {"triage": {"total": 2310}},
        "step_history": [_m2b_entry("check", "Check totals", "transform", 0, 0.5, output_key="triage"),
                         _m2b_entry("review", "Needs review?", "if_else", 1, 0.0),
                         {"step_id": "approve", "step_name": "Accounts payable approval", "step_type": "human",
                          "status": "paused", "started_at": _m2b_at(1)}]},
}
_M2B_STREAM = "".join([
    _frame({"type": "orchestration_start", "run_id": "run_smoke_m2b_done"}, "1"),
    _frame({"type": "step_start", "orch_step_id": "write", "step_name": "Write the review", "step_type": "llm"}, "2"),
    _frame({"type": "final", "orch_step_id": "write", "response": "The smoke review for the operations team."}, "3"),
    _frame({"type": "step_complete", "orch_step_id": "write", "step_name": "Write the review",
            "duration_seconds": 0.7}, "4"),
    _frame({"type": "stream_complete"})])


def _m2b_summary(run: dict) -> dict:
    return {k: run.get(k) for k in ("run_id", "orchestration_id", "status", "started_at", "ended_at",
                                    "current_step_id", "waiting_for_human", "total_cost_usd", "session_id",
                                    "error")} | {"steps_completed": len(run["step_history"]), "score": None,
                                                 "score_regression": False}


async def _m2b_page(browser, path: str, width=WIDE, runs: dict | None = None, theme: str | None = None):
    """A page over the stubbed runs. Returns (ctx, page, the /api/orchestrations requests it made)."""
    runs = runs if runs is not None else _M2B_RUNS
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    if theme:
        await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
    seen: list[str] = []
    for orch in (_M2B_REVIEW, _M2B_TRIAGE):
        await ctx.route(f"**/api/orchestrations/{orch['id']}", _answer(orch))
    await ctx.route(re.compile(r".*/api/orchestrations(\?.*)?$"), _answer([_M2B_REVIEW, _M2B_TRIAGE]))
    await ctx.route(re.compile(r".*/api/orchestrations/runs(\?.*)?$"),
                    _answer([_m2b_summary(r) for r in runs.values()]))
    for rid, run in runs.items():
        # With or without `?definition=1`, which the run page has asked for since F443 (F820: a glob with no
        # query left every run-page check reading "Run not found").
        await ctx.route(re.compile(rf".*/api/orchestrations/runs/{rid}(\?.*)?$"), _answer(run))
        await ctx.route(f"**/api/orchestrations/runs/{rid}/children", _answer([]))
        await ctx.route(f"**/api/orchestrations/runs/{rid}/events/stream*",
                        _answer(_M2B_STREAM, content_type="text/event-stream"))
    page = await ctx.new_page()
    page.on("request", lambda r: seen.append(r.method + " " + urllib.parse.urlsplit(r.url).path)
            if "/api/orchestrations" in r.url else None)
    await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
    await page.locator("main h1").first.wait_for(timeout=30_000)
    await asyncio.sleep(SETTLE)
    return ctx, page, seen


@check
async def check_runslist(browser) -> str:
    """Runs is a list grouped by day with a path per run (M2b): pills count and filter, a row links to its run's
    page, a free run reads `$0`, an empty trigger reads Manual, and a failed run names the step it stopped at."""
    ctx, page, _ = await _m2b_page(browser, "/runs")
    bad: list[str] = []
    try:
        table = page.get_by_role("table", name="Runs")
        rows = table.locator("a[data-row-open]")
        if await rows.count() != len(_M2B_RUNS):
            bad.append(f"{await rows.count()} rows, not {len(_M2B_RUNS)}")
        failed = page.get_by_role("button", name=re.compile(r"^Failed"))
        if (await failed.inner_text()).split()[-1] != "2":
            bad.append(f"the Failed pill reads {await failed.inner_text()!r}")
        if await table.locator('[role="img"]').count() < len(_M2B_RUNS):
            bad.append("a row has no path glyph")
        done = table.locator('[role="row"]', has=page.locator('a[href="/runs/run_smoke_m2b_done"]'))
        text = await done.inner_text()
        if "$0" not in text.split() or "Manual" not in text:
            bad.append(f"the free, manual run reads {text!r}")
        fail_row = await table.locator('[role="row"]', has=page.locator('a[href="/runs/run_smoke_m2b_fail"]')).inner_text()
        if "Failed · Check totals" not in fail_row:
            bad.append(f"the failed run reads {fail_row!r}")
        await failed.click()
        await asyncio.sleep(0.4)
        if await failed.get_attribute("aria-pressed") != "true" or await rows.count() != 2:
            bad.append(f"Failed shows {await rows.count()} rows")
        await page.get_by_role("button", name=re.compile(r"^All")).click()
        await page.get_by_role("searchbox", name="Search runs…").fill("triage")
        await asyncio.sleep(0.4)
        if await rows.count() != 2:
            bad.append(f"searching 'triage' leaves {await rows.count()} rows")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "pills count and filter, search narrows, rows link to /runs/<id>, $0 and Manual, the failed step named"


@check
async def check_runpath(browser) -> str:
    """A completed run reads as a path (M2b): the followed line, a timeline of real starts with a Parallel's
    branches indented under it and model calls in the machine colour, a step that opens to its output, the
    Result first under What it produced — and the page asks only for the run, its workflow, its nested runs and
    its events."""
    ctx, page, seen = await _m2b_page(browser, "/runs/run_smoke_m2b_done")
    bad: list[str] = []
    try:
        if (await page.locator("main h1").first.inner_text()).strip() != "Smoke supplier review":
            bad.append("the title is not the workflow")
        line = "Followed the saved workflow: 5 of 5 steps, in order."
        if not await page.get_by_text(line).count():
            bad.append(f"no '{line}'")
        timeline = page.get_by_role("table", name="Steps on a timeline")
        rows = timeline.locator(':scope > [role="rowgroup"]')
        if await rows.count() != 5:
            bad.append(f"{await rows.count()} timeline rows, not 5")
        order = [t.split("\n")[0].strip() for t in await rows.all_inner_texts()]
        if order[:1] != ["Gather"] or "Combine" not in order or order.index("Combine") < 3:
            bad.append(f"the timeline reads {order}")
        colours = await page.evaluate("""() => {
            const cs = getComputedStyle(document.documentElement);
            const bars = [...document.querySelectorAll('[aria-label="Steps on a timeline"] [role=cell] > span.absolute.top-1')];
            const probe = document.createElement('i'); document.body.append(probe);
            const resolve = (v) => { probe.style.color = v; return getComputedStyle(probe).color; };
            const out = { machine: resolve(cs.getPropertyValue('--machine')), bars: bars.map(b => getComputedStyle(b).backgroundColor) };
            probe.remove(); return out; }""")
        if colours["bars"].count(colours["machine"]) != 3:
            bad.append(f"{colours['bars'].count(colours['machine'])} bars in the machine colour, not the 3 model calls")
        indented = await timeline.locator(".border-l").count()
        if indented != 2:
            bad.append(f"{indented} rows indented under the Parallel, not 2")
        await page.get_by_role("button", name="Show output of Write the review").click()
        await asyncio.sleep(0.3)
        if not await timeline.get_by_text("The smoke review for the operations team.").count():
            bad.append("opening a step does not show its output")
        produced = await page.locator("main ul").first.locator("li").first.inner_text()
        if not produced.startswith("review") or "Result" not in produced:
            bad.append(f"What it produced starts with {produced[:40]!r}")
        allowed = {"GET /api/orchestrations/runs/run_smoke_m2b_done", "GET /api/orchestrations/orch_smoke_m2b",
                   "GET /api/orchestrations/runs/run_smoke_m2b_done/children",
                   "GET /api/orchestrations/runs/run_smoke_m2b_done/events/stream"}
        extra = sorted(set(seen) - allowed)
        if extra:
            bad.append(f"the page asked for {extra}")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "followed line, timeline with branches indented and model bars cyan, a step opens, Result first, 4 reads"


@check
async def check_runfailure(browser) -> str:
    """A failed run leads with one plain sentence and the fix (M2b): the Python step's line, Open in editor and
    Replay, and its traceback only behind Technical details; a run-level limit says it in words too."""
    ctx, page, _ = await _m2b_page(browser, "/runs/run_smoke_m2b_fail")
    bad: list[str] = []
    try:
        card = page.get_by_role("region", name="Why this run failed")
        said = (await card.locator("h2").inner_text()).strip()
        if said != "Check totals’s Python code raised an error on line 19.":
            bad.append(f"the failure reads {said!r}")
        trace = page.get_by_text("Traceback (most recent call last)")
        if await trace.count() and await trace.first.is_visible():
            bad.append("the traceback shows before Technical details is opened")
        editor = card.get_by_role("link", name="Open in editor")
        if await editor.get_attribute("href") != "/workflows?run=run_smoke_m2b_fail":
            bad.append(f"Open in editor goes to {await editor.get_attribute('href')}")
        if not await card.get_by_role("button", name="Replay").count():
            bad.append("the card offers no Replay")
        await card.locator("summary", has_text="Technical details").click()
        await asyncio.sleep(0.3)
        if not await trace.first.is_visible():
            bad.append("Technical details does not hold the traceback")
        if not await page.get_by_text("Stopped at step 1 of 4: Check totals.").count():
            bad.append("no 'Stopped at step 1 of 4'")
        await page.goto(f"{APP}/runs/run_smoke_m2b_turns", wait_until="domcontentloaded")
        await page.get_by_role("region", name="Why this run failed").wait_for(timeout=20_000)
        said = (await page.get_by_role("region", name="Why this run failed").locator("h2").inner_text()).strip()
        if said != "This run used all 100 of its turns.":
            bad.append(f"the turn limit reads {said!r}")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the plain sentence and its fix lead; the traceback waits behind Technical details; the turn limit in words"


@check
async def check_runanswer(browser) -> str:
    """A waiting run is answered on its page (M2b): its steps as a stepper with the waiting step marked, the
    step's fields, and the answer posted keyed by name; the page then reads the run on, and the card goes. The step's
    one Yes / No is two buttons since M2a (Naveen's call): "Approve payment" posts it true with the note."""
    runs = json.loads(json.dumps(_M2B_RUNS))
    answered = json.loads(json.dumps(runs["run_smoke_m2b_wait"]))
    answered.update(status="completed", waiting_for_human=False, human_prompt=None, ended_at=_m2b_at(4))
    answered["step_history"][-1].update(status="completed", ended_at=_m2b_at(3), duration_seconds=2.0)
    sent: list[dict] = []
    ctx, page, _ = await _m2b_page(browser, "/runs/run_smoke_m2b_wait", runs=runs)
    bad: list[str] = []
    try:
        async def human(route):
            sent.append(json.loads(route.request.post_data or "{}"))
            await route.fulfill(status=200, content_type="text/event-stream", body=_frame({"type": "done"}))
        await ctx.route("**/api/orchestrations/runs/run_smoke_m2b_wait/human-input", human)
        steps = page.get_by_role("list", name="Steps")
        state = await steps.locator("li").nth(2).inner_text()
        if "Accounts payable approval" not in state or "Needs input" not in state:
            bad.append(f"the third step reads {state!r}")
        if "Not reached" not in await steps.locator("li").nth(3).inner_text():
            bad.append("the step after the question is not 'Not reached'")
        card = page.get_by_role("region", name="Waiting for a person")
        if await card.get_by_role("checkbox", name="Approve payment").count():
            bad.append("the lone Yes / No is still a checkbox, not the two buttons")
        await card.get_by_role("textbox", name="Note for the ledger").fill("Paid on Friday")
        # The page reads the answered run from here on.
        await ctx.route(re.compile(r".*/api/orchestrations/runs/run_smoke_m2b_wait(\?.*)?$"), _answer(answered))
        await card.get_by_role("button", name="Approve payment").click()
        await page.get_by_text("Answer sent — the run is resuming.").wait_for(timeout=10_000)
        if sent != [{"response": {"approved": True, "note": "Paid on Friday"}}]:
            bad.append(f"the answer posted {sent}")
        try:
            await card.wait_for(state="detached", timeout=10_000)
        except Exception:
            bad.append("the question stayed up after it was answered")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the stepper marks the waiting step; the fields post keyed by name; the page reads the run on"



@check
async def check_emptyanswer(browser) -> str:
    """An empty answer is never sent (F842, Naveen 2026-10-23): on the run page and on Home, a free-text question's
    Send answer stays disabled until something is typed — blank space is nothing — and then posts what was typed.
    On prod an empty "Send answer" on the run page resumed a run past its approval."""
    def free_text(runs: dict) -> dict:
        for run in runs.values():
            if run.get("waiting_for_human"):
                run["human_fields"] = []
        return runs

    bad: list[str] = []
    for path, runs in (("/runs/run_smoke_m2b_wait", free_text(json.loads(json.dumps(_M2B_RUNS)))),
                       ("/dashboard", free_text(_m2a_runs()))):
        sent: list[dict] = []
        ctx, page, _ = await _m2b_page(browser, path, runs=runs)
        try:
            async def human(route):
                sent.append(json.loads(route.request.post_data or "{}"))
                await route.fulfill(status=200, content_type="text/event-stream", body=_frame({"type": "done"}))
            await ctx.route("**/api/orchestrations/runs/*/human-input", human)
            card = page.get_by_role("region", name="Waiting for a person").first
            box = card.get_by_role("textbox", name="Your answer")
            send = card.get_by_role("button", name="Send answer", exact=True)
            if not await send.is_disabled():
                bad.append(f"{path}: Send answer is enabled with nothing typed")
            await box.fill("   ")
            if not await send.is_disabled():
                bad.append(f"{path}: Send answer is enabled for blank space")
            await box.fill("Looks right, go ahead")
            if await send.is_disabled():
                bad.append(f"{path}: Send answer stays disabled after an answer was typed")
            else:
                await send.click()
                await page.get_by_text("Answer sent — the run is resuming.").first.wait_for(timeout=10_000)
            if sent != [{"response": "Looks right, go ahead"}]:
                bad.append(f"{path}: posted {sent}")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "run page and Home: Send answer waits for words (blank space is none), then posts them once"



@check
async def check_runcontrast(browser) -> str:
    """The runs list and the run page meet AA in both themes (M2b): the facts, the followed line on its tint, the
    failure card with Technical details open, the question's card, an opened step, the stepper and the log."""
    bad: list[str] = []
    shots = (("/runs", None), ("/runs/run_smoke_m2b_done", "Show output of Write the review"),
             ("/runs/run_smoke_m2b_fail", "Technical details"), ("/runs/run_smoke_m2b_wait", None),
             ("/runs/run_smoke_m2b_turns", None))
    for theme in ("light", "dark"):
        for path, open_first in shots:
            ctx, page, _ = await _m2b_page(browser, path, width=(WIDE[0], 2200), theme=theme)
            try:
                if open_first == "Technical details":
                    await page.locator("summary", has_text="Technical details").click()
                elif open_first:
                    await page.get_by_role("button", name=open_first).click()
                await asyncio.sleep(0.6)
                for row in await page.evaluate(CONTRAST):
                    if any(row["fg"] == fg and row["bg"] == bg for fg, bg, _ in CONTRAST_WAIVED):
                        continue
                    bad.append(f"{theme} {path}: {row['ratio']}:1 (needs {row['need']}) "
                               f"{row['fg']} on {row['bg']} — {row['txt'][:24]!r}")
            finally:
                await ctx.close()
    if bad:
        raise AssertionError(f"{len(bad)} below AA: " + "; ".join(bad[:8]))
    return f"{len(shots)} run screens in both themes, every text at AA"



# ── Home (M2a): what needs you first, answered in place ──────────────────────────────────────────────────────────


def _m2a_runs() -> dict:
    """M2b's stubbed runs, moved to an hour ago so they fall in this week, plus a second question waiting."""
    shift = time.time() - 3600 - 1790762400
    def move(v):
        if isinstance(v, str) and re.fullmatch(r"2026-\d\d-\d\dT\d\d:\d\d:\d\dZ", v):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ")) + shift))
        if isinstance(v, dict):
            return {k: move(x) for k, x in v.items()}
        if isinstance(v, list):
            return [move(x) for x in v]
        return v
    runs = move(json.loads(json.dumps(_M2B_RUNS)))
    second = json.loads(json.dumps(runs["run_smoke_m2b_wait"]))
    second.update(run_id="run_smoke_m2a_wait2", human_prompt="Invoice INV-1048 totals **$2,544.00**. Approve payment?")
    runs["run_smoke_m2a_wait2"] = second
    return runs


@check
async def check_homeanswer(browser) -> str:
    """Home answers what waits in place (M2a): each waiting run is the run page's own card, its one Yes / No two
    buttons; Approve and Decline post exactly what the run page posts, to the same address; an answered card says so
    and the count goes down; Open run goes to the run's page."""
    runs = _m2a_runs()
    sent: list[tuple[str, dict]] = []
    ctx, page, _ = await _m2b_page(browser, "/dashboard", runs=runs)
    bad: list[str] = []
    try:
        for rid in ("run_smoke_m2b_wait", "run_smoke_m2a_wait2"):
            async def human(route, rid=rid):
                sent.append((urllib.parse.urlsplit(route.request.url).path, json.loads(route.request.post_data or "{}")))
                await route.fulfill(status=200, content_type="text/event-stream", body=_frame({"type": "done"}))
            await ctx.route(f"**/api/orchestrations/runs/{rid}/human-input", human)
        cards = page.get_by_role("region", name="Waiting for a person")
        await cards.first.wait_for(timeout=15_000)
        heading = (await page.locator("#home-needs-you").inner_text()).strip()
        if heading != "Needs you · 2":
            bad.append(f"the section reads {heading!r}")
        if await cards.count() != 2:
            bad.append(f"{await cards.count()} cards for two waiting runs")
        first = cards.filter(has_text="INV-1047")
        if "Smoke invoice triage" not in await first.inner_text():
            bad.append("the card does not name its workflow")
        href = await first.get_by_role("link", name="Open run").get_attribute("href")
        if href != "/runs/run_smoke_m2b_wait":
            bad.append(f"Open run goes to {href!r}")
        if await first.get_by_role("button", name="Send answer").count():
            bad.append("a lone Yes / No still has Send answer")
        await first.get_by_role("textbox", name="Note for the ledger").fill("Paid on Friday")
        # From here the list reads the first run as answered.
        answered = {k: v for k, v in runs.items() if k != "run_smoke_m2b_wait"}
        await ctx.route(re.compile(r".*/api/orchestrations/runs(\?.*)?$"),
                        _answer([_m2b_summary(r) for r in answered.values()]))
        await first.get_by_role("button", name="Approve payment").click()
        await page.get_by_role("status").filter(has_text="Answer sent — the run is resuming.").first.wait_for(timeout=10_000)
        await cards.filter(has_text="INV-1048").get_by_role("button", name="Decline").click()
        await page.get_by_role("status").filter(has_text="Answer sent").nth(1).wait_for(timeout=10_000)
        want = [("/api/orchestrations/runs/run_smoke_m2b_wait/human-input", {"response": {"approved": True, "note": "Paid on Friday"}}),
                ("/api/orchestrations/runs/run_smoke_m2a_wait2/human-input", {"response": {"approved": False, "note": ""}})]
        if sent != want:
            bad.append(f"the answers posted {sent}")
        if await cards.count():
            bad.append("a card stayed up after its answer")
        heading = (await page.locator("#home-needs-you").inner_text()).strip()
        if heading != "Needs you · 0":
            bad.append(f"after both answers the section reads {heading!r}")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "two waiting runs answered in place, Approve and Decline posting the run page's request"


@check
async def check_homestats(browser) -> str:
    """Home's head, strip and week say how things stand (M2a): the greeting, one status line, the four figures with a
    dot each and the numbers in the text colour, a bar per run of the week, and every run row opening its run's page."""
    ctx, page, _ = await _m2b_page(browser, "/dashboard", runs=_m2a_runs())
    bad: list[str] = []
    try:
        await page.get_by_role("region", name="Waiting for a person").first.wait_for(timeout=15_000)
        title = (await page.locator("main h1").first.inner_text()).strip()
        if not re.match(r"^Good (morning|afternoon|evening)\b", title):
            bad.append(f"the title reads {title!r}")
        line = (await page.locator("main header p").first.inner_text()).strip()
        if line != "Two things need you. Two runs failed this week.":
            bad.append(f"the status line reads {line!r}")
        strip = await page.evaluate("""() => [...document.querySelectorAll('dl[aria-label="This week"] > div')].map(d => ({
            k: d.querySelector('dt').innerText.trim(), v: d.querySelector('dd').innerText.trim(),
            s: (d.querySelectorAll('dd')[1] || {}).innerText || '', dot: !!d.querySelector('dt span[aria-hidden]'),
            colour: getComputedStyle(d.querySelector('dd')).color, text: getComputedStyle(document.querySelector('main h1')).color }))""")
        got = {c["k"]: (c["v"], c["s"].strip()) for c in strip}
        want = {"Running now": "0", "Waiting on you": "2", "Failed · 7 days": "2", "Completed · 7 days": "20%"}
        for k, v in want.items():
            if got.get(k, ("?",))[0] != v:
                bad.append(f"{k} shows {got.get(k)}")
        if got.get("Completed · 7 days", ("", ""))[1] != "1 of 5 runs":
            bad.append(f"Completed's line is {got.get('Completed · 7 days')}")
        for c in strip:
            if not c["dot"] or c["colour"] != c["text"]:
                bad.append(f"{c['k']}: dot {c['dot']}, number {c['colour']} (text {c['text']})")
        summary = (await page.locator("p.sr-only", has_text="This week").inner_text()).strip()
        if summary != "This week, 5 runs: 1 completed, 2 failed, 2 waiting on a person.":
            bad.append(f"the week says {summary!r}")
        recent = page.get_by_role("list", name="Recent runs")
        hrefs = await recent.get_by_role("link").evaluate_all("els => els.map(e => e.getAttribute('href'))")
        if not hrefs or not all(h.startswith("/runs/run_smoke_") for h in hrefs):
            bad.append(f"Recent runs open {hrefs}")
        active = await page.get_by_role("list", name="Active now").get_by_role("link").count()
        if active != 2:
            bad.append(f"Active now lists {active} runs, not the two waiting")
        for label in ("Total spend", "Requests", "Tokens", "Cache saved", "Spend by model", "Workspace", "Coming up"):
            if not await page.get_by_text(label, exact=True).count():
                bad.append(f"no {label!r}")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "greeting, status line, the strip in quiet colour, the week's bars, run rows to their pages"


@check
async def check_homeempty(browser) -> str:
    """Home with nothing yet (M2a): one calm line under Needs you, no card, and each section's own empty words.
    A fresh Home leads with its first-workflow path since Naveen's ask (`homestart`), so this one has it hidden, as
    "Don't show again" leaves it."""
    ctx, page, _ = await _m2b_page(browser, "/dashboard", runs={})
    bad: list[str] = []
    try:
        await page.evaluate("localStorage.setItem('synapseHomeStartHidden', '1')")
        await page.reload(wait_until="domcontentloaded")
        await page.get_by_text("Nothing is waiting on you.").wait_for(timeout=15_000)
        if await page.locator("#home-start").count():
            bad.append("the path shows after Don't show again")
        if await page.get_by_role("region", name="Waiting for a person").count():
            bad.append("a card with nothing waiting")
        heading = (await page.locator("#home-needs-you").inner_text()).strip()
        if heading != "Needs you · 0":
            bad.append(f"the section reads {heading!r}")
        line = (await page.locator("main header p").first.inner_text()).strip()
        if line != "Nothing needs you. Nothing has run this week.":
            bad.append(f"the status line reads {line!r}")
        for words in ("Nothing running.", "No runs this week.", "No runs yet", "all clear", "Nothing runs on its own yet."):
            if not await page.get_by_text(words, exact=False).count():
                bad.append(f"no {words!r}")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "nothing waiting reads as one calm line, and every section says it is empty"



async def _composer(page) -> str:
    """What the AI builder's composer holds, once its panel is open."""
    box = page.locator('[data-testid="assistant-panel"]').get_by_role("textbox", name="Message Synapse AI")
    await box.wait_for(timeout=10_000)
    return await box.input_value()


@check
async def check_homestart(browser) -> str:
    """A new workspace's Home leads with its first workflow as a path (M2a, Naveen's ask): steps that tick themselves,
    the current one with its action and tip; "describe what you want to automate" and a suggestion each open Build
    with AI with the words typed, unsent; Don't show again puts it away."""
    ctx, page, _ = await _m2b_page(browser, "/dashboard", runs={})
    bad: list[str] = []
    try:
        await page.evaluate("localStorage.removeItem('synapseHomeStartHidden'); localStorage.removeItem('synapseHomeStartOpen')")
        await page.reload(wait_until="domcontentloaded")
        path = page.locator("section[aria-labelledby='home-start']")
        await path.wait_for(timeout=15_000)
        steps = page.get_by_role("list", name="Your first workflow").locator("li")
        titles = [(await steps.nth(i).locator("p").first.inner_text()).strip().split("\n")[0] for i in range(await steps.count())]
        if [t.split(" · ")[0] for t in titles][:5] != ["Connect a model", "Build a workflow", "Run it once",
                                                       "Answer its first question", "Let it run on its own"]:
            bad.append(f"the steps read {titles}")
        current = page.locator("li[aria-current='step']")
        if await current.count() != 1:
            bad.append(f"{await current.count()} current steps")
        if await page.locator("#home-needs-you").count():
            bad.append("Needs you shows above a fresh path with nothing waiting")
        line = (await page.locator("main header p").first.inner_text()).strip()
        if line != "Let’s get your first workflow running. It takes about ten minutes.":
            bad.append(f"the status line reads {line!r}")
        await page.get_by_label("Or describe what you want to automate").fill("Every Friday, sum up the week's invoices")
        await path.get_by_role("button", name="Build with AI").last.click()
        if (await _composer(page)) != "Every Friday, sum up the week's invoices":
            bad.append(f"the builder opened with {await _composer(page)!r}")
        sent = await page.evaluate("[...document.querySelectorAll('[data-testid=\"assistant-panel\"] *')].some(e => e.innerText === \"Every Friday, sum up the week's invoices\" && e.tagName !== 'TEXTAREA')")
        if sent:
            bad.append("the words were sent, not left in the composer")
        await page.get_by_role("button", name="Close Synapse AI").click()
        await page.get_by_role("button", name="A weekly report").click()
        if not (await _composer(page)).startswith("Every Monday at 9, write a short report"):
            bad.append(f"the suggestion opened the builder with {await _composer(page)!r}")
        await page.get_by_role("button", name="Close Synapse AI").click()
        await page.get_by_role("button", name="Don’t show again").click()
        await page.get_by_text("Nothing is waiting on you.").wait_for(timeout=5_000)
        if await path.count():
            bad.append("Don't show again left the path up")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the path with one current step, the describe field and a suggestion fill the builder unsent, it hides"


@check
async def check_homeways(browser) -> str:
    """With nothing scheduled, Coming up says how a workflow starts on its own (M2a, Naveen's ask): a schedule, a
    message, your systems, each to its page; the tip names the workflow run most by hand this week and opens Build
    with AI with the sentence typed. Once a workflow has run, the path is one line that opens and closes."""
    ctx, page, _ = await _m2b_page(browser, "/dashboard", runs=_m2a_runs())
    bad: list[str] = []
    try:
        await page.evaluate("localStorage.removeItem('synapseHomeStartHidden'); localStorage.removeItem('synapseHomeStartOpen')")
        await page.reload(wait_until="domcontentloaded")
        ways = page.get_by_role("list", name="Ways a workflow starts on its own")
        await ways.wait_for(timeout=15_000)
        links = await ways.get_by_role("link").evaluate_all("els => els.map(e => [e.innerText.trim(), e.getAttribute('href')])")
        if links != [["Add a schedule", "/schedules"], ["Channels", "/channels"], ["API keys", "/settings/api_keys"]]:
            bad.append(f"the ways link to {links}")
        # The ways show once the schedules are read; the tip waits for the runs too.
        tip = page.get_by_text("“Smoke invoice triage” ran 3 times by hand this week. Put it on a schedule?")
        try:
            await tip.wait_for(timeout=15_000)
        except Exception:
            bad.append("no tip for the workflow run most by hand")
        await page.get_by_role("button", name="Schedule it with AI").click()
        if (await _composer(page)) != "Put the workflow “Smoke invoice triage” on a schedule.":
            bad.append(f"Schedule it with AI opened the builder with {await _composer(page)!r}")
        await page.get_by_role("button", name="Close Synapse AI").click()
        folded = page.locator("section[aria-labelledby='home-start']")
        if "Next:" not in await folded.inner_text():
            bad.append(f"the path after a run reads {await folded.inner_text()!r}")
        await folded.get_by_role("button", name="Show").click()
        await page.get_by_role("list", name="Your first workflow").wait_for(timeout=5_000)
        await page.locator("section[aria-labelledby='home-start']").get_by_role("button", name="Hide").click()
        if await page.get_by_role("list", name="Your first workflow").count():
            bad.append("Hide left the steps open")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "three ways to their pages, the by-hand tip fills the builder, the folded path opens and closes"


# ── M2c: one composer for chat and the builder; the plan as a card ───────────

#: A real plan question's shape (the dev stack, 2026-09-23), cut short: the
#: builder's lead-in, its rule, then the plan as markdown.
PLAN_QUESTION = (
    "Here is the proposed plan. Reply **approve** (or 'ok' / 'looks good' / 'yes') to build it as-is, "
    "or describe the changes you want and it will be redrafted.\n\n---\n\n"
    "## Overview\nThis orchestration drafts a weekly update email for Lumen Dental every Friday, summarizing what "
    "their three competitors published that week. The drafted email is reviewed by a human before being sent.\n\n"
    "## State schema\n- `current_date` (str): today.\n- `draft_email` (str): the draft.\n\n"
    "## Steps\n1. **Competitor Research Agent** (agent)\n   - What it does: Researches competitor publications.\n"
    "   - Type: NEW agent\n\n2. **Email Drafter** (agent)\n   - What it does: Drafts the client email.\n\n"
    "3. **Human Review** (human)\n   - What it does: Reviews the drafted email before it is sent.\n"
    "   - Fields collected: `{\"approved\": {\"type\": \"boolean\", \"label\": \"Approve?\"}}`\n\n"
    "4. **Email Sender** (tool)\n   - Forced tool: Email Sender\n\n5. **end** (end)\n   - Terminates the orchestration.\n\n"
    "## Agents summary\n### New agents to create\n- **Competitor Research Agent**\n  - name: `Competitor Research Agent`\n"
    "- **Email Drafter**\n  - name: `Email Drafter`\n\n"
    "## Flow\n```flow\nCompetitor Research Agent (agent)\n    ↓\nend (end)\n```\n\n"
    "## Schedule\n- **When**: Every Friday at 9 AM UTC. `0 9 * * 5`\n\n## Open questions\n- None"
)


def _parked(prompt: str, step_id: str = "step_human_review") -> dict:
    return {
        "client_session_id": "conv-uismoke-plan", "run_id": RUN_ID, "status": "paused",
        "turns": [{"user": "a weekly competitor email for Lumen Dental", "assistant": ""}],
        "pending": {"run_id": RUN_ID, "field": "feedback", "step_id": step_id, "prompt": prompt},
    }


async def _resumes(page) -> list:
    calls = await page.evaluate("() => window.__calls")
    return [json.loads(c["body"]) for c in calls if "/api/builder/resume" in c["url"] and c["body"]]


@check
async def check_plancard(browser) -> str:
    """The builder's plan reads as a card (M2c): numbered steps, each coloured by who does it, and the answers as
    buttons — Build it sends the word the question asks for, exactly as typing it does, and only once; Change
    something goes to the composer; a plan the card cannot read is shown as its words with the same answers."""
    bad: list[str] = []
    ctx, page = await _builder_back(browser, _parked(PLAN_QUESTION))
    try:
        await _build_with_ai(page)
        card = page.locator('[data-testid="assistant-panel"] section[aria-label="The plan"]')
        await card.wait_for(timeout=10_000)
        rows = card.get_by_role("list", name="Steps").locator("li")
        names = [(await rows.nth(i).inner_text()).replace("\n", " ") for i in range(await rows.count())]
        if len(names) != 5 or "Human Review" not in names[2] or "a person" not in names[2]:
            bad.append(f"the steps read {names}")
        if "calls a model" not in names[0] or "Agent" not in names[0]:
            bad.append(f"the first step reads {names[:1]}")
        text = await card.inner_text()
        for words in ("Needs your answer", "Every Friday at 9 AM UTC", "Competitor Research Agent, Email Drafter"):
            if words not in text:
                bad.append(f"the card does not say {words!r}")
        if "Open questions" in text:
            bad.append("an Open questions box for a plan whose questions are 'None'")
        await card.get_by_role("button", name="Change something").click()
        focused = await page.evaluate("() => document.activeElement && document.activeElement.getAttribute('aria-label')")
        if focused != "Message Synapse AI":
            bad.append(f"Change something left the focus on {focused!r}")
        if await _resumes(page):
            bad.append("Change something sent an answer")
        build = card.get_by_role("button", name="Build it")
        await build.click()
        await asyncio.sleep(0.6)
        sent = await _resumes(page)
        if sent != [{"run_id": RUN_ID, "response": {"feedback": "approve"}, "images": []}] and \
                [s.get("response") for s in sent] != [{"feedback": "approve"}]:
            bad.append(f"Build it sent {sent}")
    finally:
        await ctx.close()

    # An unreadable plan, and a change plan.
    unread = "Here is the proposed plan. Reply **approve** to build it.\n\n---\n\nOne step that summarises the tickets."
    change = ("Here is what I would do. Reply **approve** (or 'ok' / 'yes' / 'go ahead') and I will do it, or tell me "
              "what to change.\n\n---\n\n**Plan**\n\n- `update_agent` — shorten the system prompt of Summarizer\n"
              "- ⚠️ DELETES: `delete_schedule` — the old Monday schedule")
    for prompt, step, button, words in ((unread, "step_human_review", "Build it", "One step that summarises the tickets."),
                                        (change, "step_manage_review", "Go ahead", "Deletes: delete_schedule")):
        ctx, page = await _builder_back(browser, _parked(prompt, step))
        try:
            await _build_with_ai(page)
            await asyncio.sleep(0.6)
            panel = await _panel_text(page)
            if words not in panel:
                bad.append(f"{step}: the card does not show {words!r}")
            go = page.locator('[data-testid="assistant-panel"]').get_by_role("button", name=button)
            if not await go.count():
                bad.append(f"{step}: no {button!r}")
            else:
                await go.click()
                await asyncio.sleep(0.5)
                if [s.get("response") for s in await _resumes(page)] != [{"feedback": "approve"}]:
                    bad.append(f"{step}: {button} sent {await _resumes(page)}")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the plan's five steps with a person's step amber; Build it and Go ahead send 'approve' once; an unread plan keeps its words"


@check
async def check_buildersuggest(browser) -> str:
    """An empty builder offers its examples as suggestions under the composer (M2c, they were bullets): a tap puts
    the words in the composer and sends nothing; the keys' hint shows until the first message."""
    ctx, page = await _builder_back(browser, None)
    bad: list[str] = []
    try:
        await _build_with_ai(page)
        panel = page.locator('[data-testid="assistant-panel"]')
        chips = panel.get_by_role("list", name="Things to ask").get_by_role("button")
        await chips.first.wait_for(timeout=10_000)
        # The examples (four since ASSIST1 added "Summarise today") and the Agent Builder switch (SW1).
        if await chips.count() != 5:
            bad.append(f"{await chips.count()} suggestions")
        first = (await chips.first.inner_text()).strip()
        await chips.first.click()
        value = await panel.locator("textarea").input_value()
        if value != first:
            bad.append(f"the composer holds {value!r}, not {first!r}")
        if [c for c in await page.evaluate("() => window.__calls") if "/api/builder/chat" in c["url"]]:
            bad.append("a suggestion was sent")
        if "Enter to send" not in await _panel_text(page):
            bad.append("no hint before the first message")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "five suggestions; one fills the composer, unsent; the hint shows before the first message"


COMPOSER_PROBE = """() => {
  const form = document.querySelector('main form');
  if (!form) return null;
  const send = form.querySelector('button[type="submit"]');
  const stop = [...form.querySelectorAll('button')].find((b) => b.getAttribute('aria-label') === 'Stop the agent');
  const hint = [...document.querySelectorAll('main span')].find((s) => s.textContent === 'Enter to send · Shift+Enter for a new line');
  const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return {x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width)}; };
  return {
    pill: !!form.querySelector('button[aria-label^="Agents"]'),
    attach: !!form.querySelector('button[aria-label="Attach images"]'),
    textareas: form.querySelectorAll('textarea').length,
    send: send ? {name: send.getAttribute('aria-label'), at: r(send)} : null,
    stop: stop ? {at: r(stop)} : null,
    hint: hint ? getComputedStyle(hint).visibility : null,
    overflow: form.scrollWidth > form.clientWidth + 1,
  };
}"""


@check
async def check_composer(browser) -> str:
    """Chat's composer is one field (M2c): the agent's pill, Attach, the model and Send inside one box, at 1440 and
    400 px. Shift+Enter is a new line and Enter sends; the keys' hint goes after the first message; while the turn
    streams Stop stands where Send was; the answer's tools show as a chip without a reload."""
    bad: list[str] = []
    for width in (WIDE, PHONE):
        ctx, page = await _chat_page(browser, f"uismoke-composer-{width[0]}", width=width)
        try:
            before = await page.evaluate(COMPOSER_PROBE)
            if not before or not (before["pill"] and before["attach"] and before["textareas"] == 1 and before["send"]):
                bad.append(f"{width[0]}px: the field holds {before}")
                continue
            if before["send"]["name"] != "Send" or before["overflow"]:
                bad.append(f"{width[0]}px: Send is {before['send']}, overflow {before['overflow']}")
            if width == WIDE and before["hint"] != "visible":
                bad.append(f"the hint is {before['hint']} before the first message")
            box = page.locator("main textarea").last
            await box.fill("first line")
            await box.press("Shift+Enter")
            await box.type("second line")
            if "\n" not in await box.input_value() or [c for c in await page.evaluate("() => window.__calls") if "/api/chat/stream" in c["url"]]:
                bad.append(f"{width[0]}px: Shift+Enter did not make a new line, or it sent")
            await box.press("Enter")
            await _stream_open(page, "post")
            await asyncio.sleep(0.8)  # the hint fades over 500 ms
            during = await page.evaluate(COMPOSER_PROBE)
            if not during["stop"] or abs(during["stop"]["at"]["x"] - before["send"]["at"]["x"]) > 2:
                bad.append(f"{width[0]}px: Stop is {during['stop']}, Send was at {before['send']['at']}")
            if width == WIDE and during["hint"] != "hidden":
                bad.append(f"the hint is still {during['hint']} after the first message")
            frames = [
                _frame({"type": "tool_execution", "tool_name": "web_search", "args": {"q": "uismoke"}}, "c-1"),
                _frame({"type": "tool_result", "tool_name": "web_search", "preview": "two results"}, "c-2"),
                _frame({"type": "response", "content": "Found it.", "intent": "chat"}, "c-3"),
                _frame({"type": "done"}, "c-4"),
            ]
            await page.evaluate("(fs) => fs.forEach((f) => window.__streams.post.push(f))", frames)
            if not await _on_screen(page, "Tools used · 1"):
                bad.append(f"{width[0]}px: no tool chip under a live answer")
        finally:
            await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "one field at 1440 and 400; Shift+Enter a new line, Enter sends; Stop in Send's place; the hint goes; a live tool chip"


WAITING_RUN = "run_uismoke_waiting"

#: The bell's ring with one run waiting on a person, that run's record, and the runs list with it paused beside one
#: running; the answer is held in the browser.
WAITING_STUB = r"""
window.__answers = [];
const _waitFetch = window.fetch;
const _json = (body) => Promise.resolve(new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}}));
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || 'GET').toUpperCase();
  const run = '%(run)s';
  if (url.includes('/api/notifications/stream')) {
    return Promise.resolve(new Response(new ReadableStream({start() {}}), {status: 200, headers: {'Content-Type': 'text/event-stream'}}));
  }
  if (url.includes('/api/notifications')) {
    return _json({last_id: 9001, notifications: [{id: 9001, ts: Date.now() / 1000 - 600, kind: 'human_input',
      title: 'Invoice triage needs input', body: 'Approve payment?', run_id: run, data: {}, resolved: false}]});
  }
  if (url.endsWith('/api/orchestrations/runs/' + run + '/human-input') && method === 'POST') {
    window.__answers.push(JSON.parse(String(init.body)));
    return _json({status: 'resumed'});
  }
  if (url.includes('/api/orchestrations/runs/' + run)) {
    return _json({run_id: run, orchestration_id: 'orch_uismoke_wait', status: 'paused', waiting_for_human: true,
      current_step_id: 'approve', human_prompt: 'INV-1047 totals $2,310.00. Approve payment?',
      human_fields: [{name: 'approved', type: 'boolean', label: 'Approve payment?'}, {name: 'note', type: 'text', label: 'Note for the ledger'}],
      shared_state: {}, step_history: [], started_at: new Date(Date.now() - 600000).toISOString()});
  }
  if (/\/api\/orchestrations\/runs(\?|$)/.test(url)) {
    return _json([{run_id: run, orchestration_id: 'orch_uismoke_wait', status: 'paused', started_at: null},
                  {run_id: 'run_uismoke_busy', orchestration_id: 'orch_uismoke_busy', status: 'running', started_at: null}]);
  }
  return _waitFetch(input, init);
};
""" % {"run": WAITING_RUN}


@check
async def check_waitingrow(browser) -> str:
    """What waits on a person shows from any screen (M2c, Naveen's ask): the menu's amber "Waiting on you" row with
    its count, which opens Home's Needs you in a drawer, answered there with the run page's own request; beside the
    bell on a phone. Chat's strip shows only what is running."""
    bad: list[str] = []
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(WAITING_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(APP, wait_until="domcontentloaded")
        row = page.get_by_role("button", name=re.compile("^Waiting on you")).first
        await row.wait_for(timeout=15_000)
        if "1" not in await row.inner_text():
            bad.append(f"the row reads {await row.inner_text()!r}")
        strip = await page.evaluate("() => (document.querySelector('main') || document.body).innerText")
        if "needs input" in strip:
            bad.append("chat's strip still lists the waiting run")
        await row.click()
        drawer = page.get_by_role("dialog", name="Waiting on you")
        await drawer.wait_for(timeout=10_000)
        approve = drawer.get_by_role("button", name="Approve payment")
        await approve.wait_for(timeout=10_000)
        await approve.click()
        await asyncio.sleep(0.8)
        answers = await page.evaluate("() => window.__answers")
        if len(answers) != 1 or answers[0].get("response", {}).get("approved") is not True:
            bad.append(f"the drawer answered {answers}")
    finally:
        await ctx.close()
    ctx = await browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]})
    await ctx.add_init_script(WAITING_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(APP, wait_until="domcontentloaded")
        chip = page.get_by_role("button", name="Waiting on you · 1")
        await chip.first.wait_for(timeout=15_000)
        if not await chip.first.is_visible():
            bad.append("no count beside the phone's bell")
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return "the row counts one; its drawer answers with the run page's request; a phone shows it beside the bell"



# ── the agents workbench (M2d, D211) ─────────────────────────────────────────

#: A test turn's stream, answered in the browser: a tool call, its result, the answer.
AGENT_TEST_SSE = "".join(f"id: t-{i}\ndata: {json.dumps(e)}\n\n" for i, e in enumerate([
    {"type": "thinking", "message": "Analyzing your request…"},
    {"type": "tool_execution", "tool_name": "search_knowledge", "args": {"query": "uismoke"}},
    {"type": "tool_result", "tool_name": "search_knowledge", "preview": "2 passages"},
    {"type": "response", "content": "The uismoke answer."},
    {"type": "done"},
]))

#: The page's height against the window's: a workbench that scrolls only inside its columns.
PAGE_FITS = "() => document.scrollingElement.scrollHeight <= innerHeight + 1"


async def _workbench(browser, aid: str, width=WIDE, test_open: str | None = "1"):
    """A page on the Agents workbench with `aid` open (through `?id=`), and the requests it makes."""
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    if test_open is not None:
        # Only when nothing is kept yet: a reload must read what the page itself stored.
        await ctx.add_init_script(
            f"try {{ if (localStorage.getItem('synapseAgentTestOpen') === null) "
            f"localStorage.setItem('synapseAgentTestOpen', '{test_open}'); }} catch (e) {{}}")
    page = await ctx.new_page()
    seen: list[dict] = []
    page.on("request", lambda r: seen.append({"url": r.url, "method": r.method, "body": r.post_data}))
    await page.goto(f"{APP}/agents?id={aid}", wait_until="domcontentloaded")
    await page.get_by_role("tab", name="Setup").wait_for(timeout=30_000)
    await asyncio.sleep(SETTLE)
    return ctx, page, seen


@check
async def check_agentsopen(browser) -> str:
    """The Agents page opens on an agent, never on "Nothing selected" (M2d, UX1's finding), and fits the window:
    the list, the setup and the test chat scroll inside themselves, the page does not (Naveen saw it scroll past)."""
    stamp = int(time.time())
    ids = [f"agent_uismoke_open_{stamp}_{n}" for n in "ab"]
    for n, aid in zip("AB", ids):
        api("/api/agents", "POST", _agent(aid, f"Open {n} {stamp}"))
    bad: list[str] = []
    try:
        for width in (WIDE, PHONE):
            ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
            page = await ctx.new_page()
            try:
                await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
                await page.get_by_role("tab", name="Setup").wait_for(timeout=30_000)
                await asyncio.sleep(SETTLE)
                if not await page.locator('aside[aria-label="Your agents"] button[aria-current="true"]').count():
                    bad.append(f"{width[0]}px: no agent is open")
                if not await page.locator("#age-draftagent-name").input_value():
                    bad.append(f"{width[0]}px: the open agent has no name in its field")
                if await page.get_by_text(re.compile("Select an agent|Nothing selected")).count():
                    bad.append(f"{width[0]}px: the pane says nothing is selected")
                if await page.get_by_role("tab", name="Setup").get_attribute("aria-selected") != "true":
                    bad.append(f"{width[0]}px: Setup is not the open tab")
                if not await page.evaluate(PAGE_FITS):
                    bad.append(f"{width[0]}px: the page is taller than the window")
            finally:
                await ctx.close()
    finally:
        for aid in ids:
            try:
                api(f"/api/agents/{aid}", "DELETE")
            except Exception:
                pass
    if bad:
        raise AssertionError("; ".join(bad))
    return "opens on its first agent at 1440 and 400, Setup open, and the page fits the window"


@check
async def check_agentsavebar(browser) -> str:
    """The Save bar shows only while there is something to save; Discard asks and puts the agent back; Save posts
    once and the bar goes (M2d; the guard is F73's)."""
    aid = f"agent_uismoke_savebar_{int(time.time())}"
    api("/api/agents", "POST", _agent(aid, f"Save bar {aid[-6:]}"))
    ctx, page, seen = await _workbench(browser, aid)
    bad: list[str] = []
    try:
        save = page.get_by_role("button", name="Save agent", exact=True)
        if await save.count():
            bad.append("the Save bar shows with nothing to save")
        desc = page.get_by_label("Description", exact=True)
        before = await desc.input_value()
        await desc.fill("edited by ui-smoke")
        await asyncio.sleep(0.4)
        if not await save.count() or not await page.get_by_text("Unsaved changes.").count():
            bad.append("an edit shows no Save bar")
        await page.get_by_role("button", name="Discard", exact=True).click()
        dialog = page.get_by_role("dialog")
        await dialog.wait_for(timeout=5_000)
        await dialog.get_by_role("button", name=re.compile("discard", re.I)).first.click()
        await asyncio.sleep(0.6)
        if await desc.input_value() != before:
            bad.append("Discard did not put the description back")
        if await save.count():
            bad.append("the Save bar stays after Discard")
        await desc.fill("saved by ui-smoke")
        await asyncio.sleep(0.3)
        posts_before = len([r for r in seen if r["method"] == "POST" and r["url"].endswith("/api/agents")])
        await save.click()
        await asyncio.sleep(1.5)
        posts = len([r for r in seen if r["method"] == "POST" and r["url"].endswith("/api/agents")]) - posts_before
        if posts != 1:
            bad.append(f"Save posted the agent {posts} times")
        if await save.count():
            bad.append("the Save bar stays after Save")
        stored = next((a for a in api("/api/agents") if a["id"] == aid), {})
        if stored.get("description") != "saved by ui-smoke":
            bad.append(f"the stored description is {stored.get('description')!r}")
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    if bad:
        raise AssertionError("; ".join(bad))
    return "no bar until an edit; Discard asks and restores; Save posts once and the bar goes"


@check
async def check_agenttest(browser) -> str:
    """The workbench's test chat talks to the saved agent (D211): a turn posts the agent's id on a conversation of
    its own, never switches chat's agent, and shows its tool as a chip; with unsaved edits it says so, and Save and
    test saves first; Clear starts a new conversation. The stream is answered in the browser."""
    aid = f"agent_uismoke_test_{int(time.time())}"
    name = f"Test {aid[-6:]}"
    api("/api/agents", "POST", _agent(aid, name))
    ctx, page, seen = await _workbench(browser, aid)
    await page.route("**/api/chat/stream", lambda r: r.fulfill(
        status=200, headers={"content-type": "text/event-stream"}, body=AGENT_TEST_SSE))
    bad: list[str] = []
    try:
        panel = page.locator(f'section[aria-label="Try {name}"]')
        box = panel.get_by_role("textbox", name=f"Message {name}")
        await box.wait_for(timeout=10_000)

        async def ask(text: str) -> dict:
            await box.fill(text)
            await box.press("Enter")
            await panel.get_by_text("The uismoke answer.").last.wait_for(timeout=10_000)
            posts = [r for r in seen if r["url"].endswith("/api/chat/stream")]
            return json.loads(posts[-1]["body"] or "{}")

        first = await ask("first question")
        if first.get("agent_id") != aid or not first.get("session_id"):
            bad.append(f"the turn posted {first}")
        if not await panel.get_by_role("button", name=re.compile("search_knowledge")).count():
            bad.append("no tool chip for the turn's call")
        if [r for r in seen if "/api/agents/active" in r["url"]]:
            bad.append("testing switched chat's agent (/api/agents/active)")

        await page.get_by_label("Description", exact=True).fill("unsaved by ui-smoke")
        notice = panel.get_by_text("Testing the saved version.")
        if not await notice.count():
            bad.append("unsaved edits do not say the test uses the saved version")
        else:
            n = len([r for r in seen if r["method"] == "POST" and r["url"].endswith("/api/agents")])
            await panel.get_by_role("button", name="Save and test").click()
            await asyncio.sleep(1.5)
            if len([r for r in seen if r["method"] == "POST" and r["url"].endswith("/api/agents")]) != n + 1:
                bad.append("Save and test did not save the agent once")
            if await notice.count():
                bad.append("the notice stays after Save and test")

        await panel.get_by_role("button", name="Clear the test").click()
        await asyncio.sleep(0.4)
        if await panel.get_by_text("first question").count():
            bad.append("Clear left the conversation")
        second = await ask("second question")
        if second.get("session_id") == first.get("session_id"):
            bad.append("Clear did not start a new conversation")
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    if bad:
        raise AssertionError("; ".join(bad))
    return "the saved agent's id on its own conversation, a tool chip, no agent switch; Save and test; Clear is new"


@check
async def check_agenttestcollapse(browser) -> str:
    """The test chat collapses and the setup takes its room, remembered across a reload (D211); on a phone it opens
    in a drawer from Try it."""
    aid = f"agent_uismoke_collapse_{int(time.time())}"
    name = f"Collapse {aid[-6:]}"
    api("/api/agents", "POST", _agent(aid, name))
    bad: list[str] = []
    try:
        ctx, page, _ = await _workbench(browser, aid)
        try:
            setup = page.locator("#age-draftagent-name")
            wide_before = (await setup.bounding_box())["width"]
            await page.get_by_role("button", name="Hide the test").click()
            await asyncio.sleep(0.6)
            if await page.locator(f'section[aria-label="Try {name}"]').count():
                bad.append("the panel stays after Hide the test")
            strip = page.get_by_role("button", name=f"Try {name}")
            if not await strip.count():
                bad.append("no strip to bring the panel back")
            if (await setup.bounding_box())["width"] <= wide_before:
                bad.append("the setup did not take the panel's room")
            # Again by `?id=`: the page drops it from the address once read, so a bare reload opens the first agent.
            await page.goto(f"{APP}/agents?id={aid}", wait_until="domcontentloaded")
            await page.get_by_role("tab", name="Setup").wait_for(timeout=30_000)
            await asyncio.sleep(SETTLE)
            if await page.locator(f'section[aria-label="Try {name}"]').count():
                bad.append("a reload forgot that the panel was hidden")
            await page.get_by_role("button", name=f"Try {name}").click()
            await asyncio.sleep(0.6)
            if not await page.locator(f'section[aria-label="Try {name}"]').count():
                bad.append("the strip does not bring the panel back")
        finally:
            await ctx.close()
        ctx, page, _ = await _workbench(browser, aid, width=PHONE, test_open=None)
        try:
            await page.get_by_role("button", name="Try it").click()
            dialog = page.get_by_role("dialog", name="Test chat")
            await dialog.wait_for(timeout=5_000)
            if not await dialog.get_by_role("textbox", name=f"Message {name}").count():
                bad.append("the phone's drawer has no message box")
        finally:
            await ctx.close()
    finally:
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    if bad:
        raise AssertionError("; ".join(bad))
    return "Hide the test widens the setup and is remembered; the strip brings it back; a phone's Try it opens the drawer"


def _person_orchestration(oid: str, name: str) -> dict:
    """A Print step, then a Human step, then End: it asks a person before it finishes (never run here)."""
    return {
        "id": oid, "name": name, "description": "Temporary fixture created by scripts/ui-smoke.py (M2k).",
        "trigger": "manual", "state_schema": {}, "entry_step_id": f"{oid}_a",
        "steps": [
            {"id": f"{oid}_a", "name": "Note", "type": "print", "print_content": "one",
             "next_step_id": f"{oid}_ask", "output_key": "one"},
            {"id": f"{oid}_ask", "name": "Approve", "type": "human", "human_prompt": "OK?",
             "next_step_id": f"{oid}_end", "output_key": "ok"},
            {"id": f"{oid}_end", "name": "End", "type": "end", "next_step_id": None},
        ],
    }


@check
async def check_workflowpillstays(browser) -> str:
    """A pill the person picked on Workflows stays picked (F510). The page jumps to Active once when a run waits,
    and that read can land after the click: on a seeded workspace `workflowlist` and `workflowmenu` clicked
    Workflows, then lost the list (and its search box) to Active. The runs read is held 2 s here."""
    paused = [{"run_id": "run_smoke_pill", "orchestration_id": "orch_smoke_pill", "orchestration_name": "Smoke pill",
               "status": "paused", "waiting_for_human": True, "started_at": "2026-10-14T09:00:00Z"}]
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/orchestrations/runs", _answer(paused, hold=2.0))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        pill = page.get_by_role("group", name="Show").get_by_role("button", name=re.compile("^Workflows"))
        await pill.click(timeout=30_000)
        await page.wait_for_timeout(3_500)  # the held read lands
        if await pill.get_attribute("aria-pressed") != "true":
            raise AssertionError("the page moved off the pill the person picked")
        # With no pick, a waiting run still lands it on Active, as before.
        await page.reload(wait_until="domcontentloaded")
        active = page.get_by_role("group", name="Show").get_by_role("button", name=re.compile("^Active"))
        await page.wait_for_function(
            "() => [...document.querySelectorAll('[role=group][aria-label=Show] button')]"
            ".some(b => /^Active/.test(b.textContent.trim()) && b.getAttribute('aria-pressed') === 'true')",
            timeout=10_000)
        if await active.get_attribute("aria-pressed") != "true":
            raise AssertionError("a waiting run no longer lands the page on Active")
    finally:
        await ctx.close()
    return "a picked pill stays; with no pick a waiting run still lands on Active"


@check
async def check_workflowlist(browser) -> str:
    """The Workflows list (M2k): a row per workflow says what starts it, how its last run went (time, duration, cost
    through `money`) and its steps by role, a person's step named in the path; a schedule's chip is its rhythm in
    words; the search finds a row; the page fits at 1600 and 400."""
    stamp = int(time.time())
    ran, asks = f"orch_uismoke_wfl_{stamp}", f"orch_uismoke_wfa_{stamp}"
    ran_name, asks_name = f"ui-smoke list ran {stamp}", f"ui-smoke list asks {stamp}"
    api("/api/orchestrations", "POST", print_orchestration(ran, ran_name))
    api("/api/orchestrations", "POST", _person_orchestration(asks, asks_name))
    run_to_end(ran)
    sched = [{"id": "sch_uismoke_wfl", "name": "Smoke Monday", "enabled": True, "target_type": "orchestration",
              "target_id": ran, "schedule_type": "cron", "cron_expression": "0 7 * * 1"}]
    seen = []
    try:
        for width, height in (WIDE, PHONE):
            ctx = await browser.new_context(viewport={"width": width, "height": height})
            await ctx.route("**/api/schedules", _answer(sched))
            page = await ctx.new_page()
            try:
                await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
                await page.get_by_role("button", name=re.compile(r"^Workflows")).click(timeout=30_000)
                await page.get_by_role("searchbox", name=re.compile("Search workflows")).fill(f"{stamp}")
                row = page.locator(f"[data-workflow-row='{ran}']")
                await row.wait_for(timeout=30_000)
                # The last runs are read after the rows; a cell says "—" until then (workflowlistunknown).
                await page.locator("[data-last-run-unknown]").first.wait_for(state="detached", timeout=30_000)
                text = await row.inner_text()
                for want in ("Mondays 07:00 UTC", "2 steps", "$0"):
                    if want not in text:
                        raise AssertionError(f"at {width}px the run row lacks {want!r}: {text!r}")
                if await row.locator("[data-step-path]").count() != 1:
                    raise AssertionError("the row has no path of its steps")
                if "for a person" in (await row.locator("[data-step-path]").get_attribute("aria-label") or ""):
                    raise AssertionError("a workflow with no person has a person's step in its path")
                other = page.locator(f"[data-workflow-row='{asks}']")
                otext = await other.inner_text()
                for want in ("Manual", "Never run"):
                    if want not in otext:
                        raise AssertionError(f"at {width}px the person row lacks {want!r}: {otext!r}")
                # A person's step is the amber tile; its words are the path's name (Naveen dropped the sentence).
                label = await other.locator("[data-step-path]").get_attribute("aria-label") or ""
                if "1 for a person" not in label:
                    raise AssertionError(f"the person row's path does not say so: {label!r}")
                if await page.locator("[data-workflow-row]").count() != 2:
                    raise AssertionError("the search kept rows that do not match")
                wide = await page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                if wide > 1:
                    raise AssertionError(f"at {width}px the page scrolls {wide}px sideways")
                seen.append(str(width))
            finally:
                await ctx.close()
    finally:
        drop_fixture(ran)
        drop_fixture(asks)
    return f"chip, last run, path and the person line at {' and '.join(seen)}px; the search narrows the list"


@check
async def check_workflowlistunknown(browser) -> str:
    """A row's last run claims nothing until it is known (TABS1, F930): the rows come with the workflows, and their
    runs from a second read. A workflow that ran said "Never run" until that read answered — a wrong word on the
    way (stack 1's baseline, workflowlist). While the read is out, and when it fails, the cell is "—"; "Never run"
    only once it has answered and there is no run."""
    stamp = int(time.time())
    oid = f"orch_uismoke_wfu_{stamp}"
    api("/api/orchestrations", "POST", print_orchestration(oid, f"ui-smoke unknown {stamp}"))
    said = {}
    try:
        for case, answer in (("held", _answer([], hold=4.0)), ("failed", _answer({"detail": "boom"}, status=500))):
            ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
            await ctx.route("**/api/orchestrations/runs?limit=200", answer)
            page = await ctx.new_page()
            try:
                await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
                row = page.locator(f"[data-workflow-row='{oid}']")
                await row.wait_for(timeout=30_000)
                said[case] = await row.inner_text()
                if case == "held":
                    await page.locator("[data-last-run-unknown]").first.wait_for(state="detached", timeout=15_000)
                    said["answered"] = await row.inner_text()
                else:
                    await asyncio.sleep(SETTLE)
                    said["after failing"] = await row.inner_text()
            finally:
                await ctx.close()
    finally:
        drop_fixture(oid)
    for case in ("held", "failed", "after failing"):
        if "Never run" in said[case]:
            raise AssertionError(f"while its runs are {case}, the row says 'Never run': {said[case]!r}")
    if "Never run" not in said["answered"]:
        raise AssertionError(f"once the read answered with no run, the row does not say 'Never run': {said['answered']!r}")
    return "a row's last run is '—' while its read is out or failed, and 'Never run' only once it answered"


@check
async def check_workflowmenu(browser) -> str:
    """A workflow's row menu (M2k): Duplicate adds the copy to the list without leaving it; Copy ID says so; Delete
    asks first ("Delete workflow") and the row goes; Enter on the row opens it on the canvas."""
    stamp = int(time.time())
    oid, name = f"orch_uismoke_wfm_{stamp}", f"ui-smoke menu {stamp}"
    api("/api/orchestrations", "POST", print_orchestration(oid, name))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, permissions=["clipboard-read", "clipboard-write"])
    page = await ctx.new_page()
    copies: list[str] = []
    try:
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile(r"^Workflows")).click(timeout=30_000)
        await page.get_by_role("searchbox", name=re.compile("Search workflows")).fill(name)
        await page.get_by_role("button", name=f"Actions for {name}").click(timeout=30_000)
        await page.get_by_role("menuitem", name="Duplicate").click()
        copy = page.get_by_role("button", name=f"Open {name} (Copy)")
        try:
            await copy.wait_for(timeout=20_000)
        except Exception:
            raise AssertionError("Duplicate did not add the copy to the list")
        if "id=" in page.url:
            raise AssertionError("Duplicate from the list opened the copy")
        copies = [o["id"] for o in api("/api/orchestrations") if o.get("name") == f"{name} (Copy)"]

        await page.get_by_role("button", name=f"Actions for {name} (Copy)").click()
        await page.get_by_role("menuitem", name="Copy ID").click()
        await page.get_by_text("ID copied.").wait_for(timeout=10_000)

        await page.get_by_role("button", name=f"Actions for {name} (Copy)").click()
        await page.get_by_role("menuitem", name="Delete").click()
        await page.get_by_text("Delete workflow").wait_for(timeout=10_000)
        await page.get_by_role("button", name=re.compile("delete", re.I)).last.click()
        await copy.wait_for(state="detached", timeout=20_000)

        opener = page.get_by_role("button", name=f"Open {name}", exact=True)
        await opener.focus()
        await page.keyboard.press("Enter")
        await page.locator(".react-flow").wait_for(timeout=30_000)
    finally:
        await ctx.close()
        drop_fixture(oid)
        for c in copies:
            drop_fixture(c)
    return "Duplicate stays on the list, Copy ID confirms, Delete asks then removes, Enter opens the canvas"


@check
async def check_workflowsettle(browser) -> str:
    """The list never says "No workflows yet" while its first read is still out (Naveen, M2k review)."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/orchestrations", _answer([print_orchestration("orch_uismoke_settle", "ui-smoke settle")],
                                                     hold=2.0))
    await ctx.route("**/api/orchestrations/runs*", _answer([]))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await page.locator("main h1").wait_for(timeout=30_000)
        await asyncio.sleep(1.0)
        if await page.get_by_text("No workflows yet").count():
            raise AssertionError("the empty state shows while the list is still being read")
        await page.locator("[data-workflow-row='orch_uismoke_settle']").wait_for(timeout=10_000)
    finally:
        await ctx.close()
    return "nothing, then the rows; never the empty state on the way"


@check
async def check_workflowempty(browser) -> str:
    """With no workflows the list says so and offers a template (M2k), and the header keeps New workflow, Build with
    AI and Templates."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/orchestrations", _answer([]))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await page.get_by_text("No workflows yet").wait_for(timeout=30_000)
        await page.get_by_role("link", name="Start from a template").wait_for()
        for name in ("New workflow", "Build with AI"):
            if not await page.locator("main header").get_by_role("button", name=name).count():
                raise AssertionError(f"the header lacks {name}")
        if not await page.locator("main header").get_by_role("link", name="Templates").count():
            raise AssertionError("the header lacks Templates")
    finally:
        await ctx.close()
    return "No workflows yet with a template link; the header keeps its three actions"


# ── the widget (M2i) ──────────────────────────────────────────────────────────

WIDGET_SHOP = "https://shop.uismoke.example"  # a site of its own; only localhost may be http


def _smoke_widget(origins: list[str]) -> dict:
    """A widget for the checks below, on the agent an existing widget already serves (a publishable one), or on the
    first agent. The caller deletes it."""
    widgets = api("/api/widgets")
    widgets = widgets.get("widgets", widgets) if isinstance(widgets, dict) else widgets
    agent = next((w["agent_id"] for w in widgets if w.get("agent_id")), None)
    if not agent:
        agents = api("/api/agents")
        agents = agents.get("agents", agents) if isinstance(agents, dict) else agents
        agent = agents[0]["id"]
    made = api("/api/widgets", "POST", {"name": "uismoke widget", "agent_id": agent, "allowed_origins": origins,
                                        "title": "Smoke help", "greeting": "Hi from the smoke test."})
    return made.get("widget", made)


async def _widget_page(browser, key: str, referer: str, scheme: str = "light"):
    """The widget's own page, opened as a frame on `referer` would open it: the session is minted for that origin."""
    ctx = await browser.new_context(viewport={"width": 400, "height": 620}, color_scheme=scheme)
    page = await ctx.new_page()
    await page.goto(f"{APP}/embed/{key}", referer=referer + "/", wait_until="domcontentloaded")
    return ctx, page


@check
async def check_widgetcomposer(browser) -> str:
    """The widget's composer has the product's shape (M2i): one field holding the text and one round button, Send or
    Stop, both named. Shift+Enter is a new line, an Enter that ends an IME composition never sends, Enter sends, and
    Stop asks the API to cancel. The chat's stream is stubbed; the session is the real one."""
    widget = _smoke_widget([WIDGET_SHOP])
    bad: list[str] = []
    try:
        ctx, page = await _widget_page(browser, widget["public_key"], WIDGET_SHOP)
        try:
            calls: list[str] = []
            hold = asyncio.Event()

            async def chat(route):
                calls.append("chat")
                if len(calls) > 1:
                    await hold.wait()  # the second turn stays open, so Stop stands
                body = 'data: {"type": "response", "content": "Smoke answer."}\n\ndata: {"type": "done"}\n\n'
                try:
                    await route.fulfill(status=200, content_type="text/event-stream", body=body)
                except Exception:  # Stop aborted the held turn's request first
                    pass

            async def cancel(route):
                calls.append("cancel")
                hold.set()
                await route.fulfill(status=202, content_type="application/json", body="{}")

            await page.route("**/api/widget/chat", chat)
            await page.route("**/api/widget/cancel", cancel)
            box = page.get_by_role("textbox", name="Message")
            await box.wait_for(timeout=15000)
            shape = await box.evaluate("""(e) => {
                const field = e.parentElement, send = field.querySelector('button');
                const r = send.getBoundingClientRect(), f = field.getBoundingClientRect();
                return {tag: e.tagName, name: send.getAttribute('aria-label'), w: r.width, h: r.height,
                        round: getComputedStyle(send).borderRadius, inside: r.right <= f.right && r.top >= f.top};
            }""")
            if shape["tag"] != "TEXTAREA" or shape["name"] != "Send" or not shape["inside"] \
                    or abs(shape["w"] - shape["h"]) > 1 or shape["round"] != "50%":
                bad.append(f"the composer is {shape}")
            user = page.locator("p[class*='fromUser']")
            await box.fill("first line")
            await box.press("Shift+Enter")
            await box.type("second line")
            if "\n" not in await box.input_value() or await user.count():
                bad.append("Shift+Enter did not make a new line, or it sent")
            await box.evaluate("e => e.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229, "
                               "isComposing: true, bubbles: true, cancelable: true}))")
            await asyncio.sleep(0.3)
            if await user.count():
                bad.append("an Enter that ends an IME composition sent the message")
            await box.press("Enter")
            await page.get_by_text("Smoke answer.").wait_for(timeout=10000)
            if await user.count() != 1 or "second line" not in await user.first.inner_text():
                bad.append(f"Enter sent {await user.count()}")
            await box.fill("hold on")
            await page.get_by_role("button", name="Send").click()
            stop = page.get_by_role("button", name="Stop")
            await stop.wait_for(timeout=5000)
            await stop.click()
            for _ in range(30):
                if "cancel" in calls:
                    break
                await asyncio.sleep(0.1)
            if "cancel" not in calls:
                bad.append("Stop never asked the API to cancel")
            await page.get_by_role("button", name="Send").wait_for(timeout=10000)
        finally:
            await ctx.close()
    finally:
        api(f"/api/widgets/{widget['id']}", "DELETE")
    if bad:
        raise AssertionError("; ".join(bad))
    return "one field with a round Send and Stop; Shift+Enter, an IME Enter, Enter and Stop as the product's composer"


@check
async def check_widgetrefused(browser) -> str:
    """A site not on the widget's list gets the refusal, in words, with no composer (M2i kept the rule as it was)."""
    widget = _smoke_widget([WIDGET_SHOP])
    try:
        ctx, page = await _widget_page(browser, widget["public_key"], "https://elsewhere.uismoke.example")
        try:
            alert = page.locator("main[role=alert]")  # Next adds a route announcer, also an alert
            await alert.wait_for(timeout=15000)
            text = await alert.inner_text()
            if "can’t load here" not in text or "elsewhere.uismoke.example" not in text:
                raise AssertionError(f"the refusal reads {text!r}")
            if await page.get_by_role("textbox", name="Message").count():
                raise AssertionError("a refused page still offers the composer")
        finally:
            await ctx.close()
    finally:
        api(f"/api/widgets/{widget['id']}", "DELETE")
    return "an unlisted origin is refused, in words"


# ── M2h: Logs, Evals and Schedules ───────────────────────────────────────────

_M2H_LOGS = [
    {"run_id": "run_m2h_b", "orchestration_name": "Smoke failing", "orchestration_id": "orch_m2h", "session_id": "",
     "started_at": "2026-09-30 10:05:00", "user_input": "second", "file_size_kb": 3.1},
    {"run_id": "run_m2h_a", "orchestration_name": "Smoke passing", "orchestration_id": "orch_m2h", "session_id": "",
     "started_at": "2026-09-30 10:00:00", "user_input": "first", "file_size_kb": 2.0},
]
_M2H_RUNS = [
    {"run_id": "run_m2h_b", "orchestration_id": "orch_m2h", "status": "failed", "started_at": "2026-09-30T10:05:00Z",
     "ended_at": "2026-09-30T10:05:04Z", "total_cost_usd": 0.0012},
    {"run_id": "run_m2h_a", "orchestration_id": "orch_m2h", "status": "completed", "started_at": "2026-09-30T10:00:00Z",
     "ended_at": "2026-09-30T10:00:02Z", "total_cost_usd": 0},
]


@check
async def check_logfirst(browser) -> str:
    """Logs never opens on an empty pane (M2h): the newest log is open beside the list, and Copy is there."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/logs/agents?*", _answer([{"run_id": "run_m2h_agent", "agent_name": "Smoke agent",
                                                      "source": "chat", "session_id": "",
                                                      "started_at": "2026-09-30 10:00:00", "file_size_kb": 1}]))
    await ctx.route("**/api/logs/agents/run_m2h_agent", _answer("🔄 LLM CALL — TURN 1\nhello\n", content_type="text/plain"))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/logs", wait_until="domcontentloaded")
        first = page.locator('[data-log-row="run_m2h_agent"] [data-row-open]')
        await first.wait_for(timeout=30_000)
        if await first.get_attribute("aria-current") != "true":
            raise AssertionError("the newest log is not open")
        await page.get_by_role("button", name="Copy").wait_for(timeout=10_000)
        if await page.get_by_text("Select a log to view its contents").count():
            raise AssertionError("the pane still asks for a pick")
    finally:
        await ctx.close()
    return "the newest log is open beside the list, with Copy"


@check
async def check_goldendelete(browser) -> str:
    """A golden set can be deleted from Evals (F508): before, only through the API. Never while it runs: the
    button waits for Stop and the route refuses (409). Its past runs stay."""
    stamp = int(time.time())
    suite = api("/api/evals/suites", "POST", {"name": f"Smoke delete set {stamp}", "orchestration_id": "orch_none",
                                              "cases": [{"name": "one", "input": "hi", "notes": ""}]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    deleted: list[str] = []
    page.on("request", lambda r: deleted.append(r.url) if r.method == "DELETE" and "/api/evals/suites/" in r.url else None)
    try:
        # A set that runs: Delete waits for Stop.
        listed = api("/api/evals/suites")

        async def running(route):
            if route.request.method != "GET":
                return await route.fallback()
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps([{**x, "running": x["id"] == suite["id"]} for x in listed]))
        await page.route("**/api/evals/suites", running)
        await page.goto(f"{APP}/evals", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile(f"^Smoke delete set {stamp}")).click(timeout=30_000)
        detail = page.locator('section[aria-label="Detail"]')
        remove = detail.get_by_role("button", name="Delete set")
        await remove.wait_for(timeout=10_000)
        if not await remove.is_disabled():
            raise AssertionError("Delete is offered while the set runs")
        await page.unroute("**/api/evals/suites", running)

        # At rest: confirmed, deleted, gone from the list.
        await page.reload(wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile(f"^Smoke delete set {stamp}")).click(timeout=30_000)
        await detail.get_by_role("button", name="Delete set").click()
        await page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        await _status_saying(page, "Set deleted. Its past runs are kept.")
        await page.get_by_role("button", name=re.compile(f"^Smoke delete set {stamp}")).wait_for(
            state="detached", timeout=10_000)
        if [x for x in api("/api/evals/suites") if x["id"] == suite["id"]]:
            raise AssertionError("the set is still stored")
        if len(deleted) != 1:
            raise AssertionError(f"{len(deleted)} delete requests (expected 1)")
    finally:
        await ctx.close()
        try:
            api(f"/api/evals/suites/{suite['id']}", "DELETE")
        except Exception:
            pass
    return "Delete waits for a running set, then deletes it with a confirm; the list lets it go"


@check
async def check_evalfields(browser) -> str:
    """A criterion's weight, the scale's min, the baseline runs and a case's notes are edited on Evals (F509):
    before, the store kept them and the screen sent them back untouched, with no field for any. A scale whose
    min is not below its max is refused in words, not quietly put back to 0-10 by the store."""
    stamp = int(time.time())
    rubric = api("/api/rubrics", "POST", {"name": f"Smoke fields {stamp}", "judge_model": "default",
                                          "criteria": [{"name": "Clear", "guidance": "Plain words"}]})
    suite = api("/api/evals/suites", "POST", {"name": f"Smoke fields set {stamp}", "orchestration_id": "orch_none",
                                              "cases": [{"name": "one", "input": "hi", "notes": ""}]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    puts: list[str] = []
    page.on("request", lambda r: puts.append(r.url) if r.method == "PUT" and "/api/rubrics/" in r.url else None)
    try:
        await page.goto(f"{APP}/evals", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile(f"^Smoke fields {stamp}")).click(timeout=30_000)
        detail = page.locator('section[aria-label="Detail"]')
        await detail.get_by_role("spinbutton", name="Criterion 1 weight").fill("2.5")
        await detail.get_by_role("spinbutton", name="Scale min").fill("10")
        await detail.get_by_role("button", name="Save rubric").click()
        await _status_saying(page, "Scale min must be below Scale max.")
        if puts:
            raise AssertionError("a scale with min at max was sent")
        await detail.get_by_role("spinbutton", name="Scale min").fill("1")
        await detail.get_by_role("spinbutton", name="Runs to compare with").fill("7")
        await detail.get_by_role("button", name="Save rubric").click()
        await _status_saying(page, "Saved as version 2")
        stored = next(r for r in api("/api/rubrics") if r["id"] == rubric["id"])
        if (stored["criteria"][0]["weight"], stored["scale"]["min"], stored["baseline_runs"]) != (2.5, 1.0, 7):
            raise AssertionError(f"stored {stored['criteria'][0]['weight']}, {stored['scale']}, {stored['baseline_runs']}")

        await page.get_by_role("button", name=re.compile(f"^Smoke fields set {stamp}")).click()
        await detail.get_by_role("textbox", name="Case 1 notes").fill("A greeting with nothing else")
        await detail.get_by_role("button", name="Save set").click()
        await _status_saying(page, "Set saved.")
        notes = next(x for x in api("/api/evals/suites") if x["id"] == suite["id"])["cases"][0]["notes"]
        if notes != "A greeting with nothing else":
            raise AssertionError(f"the case's notes read {notes!r}")
    finally:
        await ctx.close()
        for path in (f"/api/evals/suites/{suite['id']}", f"/api/rubrics/{rubric['id']}"):
            try:
                api(path, "DELETE")
            except Exception:
                pass
    return "weight 2.5, scale 1-10, baseline 7 and a case's notes saved and read back; min at max refused in words"


#: The words D209 retired from what people see (F511): Orchestrations → Workflows, MCP servers → Apps.
_OLD_WORDS = re.compile(r"\borchestrations?\b|\bMCP servers?\b", re.I)
#: Kept on purpose (F511's K list, Naveen's OK): the protocol where a person connects or debugs one.
_KEPT_WORDS = re.compile(r"(filesystem|Google Workspace|outside|SynapseOrch) MCP server|MCP servers registry|"
                         r"Remove MCP server", re.I)
#: Every main page but Apps and Tools (servers added by endpoint, K1) and API keys (the API's own nouns, K2).
#: Templates is read too: its packs' own names and descriptions were renamed (F515).
_WORD_PAGES = ["/", "/agents", "/channels", "/evals", "/knowledge", "/logs", "/runs", "/schedules", "/skills",
               "/templates", "/usage", "/vault", "/widgets", "/workflows", "/settings/general", "/settings/memory",
               "/settings/scale", "/settings/support", "/settings/variables", "/settings/import_export"]


#: Where the workspace's own words are served: what people named and described, which D209 does not rename (F516).
_WORKSPACE_WORDS_FROM = ["/api/orchestrations", "/api/orchestrations/runs?limit=200", "/api/logs/orchestrations",
                         "/api/logs/agents", "/api/logs/schedules", "/api/agents", "/api/schedules"]
_WORKSPACE_WORD_KEYS = {"name", "description", "orchestration_name", "agent_name", "user_input"}


def _strings_under(value, keys: set[str], out: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str):
                out.add(item.strip())
            else:
                _strings_under(item, keys, out)
    elif isinstance(value, list):
        for item in value:
            _strings_under(item, keys, out)


#: Markdown's marks, which a page renders away: a skill's line is read as the page shows it.
_MARKDOWN_MARKS = re.compile(r"^\s*(?:#+|>|[-*+]|\d+[.)])\s+|\*\*|__|`")


def _workspace_words() -> list[str]:
    """Every name and description the workspace gave its own things, longest first.

    And every line of its skills, as written (a skill installed on this machine is the workspace's text: one
    describes "Staggered Orchestration" in its own words), with Markdown's marks taken off as the page renders it.
    """
    words: set[str] = set()
    for path in _WORKSPACE_WORDS_FROM:
        try:
            _strings_under(api(path), _WORKSPACE_WORD_KEYS, words)
        except (urllib.error.URLError, ValueError):
            continue
    try:
        skills = api("/api/skills")
    except (urllib.error.URLError, ValueError):
        skills = []
    for skill in skills if isinstance(skills, list) else []:
        try:
            body = api(f"/api/skills/{urllib.parse.quote(str(skill.get('name', '')))}").get("body") or ""
        except (urllib.error.URLError, ValueError):
            continue
        words.add(str(skill.get("description") or "").strip())
        for line in body.splitlines():
            words.add(" ".join(_MARKDOWN_MARKS.sub("", line).split()))
    return sorted((w for w in words if len(w) >= 3), key=len, reverse=True)


@check
async def check_oldwords(browser) -> str:
    """No page says the words D209 retired (F511): "Orchestration(s)" became "Workflow(s)" and "MCP servers" "Apps"
    in what people see, and the old words lingered on the canvas top bar, the chat's starter and run lines, Help &
    docs, Import / Export, Usage, Memory, Scale and the orchestrator agent's note. Reads the text and every
    aria-label, placeholder and title, on each main page and on a workflow's canvas.

    "MCP server" stays as the name of Synapse's own server, the Settings page Claude, Codex or Cursor connect through
    (Naveen 2026-10-24, GUIDE1 F1027): only a link to that page is let through, so the word anywhere else still fails.

    What the workspace wrote is not the product's words (F516): a workflow named "New orchestration" before F511,
    a run log's own lines. So the names and descriptions it gave its things are taken out first, and so is every
    element that keeps its text verbatim (`white-space: pre…`: a log line, a file, code). The check's own fixture
    is named that way and run once, so both are read every time.
    """
    oid = f"orch_uismoke_words_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, "New orchestration (ui-smoke words)"))
    run_to_end(oid)
    theirs = _workspace_words()
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    found: list[str] = []
    pages = _WORD_PAGES + [f"/workflows?id={oid}"]
    try:
        for path in pages:
            await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE)
            text = await page.evaluate(
                """() => {
                  // Synapse's own MCP server keeps its name (Naveen 2026-10-24): it is not the apps D209 renamed. Only
                  // a link to its Settings page, the nav entry or a "Settings › MCP server" link, is let through.
                  const OWN_SERVER = 'a[href$="/settings/mcp_server"]';
                  const verbatim = [...document.body.querySelectorAll('*')]
                    .filter(e => getComputedStyle(e).whiteSpace.startsWith('pre') || e.matches(OWN_SERVER));
                  const shown = verbatim.map(e => e.style.display);
                  verbatim.forEach(e => { e.style.display = 'none'; });
                  const said = document.body.innerText;
                  verbatim.forEach((e, i) => { e.style.display = shown[i]; });
                  return [said, ...[...document.querySelectorAll('[aria-label],[placeholder],[title]')]
                    .filter(e => !e.closest(OWN_SERVER))
                    .flatMap(e => ['aria-label', 'placeholder', 'title'].map(a => e.getAttribute(a)))
                    .filter(Boolean)].join('\\n');
                }""")
            for word in theirs:
                text = text.replace(word, " ")
            for m in _OLD_WORDS.finditer(text):
                around = text[max(0, m.start() - 40): m.end() + 20].replace("\n", " ")
                if not _KEPT_WORDS.search(around):
                    found.append(f"{path}: …{around.strip()}…")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if found:
        raise AssertionError(f"{len(found)} retired words: " + " | ".join(found[:8]))
    return f"no retired word on {len(pages)} pages (text, labels, placeholders, titles)"


@check
async def check_logswitch(browser) -> str:
    """Switching kinds never reads the open log as the other kind's (F503): Agent logs → Workflow logs fetched the
    open agent log's id as a workflow log (`/api/logs/orchestrations/agentrun_…`, a 404, "This log is no longer on
    disk.") until the workflow list arrived, under the Workflow logs pill. Here that list is held for 2 s."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/logs/agents?*", _answer([{"run_id": "agentrun_smoke", "agent_name": "Smoke agent",
                                                      "source": "chat", "session_id": "",
                                                      "started_at": "2026-09-30 10:00:00", "file_size_kb": 1}]))
    await ctx.route("**/api/logs/agents/*", _answer("hello\n", content_type="text/plain"))
    await ctx.route("**/api/logs/orchestrations?*", _answer(_M2H_LOGS, hold=2.0))
    await ctx.route("**/api/logs/orchestrations/*", _answer("STEP START\n", content_type="text/plain"))
    page = await ctx.new_page()
    crossed: list[str] = []
    page.on("request", lambda r: crossed.append(r.url) if "/api/logs/orchestrations/agentrun_" in r.url else None)
    try:
        await page.goto(f"{APP}/logs", wait_until="domcontentloaded")
        await page.locator('[data-log-row="agentrun_smoke"]').wait_for(timeout=30_000)
        await page.get_by_role("button", name="Copy").wait_for(timeout=10_000)
        await page.get_by_role("button", name=re.compile("^Workflow logs")).click()
        await page.wait_for_timeout(500)
        if await page.locator('[data-log-row="agentrun_smoke"]').count():
            raise AssertionError("the agent log is still listed under Workflow logs")
        await page.get_by_text("Loading...").wait_for(timeout=2_000)
        await page.locator(f'[data-log-row="{_M2H_LOGS[0]["run_id"]}"]').wait_for(timeout=10_000)
        if crossed:
            raise AssertionError(f"the agent log was fetched as a workflow log: {crossed[0]}")
    finally:
        await ctx.close()
    return "the agent log left the list at once, Loading... until the workflow logs came; no crossed read"


@check
async def check_logstatus(browser) -> str:
    """A workflow log says how its run went from the runs list (M2h): a failure in words, a finished run quiet,
    and a log with no run never reads as Failed."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/logs/orchestrations?*", _answer(_M2H_LOGS))
    await ctx.route("**/api/logs/orchestrations/*", _answer("STEP START\n", content_type="text/plain"))
    await ctx.route("**/api/orchestrations/runs?*", _answer(_M2H_RUNS))
    await ctx.route("**/api/logs/agents?*", _answer([{"run_id": "run_m2h_agent", "agent_name": "Smoke agent",
                                                      "source": "chat", "session_id": "",
                                                      "started_at": "2026-09-30 10:00:00", "file_size_kb": 1}]))
    await ctx.route("**/api/logs/agents/*", _answer("hello\n", content_type="text/plain"))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/logs", wait_until="domcontentloaded")
        agent = page.locator('[data-log-row="run_m2h_agent"]')
        await agent.wait_for(timeout=30_000)
        if "Failed" in await agent.inner_text():
            raise AssertionError("an agent log with no run reads as Failed")
        await page.get_by_role("button", name=re.compile("^Workflow logs")).click()
        failing = page.locator('[data-log-row="run_m2h_b"]')
        await failing.wait_for(timeout=20_000)
        await page.wait_for_function(
            "() => document.querySelector('[data-log-row=\"run_m2h_b\"]')?.innerText.includes('Failed')", timeout=10_000)
        passing = await page.locator('[data-log-row="run_m2h_a"]').inner_text()
        if "Failed" in passing or "Completed" in passing:
            raise AssertionError(f"a finished run's log is not quiet: {passing!r}")
        if "$0" not in passing:
            raise AssertionError(f"a free run's log does not say $0: {passing!r}")
    finally:
        await ctx.close()
    return "a failed run's log says so, a finished one is quiet with $0, and an agent log reads no status"


_M2H_CRITERIA = [{"id": "c1", "name": "Cites the guides"}, {"id": "c2", "name": "Answers the question"}]
_M2H_RUBRIC = {"id": "rub_m2h", "name": "Smoke quality", "description": "", "version": 1, "criteria": _M2H_CRITERIA,
               "scale": {"min": 0, "max": 10}, "floor": 6, "drop": 1.5, "baseline_runs": 10, "judge_model": ""}


@check
async def check_evalsfirst(browser) -> str:
    """Evals opens on its first rubric's scores (M2h): one strip with Passed, the trend and each criterion's bars;
    never "Nothing selected"."""
    scores = [{"id": i, "run_id": f"run_{i}", "orchestration_id": "orch_m2h", "rubric_id": "rub_m2h",
               "rubric_version": 1, "score": v, "per_criterion": {"c1": {"score": v}, "c2": {"score": v - 1}},
               "per_step": {}, "reasoning": "ok", "judged_at": _NOW, "judge_model": "m", "baseline": None,
               "regression": v < 6, "rule": "floor" if v < 6 else None} for i, v in enumerate((8.0, 5.0, 9.0, 7.0))]
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/rubrics", _answer([_M2H_RUBRIC]))
    await ctx.route("**/api/evals/suites", _answer([]))
    await ctx.route("**/api/orchestrations", _answer([{"id": "orch_m2h", "name": "Smoke workflow", "rubric_id": "rub_m2h",
                                                        "steps": []}]))
    await ctx.route("**/api/evals/orchestrations/orch_m2h*", _answer({
        "scores": scores, "feedback": {}, "rubric": _M2H_RUBRIC,
        "agreement": {"compared": 0, "agreed": 0, "rate": None}, "regressions": []}))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/evals", wait_until="domcontentloaded")
        strip = page.locator("[data-eval-strip]")
        await strip.wait_for(timeout=30_000)
        text = " ".join((await strip.inner_text()).split())
        if "Passed" not in text or "75%" not in text:
            raise AssertionError(f"the strip reads {text!r}")
        bars = await page.locator("[data-eval-criteria] [data-bar-row]").count()
        if bars != 2:
            raise AssertionError(f"{bars} criterion bars for 2 criteria")
        if not await page.locator("[data-eval-trend] svg").count():
            raise AssertionError("no trend")
        if await page.get_by_text("Nothing selected").count():
            raise AssertionError("the detail opened empty")
        if await page.locator('[data-eval-rubric="rub_m2h"] button').get_attribute("aria-current") != "true":
            raise AssertionError("the first rubric is not marked open")
    finally:
        await ctx.close()
    return "the first rubric opens on Latest · Average · Passed · Judge vs people, the trend and its criteria"


_M2H_SCHEDULES = [
    {"id": "sch_m2h_a", "name": "Smoke every two hours", "enabled": True, "created_at": _NOW, "target_type": "orchestration",
     "target_id": "orch_m2h", "prompt": "go", "schedule_type": "cron", "cron_expression": "0 */2 * * *",
     "next_run_at": _NOW, "last_run_at": _NOW},
    {"id": "sch_m2h_b", "name": "Smoke three days", "enabled": False, "created_at": _NOW, "target_type": "agent",
     "target_id": "agent_x", "prompt": "go", "schedule_type": "cron", "cron_expression": "0 7 * * 1,3,5",
     "missed_run_policy": "skip", "next_run_at": _NOW, "last_run_at": None},
]


@check
async def check_schedulewords(browser) -> str:
    """A schedule says how often in words (M2h, the shared cronWords), Paused when it is off, and how its last
    run went when that run is known."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/schedules", _answer(_M2H_SCHEDULES))
    await ctx.route("**/api/orchestrations/runs?*", _answer([{
        "run_id": "run_m2h_s", "orchestration_id": "orch_m2h", "status": "failed", "session_id": "schedule_sch_m2h_a",
        "started_at": _NOW, "ended_at": _NOW}]))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/schedules", wait_until="domcontentloaded")
        a = page.locator('[data-schedule-row="sch_m2h_a"]')
        await a.wait_for(timeout=30_000)
        await asyncio.sleep(1.0)
        a_text = await a.inner_text()
        b_text = await page.locator('[data-schedule-row="sch_m2h_b"]').inner_text()
        for want, got in (("Every 2 hours", a_text), ("Failed", a_text), ("Mon, Wed, Fri 07:00 UTC", b_text),
                          ("Paused", b_text)):
            if want not in got:
                raise AssertionError(f"{want!r} not in {got!r}")
    finally:
        await ctx.close()
    return "Every 2 hours with its failed last run; Mon, Wed, Fri 07:00 UTC, Paused"


@check
async def check_scheduledrawer(browser) -> str:
    """A schedule opens in a drawer with its fields (M2h): Run as as a radio group set to what it runs, Esc closes
    it, and New schedule opens one with the guide unfolded."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/schedules", _answer(_M2H_SCHEDULES))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/schedules", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Open Smoke every two hours").click()
        dialog = page.get_by_role("dialog")
        await dialog.get_by_role("heading", name="Edit schedule").wait_for(timeout=10_000)
        workflow = dialog.get_by_role("radiogroup", name="Run as").get_by_role("radio", name="Workflow")
        if await workflow.get_attribute("aria-checked") != "true":
            raise AssertionError("Run as does not show the workflow it runs")
        if await dialog.locator("#sched-cron").input_value() != "0 */2 * * *":
            raise AssertionError("the drawer lost the expression")
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="detached", timeout=5_000)
        await page.locator("main header").get_by_role("button", name="New schedule").click()
        await page.get_by_role("dialog").get_by_text("Interval — run every N minutes/hours/days").wait_for(timeout=5_000)
    finally:
        await ctx.close()
    return "the drawer shows the schedule's fields, Esc closes it, and New schedule unfolds the guide"


# ── Boards (BOARD1b, D282) ─────────────────────────────────────────────────────
# Each seeds through the API (Boards switched on, its own board, workflow, schedule or agent) and removes what it made.


def _boards_on() -> None:
    api("/api/settings", "POST", {"boards_enabled": True})


@check
async def check_boards(browser) -> str:
    """Knowledge › Boards (C7): a shared board opens as columns of stickies with its access list; a person's task is
    added under Open, reads as a person's, opens in its drawer with its history, and its add is taken back with Undo."""
    _boards_on()
    name = f"Smoke board {int(time.time())}"
    made = api("/api/boards", "POST", {"name": name})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/boards?board={made['id']}", wait_until="domcontentloaded")
        await page.get_by_role("heading", name=name).wait_for(timeout=30_000)
        await page.get_by_role("heading", name="Who can use this board").wait_for(timeout=5_000)
        await page.get_by_role("button", name="Add a task").click()
        await page.get_by_label("Title").fill("Smoke card")
        await page.get_by_role("button", name="Add", exact=True).click()
        sticky = page.locator("[data-board-card]", has_text="Smoke card")
        await sticky.wait_for(timeout=10_000)
        if "border-l-person" not in (await sticky.get_attribute("class") or ""):
            raise AssertionError("a person's card has no amber edge")
        await sticky.click()
        drawer = page.get_by_role("dialog")
        await drawer.get_by_text(re.compile("added it")).wait_for(timeout=10_000)
        await drawer.get_by_role("button", name=re.compile(r"^Undo change \d+")).first.click()
        await drawer.get_by_text("Archived", exact=True).first.wait_for(timeout=10_000)
    finally:
        await ctx.close()
        try:
            api(f"/api/boards/{made['id']}", "DELETE")
        except Exception:
            pass
    return "a shared board opens with its list; a person's card is added, opened, and its add taken back"


@check
async def check_workflowboard(browser) -> str:
    """A workflow's board beside its canvas (C7, 4B): the top bar's Board opens it in the card, and a person's card
    there is the next run's instruction."""
    _boards_on()
    oid = f"orch_uismoke_board_{int(time.time())}"
    api("/api/orchestrations", "POST", {
        "id": oid, "name": f"Smoke board workflow {oid[-6:]}", "description": "", "entry_step_id": "p",
        "trigger": "manual", "steps": [{"id": "p", "name": "Say", "type": "print", "print_content": "hi",
                                        "output_key": "o", "next_step_id": None}]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await page.get_by_role("button", name=re.compile(r"^Board")).click(timeout=30_000)
        card = page.get_by_role("region", name="This workflow's board")
        await card.get_by_text("No cards yet").wait_for(timeout=10_000)
        await card.get_by_role("button", name="Add a task").click()
        await card.get_by_label("Title").fill("Skip the test supplier")
        await card.get_by_role("button", name="Add", exact=True).click()
        await card.locator("[data-board-card]", has_text="Skip the test supplier").wait_for(timeout=10_000)
        await page.get_by_role("button", name=re.compile(r"^Board · 1")).wait_for(timeout=40_000)
    finally:
        await ctx.close()
        try:
            api(f"/api/orchestrations/{oid}", "DELETE")
        except Exception:
            pass
    return "Board opens the workflow's board in the card, and a person's card lands there"


@check
async def check_scheduleboard(browser) -> str:
    """A scheduled agent's board in its schedule's drawer (C7), folded until opened (Naveen, 2026-10-16: optional),
    its folded head counting the cards that wait."""
    _boards_on()
    stamp = int(time.time())
    aid = f"agent_uismoke_sboard_{stamp}"
    api("/api/agents", "POST", _agent(aid, f"Board smoke {stamp}"))
    sched = api("/api/schedules", "POST", {"name": f"Board smoke {stamp}", "target_type": "agent", "target_id": aid,
                                          "prompt": "check", "schedule_type": "interval", "interval_value": 1, "interval_unit": "days",
                                          "enabled": False})
    api(f"/api/boards/scoped/schedule/{sched.get('id')}/cards", "POST", {"title": f"Smoke ask {stamp}", "kind": "task"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/schedules", wait_until="domcontentloaded")
        await page.get_by_role("button", name=f"Open Board smoke {stamp}").click(timeout=30_000)
        dialog = page.get_by_role("dialog")
        fold = dialog.get_by_role("button", name=re.compile(r"^Board(?!s)"))
        await fold.get_by_text("1 open").wait_for(timeout=10_000)
        if await fold.get_attribute("aria-expanded") != "false":
            raise AssertionError("the schedule's board is open before anyone opens it")
        if await dialog.get_by_text(f"Smoke ask {stamp}").count():
            raise AssertionError("a folded board still shows its cards")
        await fold.click()
        await dialog.get_by_text(f"Smoke ask {stamp}").wait_for(timeout=10_000)
    finally:
        await ctx.close()
        for path in (f"/api/schedules/{sched.get('id')}", f"/api/agents/{aid}"):
            try:
                api(path, "DELETE")
            except Exception:
                pass
    return "a schedule that runs an agent shows its board in its drawer, folded, counting the card that waits"


@check
async def check_agentboards(browser) -> str:
    """The agent editor's Boards section (C2): folded until opened, its folded row saying what is set (Naveen,
    2026-10-16); inside, its own board's switch, on unless turned off, and the shared boards whose lists name it."""
    _boards_on()
    stamp = int(time.time())
    aid = f"agent_uismoke_aboards_{stamp}"
    api("/api/agents", "POST", _agent(aid, f"Boards agent {stamp}"))
    made = api("/api/boards", "POST", {"name": f"Smoke shared {stamp}"})
    api(f"/api/boards/{made['id']}/access", "PUT", {"entries": [{"kind": "agent", "id": aid, "access": "work"}]})
    # F583: a board its schedule is named on reaches it too, and says so.
    sched = api("/api/schedules", "POST", {"name": f"Smoke intake {stamp}", "target_type": "agent", "target_id": aid,
                                          "prompt": "check", "schedule_type": "interval", "interval_value": 1,
                                          "interval_unit": "days", "enabled": False})
    via = api("/api/boards", "POST", {"name": f"Smoke via schedule {stamp}"})
    api(f"/api/boards/{via['id']}/access", "PUT", {"entries": [{"kind": "schedule", "id": sched["id"], "access": "add"}]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        await page.locator('aside[aria-label="Your agents"]').get_by_text(
            f"Boards agent {stamp}", exact=True).click(timeout=30_000)
        section = page.locator("#setup-boards")
        fold = section.get_by_role("button", name=re.compile(r"^Boards"))
        await fold.get_by_text("Own board on · 2 shared").wait_for(timeout=10_000)
        if await fold.get_attribute("aria-expanded") != "false":
            raise AssertionError("the agent's Boards section is open before anyone opens it")
        await fold.click()
        switch = section.get_by_role("switch", name="The board of the workflow or schedule it runs in")
        await switch.wait_for(timeout=10_000)
        if await switch.get_attribute("aria-checked") != "true":
            raise AssertionError("an agent's own board does not read as on")
        await section.get_by_text(f"Smoke shared {stamp}").wait_for(timeout=10_000)
        await section.get_by_text(f"Smoke via schedule {stamp}").wait_for(timeout=10_000)
        await section.get_by_text(f"Through the schedule ‘Smoke intake {stamp}’").wait_for(timeout=10_000)
    finally:
        await ctx.close()
        # The schedule first: an agent a schedule runs is not deleted (F467).
        for path in (f"/api/schedules/{sched.get('id')}", f"/api/boards/{via['id']}", f"/api/boards/{made['id']}",
                     f"/api/agents/{aid}"):
            try:
                api(path, "DELETE")
            except Exception:
                pass
    return ("the agent's Boards section is folded, says what is set, and shows its own board on, the shared board that "
            "names it and the one its schedule brings, saying so")


_FIRSTRUN_PROBLEM = "Ollama is not reachable at http://127.0.0.1:9."
_FIRSTRUN_MODELS = {"providers": {
    "ollama": {"available": False, "models": []},
    "gemini": {"available": True, "models": ["gemini-2.5-flash"]},
    "anthropic_cli": {"available": True, "models": ["cli.claude.opus", "cli.claude"],
                      "cli": {"version": "2.1.301", "signed_in": True, "sign_in_hint": None,
                              "models_source": "list", "models_note": None, "note": None}},
    "codex_cli": {"available": True, "models": ["cli.codex.gpt-6-luna", "cli.codex"],
                  "cli": {"version": "0.163.0", "signed_in": False, "sign_in_hint": "Run `codex login` in a terminal.",
                          "models_source": "cli", "models_note": "From Codex", "note": None}},
}}


async def _firstrun_page(browser, path: str, state: dict, theme: str | None = None, width=WIDE,
                         fresh: bool = False, late: float = 0):
    """A page over a stubbed install whose model cannot answer (ONB2): `state["closed"]` is the setting,
    `state["posts"]` every settings write. `fresh` is a browser that has seen no tour (`firsttour`); `late` holds the
    readiness answer back that many seconds while the step is still to open, as a slow backend would."""
    opener = browser.fresh_context if fresh else browser.new_context
    ctx = await opener(viewport={"width": width[0], "height": width[1]})
    if theme:
        await ctx.add_init_script(f"try{{localStorage.setItem('synapseTheme','{theme}')}}catch(e){{}}")
    await ctx.add_init_script("try{localStorage.removeItem('synapseHomeStartHidden')}catch(e){}")
    settings = {"model": "ollama.mistral", "builder_model": "", "gemini_key": "", "gemini_key_set": True,
                "gemini_key_hint": "x9Q2"}

    async def onboarding(route):
        if late and not state["closed"]:
            await asyncio.sleep(late)
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"model": "ollama.mistral", "ready": False, "problem": _FIRSTRUN_PROBLEM, "closed": state["closed"]}))

    async def settings_route(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            state["posts"].append(body)
            if body.get("first_run_models_closed"):
                state["closed"] = True
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))

    await ctx.route("**/api/onboarding/models", onboarding)
    await ctx.route(re.compile(r".*/api/models(\?.*)?$"), _answer(_FIRSTRUN_MODELS))
    await ctx.route(re.compile(r".*/api/settings$"), settings_route)
    await ctx.route("**/api/status", _answer({"agents": {}, "active_agent_id": None, "overall": "operational",
                                               "model": "ollama.mistral", "mode": "local", "provider": "ollama",
                                               "model_ready": False, "model_problem": _FIRSTRUN_PROBLEM}))
    page = await ctx.new_page()
    await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
    return ctx, page


@check
async def check_firstrun(browser) -> str:
    """An install whose model cannot answer opens the first-run model step once (ONB2, F728, D303): what is on this
    computer with its state (a signed-out CLI with its own hint), a saved key's tile, a click outside keeps it, the
    two models save as picked with a signed-out CLI's models left out; Escape closes it for good; Home's "Connect a
    model" and Chat's line open it again."""
    state = {"closed": False, "posts": []}
    bad: list[str] = []
    ctx, page = await _firstrun_page(browser, "/dashboard", state)
    dialog = page.get_by_test_id("model-setup")
    try:
        await dialog.wait_for(timeout=30_000)
        if _FIRSTRUN_PROBLEM not in await dialog.inner_text():
            bad.append("it does not say what is missing")
        found = {k: await page.locator(f"[data-found='{k}']").get_attribute("data-state")
                 for k in ("anthropic_cli", "codex_cli")}
        if found != {"anthropic_cli": "ready", "codex_cli": "signed-out"}:
            bad.append(f"found here reads {found}")
        if "codex login" not in await page.locator("[data-found='codex_cli']").inner_text():
            bad.append("the signed-out CLI does not give its sign-in hint")
        if await page.locator("[data-found='ollama']").count():
            bad.append("Ollama is listed though it is not running")
        if await page.locator("[data-two-ways]").count():
            bad.append("the two ways show though a CLI and a key are here")
        gemini = page.locator("[data-setup-provider='gemini']")
        if await gemini.get_attribute("aria-pressed") != "true" or "Key saved" not in await gemini.inner_text():
            bad.append("the saved Gemini key is not picked and marked")
        await page.mouse.click(5, 5)
        await asyncio.sleep(0.5)
        if not await dialog.is_visible():
            bad.append("a click outside closed it (F726)")
        await dialog.get_by_role("button", name="Continue").click()
        # The kit's Combobox opens from a Radix popover trigger, which is a button.
        everyday = dialog.get_by_role("button", name="Everyday model")
        await everyday.click()
        listed = await page.get_by_role("option").all_inner_texts()
        if any("cli.codex" in t for t in listed):
            bad.append("a signed-out CLI's models can be picked")
        await page.get_by_role("option", name="cli.claude.opus").click()
        await dialog.get_by_role("button", name="Done").click()
        await dialog.wait_for(state="hidden", timeout=5_000)
        if state["posts"][-1:] != [{"model": "cli.claude.opus", "builder_model": "", "first_run_models_closed": True}]:
            bad.append(f"Done saved {state['posts'][-1:]}")

        # Opened again by Home's step; Escape closes it.
        await page.get_by_role("button", name="Connect a model").click()
        await dialog.wait_for(timeout=5_000)
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="hidden", timeout=5_000)
        await page.reload(wait_until="domcontentloaded")
        await page.locator("main h1").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        if await dialog.count():
            bad.append("closed once, it opened again on its own")
    finally:
        await ctx.close()

    # A first load closed by Escape is remembered; Chat's line opens it.
    state = {"closed": False, "posts": []}
    ctx, page = await _firstrun_page(browser, "/", state)
    dialog = page.get_by_test_id("model-setup")
    try:
        await dialog.wait_for(timeout=30_000)
        await page.keyboard.press("Escape")
        await dialog.wait_for(state="hidden", timeout=5_000)
        await asyncio.sleep(0.5)
        if {"first_run_models_closed": True} not in state["posts"]:
            bad.append(f"Escape did not remember it was closed: {state['posts']}")
        await page.locator("[data-connect-model]").click()
        await dialog.wait_for(timeout=5_000)
    finally:
        await ctx.close()
    if bad:
        raise AssertionError("; ".join(bad))
    return ("opens once on a model that cannot answer, CLI states and a saved key shown, outside click kept, "
            "two models saved, Escape remembered, Home and Chat reopen it")


async def main() -> int:
    global APP, API
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--app", default=APP)
    ap.add_argument("--api", default=API)
    ap.add_argument("--only", action="append", help="run just these checks")
    args = ap.parse_args()

    APP, API = args.app.rstrip("/"), args.api.rstrip("/")
    if APP.startswith("http://127.0.0.1"):
        print("refusing 127.0.0.1: a dev server there never hydrates — use localhost", file=sys.stderr)
        return 1

    from playwright.async_api import async_playwright

    names = args.only or list(CHECKS)
    failed = 0
    if any(n != "firstrun" for n in names):
        # The first-run model step (ONB2) would open over every page of a scratch store whose model cannot
        # answer; `firstrun` stubs its own.
        try:
            api("/api/settings", "POST", {"first_run_models_closed": True})
        except Exception as exc:
            print(f"note: could not close the first-run model step: {exc}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROME if os.path.exists(CHROME) else None,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        for name in names:
            fn = CHECKS.get(name)
            if fn is None:
                print(f"?? {name}: no such check"); failed += 1; continue
            started = time.time()
            try:
                note = await fn(ToursSeen(browser))
                print(f"ok {name}: {note} ({time.time() - started:.1f}s)")
            except Exception as exc:                      # a broken check fails alone
                failed += 1
                print(f"FAIL {name}: {exc}")
        await browser.close()
    print(f"\n{len(names) - failed} passed, {failed} failed")
    return failed


@check
async def check_skillsfirst(browser) -> str:
    """Skills never opens on an empty pane (M2h-2): the first skill is open beside the list, on Preview, with the
    agents that use it; New skill opens a blank one in its place."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/skills", _answer([
        {"name": "smoke-skill", "description": "A smoke skill", "source": "library"},
        {"name": "smoke-disk", "description": "On disk", "source": "disk", "path": "/x/SKILL.md"}]))
    await ctx.route("**/api/skills/smoke-skill", _answer({"name": "smoke-skill", "description": "A smoke skill",
                                                          "body": "# Smoke body"}))
    await ctx.route("**/api/agents", _answer([{"id": "a1", "name": "Smoke agent", "skills": ["smoke-skill"]}]))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/skills", wait_until="domcontentloaded")
        first = page.locator('[data-skill-row="smoke-skill"] button').first
        await first.wait_for(timeout=30_000)
        if await first.get_attribute("aria-current") != "true":
            raise AssertionError("the first skill is not open")
        await page.get_by_role("heading", name="Smoke body").wait_for(timeout=10_000)
        await page.get_by_text("Used by Smoke agent").wait_for(timeout=5_000)
        await page.get_by_role("button", name="New skill").first.click()
        await page.get_by_role("heading", name="New skill").wait_for(timeout=5_000)
        if await page.get_by_placeholder("my-skill-name").input_value() != "":
            raise AssertionError("New skill did not open a blank one")
    finally:
        await ctx.close()
    return "the first skill is open on Preview with who uses it; New skill opens a blank one"


@check
async def check_vaultfirst(browser) -> str:
    """The Vault never opens on an empty pane (M2h-2): the first file is open beside the tree."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/vault/tree*", _answer({"tree": [
        {"name": "notes", "path": "notes", "type": "folder", "children": [
            {"name": "a.md", "path": "notes/a.md", "type": "file", "ext": ".md"}]},
        {"name": "top.txt", "path": "top.txt", "type": "file", "ext": ".txt"}], "source": "local"}))
    await ctx.route("**/api/vault/file*", _answer({"content": "smoke text"}))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/vault", wait_until="domcontentloaded")
        row = page.get_by_role("button", name="top.txt", exact=True)
        await row.wait_for(timeout=30_000)
        await page.get_by_role("heading", name="top.txt").wait_for(timeout=10_000)
        if await row.get_attribute("aria-current") != "true":
            raise AssertionError("the open file is not marked in the tree")
        if await page.get_by_text("Select a file to edit").count():
            raise AssertionError("the pane still asks for a pick")
    finally:
        await ctx.close()
    return "the first file is open beside the tree"


@check
async def check_channelsfirst(browser) -> str:
    """Channels never opens on an empty pane (M2h-2): the first channel is open, its status a quiet word."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/messaging/channels", _answer([
        {"id": "ch_a", "name": "Smoke first", "platform": "slack", "status": "running", "agent_ids": [],
         "multi_agent_mode": False, "enabled": True, "credentials": {}},
        {"id": "ch_b", "name": "Smoke second", "platform": "telegram", "status": "stopped", "agent_ids": [],
         "multi_agent_mode": False, "enabled": True, "credentials": {}}]))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/channels", wait_until="domcontentloaded")
        first = page.locator('[data-channel-row="ch_a"] button').first
        await first.wait_for(timeout=30_000)
        await page.get_by_role("heading", name="Smoke first").wait_for(timeout=10_000)
        if await first.get_attribute("aria-current") != "true":
            raise AssertionError("the first channel is not open")
        if await page.get_by_text("Select a channel or create a new one.").count():
            raise AssertionError("the pane still asks for a pick")
        await page.get_by_role("button", name="New channel").first.click()
        await page.get_by_role("heading", name="New channel").wait_for(timeout=5_000)
    finally:
        await ctx.close()
    return "the first channel is open; New channel opens a blank one"



@check
async def check_channelplatform(browser) -> str:
    """A saved channel switched to another platform stays that channel (F499): before, the switch dropped its id
    and Save & connect made a second channel beside it. One channel after the save, its id unchanged, and the old
    platform's token not carried across (the server keeps no secret over a switch)."""
    name = f"Smoke platform switch {int(time.time())}"
    made = api("/api/messaging/channels", "POST", {
        "name": name, "platform": "telegram", "mode": "polling", "credentials": {"bot_token": "123456:smoke-token"}})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    # Starting it would reach Discord: the enable answers the channel as saved, stopped.
    await ctx.route("**/api/messaging/channels/*/enable",
                    _answer({**made, "platform": "discord", "status": "stopped", "credentials": {}}, methods=("POST",)))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/channels", wait_until="domcontentloaded")
        await page.locator(f'[data-channel-row="{made["id"]}"] button').first.click(timeout=30_000)
        await page.get_by_role("heading", name=name).wait_for(timeout=10_000)
        await page.get_by_role("radiogroup", name="Platform").get_by_role("radio", name="Discord").click()
        async with page.expect_response(lambda r: r.url.endswith("/api/messaging/channels")
                                        and r.request.method == "POST", timeout=15_000) as saved:
            await page.get_by_role("button", name="Save & connect").click()
        if (await saved.value).status != 200:
            raise AssertionError(f"the save answered {(await saved.value).status}")
        mine = [c for c in api("/api/messaging/channels") if c["name"] == name]
    finally:
        await ctx.close()
        for c in api("/api/messaging/channels"):
            if c["name"] == name:
                api(f"/api/messaging/channels/{c['id']}", "DELETE")
    if len(mine) != 1:
        raise AssertionError(f"{len(mine)} channels named {name!r} after the switch (expected 1)")
    if mine[0]["id"] != made["id"] or mine[0]["platform"] != "discord":
        raise AssertionError(f"the saved channel is {mine[0]['id']} on {mine[0]['platform']}")
    if (mine[0].get("credentials_set") or {}).get("bot_token"):
        raise AssertionError("the Telegram token was carried into the Discord channel")
    return "the switched channel saved in place: one channel, same id, no token carried across"


async def _status_saying(page, text: str, seconds: float = 8) -> str:
    """The first toast (`role="status"`) that says `text` within `seconds`, or every one that showed instead."""
    seen: list[str] = []
    end = time.time() + seconds
    while time.time() < end:
        for said in await page.get_by_role("status").all_inner_texts():
            said = " ".join(said.split())
            if text in said:
                return said
            if said and said not in seen:
                seen.append(said)
        await asyncio.sleep(0.2)
    raise AssertionError(f"no toast said {text!r}; seen {seen}")


@check
async def check_channelfeedback(browser) -> str:
    """Channels say why a channel did not start, stop or go (F500, F513). Before: Start and Stop were silent (a
    refusal, or a token the platform refused, showed nowhere but the list's dot), Delete was silent, Save &
    connect said "Channel saved and started" when its start was refused, and a missing token read as the
    KeyError's own text, "'bot_token'"."""
    name = f"Smoke feedback {int(time.time())}"
    made = api("/api/messaging/channels", "POST", {"name": name, "platform": "discord", "mode": "polling", "credentials": {}})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    said: list[str] = []
    try:
        await page.goto(f"{APP}/channels", wait_until="domcontentloaded")
        await page.locator(f'[data-channel-row="{made["id"]}"] button').first.click(timeout=30_000)
        await page.get_by_role("heading", name=name).wait_for(timeout=10_000)
        # A start the platform refuses — here no token is saved: the real backend answers `error` with its reason.
        await page.get_by_role("button", name=f"Start {name}").click()
        said.append(await _status_saying(page, f"{name} did not start: No bot token is saved for this channel."))
        await page.get_by_text("No bot token is saved for this channel.", exact=True).wait_for(timeout=5_000)
        # A refused start, from the row and from Save & connect.
        await page.route("**/api/messaging/channels/*/enable",
                         _answer({"detail": "Messaging is not enabled."}, status=503, methods=("POST",)))
        await page.get_by_role("button", name=f"Start {name}").click()
        said.append(await _status_saying(page, f"Could not start {name}: Messaging is not enabled."))
        await page.get_by_role("button", name="Save & connect").click()
        said.append(await _status_saying(page, "Saved, but it did not start: Messaging is not enabled."))
        # A refused delete.
        await page.route(f"**/api/messaging/channels/{made['id']}",
                         _answer({"detail": "Channel not found"}, status=404, methods=("DELETE",)))
        await page.get_by_role("button", name=f"Delete {name}").click()
        await page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        said.append(await _status_saying(page, f"Could not delete {name}: Channel not found"))
        if await page.get_by_text("Channel saved and started").count():
            raise AssertionError("a refused start still said \"Channel saved and started\"")
    finally:
        await ctx.close()
        for c in api("/api/messaging/channels"):
            if c["name"] == name:
                api(f"/api/messaging/channels/{c['id']}", "DELETE")
    return "; ".join(said)


@check
async def check_webhookonce(browser) -> str:
    """The webhook address saves once (F501): Enter, then the blur that followed, posted /api/settings twice and
    toasted twice; leaving the field unchanged saved again; a refused save toasted "Webhook address saved"."""
    before = api("/api/settings").get("messaging_webhook_base_url") or ""
    address = f"https://smoke-{int(time.time())}.example.com"
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/api/settings") else None)
    try:
        # The page's own settings read fills the field, and proves it hydrated: typed into before that,
        # Enter has no handler.
        async with page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "GET",
                                        timeout=30_000):
            await page.goto(f"{APP}/channels", wait_until="domcontentloaded")
        field = page.get_by_role("textbox", name="Public webhook base URL")
        await field.wait_for(timeout=30_000)
        await field.fill(address)
        await field.press("Enter")
        await _status_saying(page, "Webhook address saved")
        await page.get_by_role("heading", name="Channels").first.click()  # the blur Enter used to be followed by
        await field.click()
        await page.get_by_role("heading", name="Channels").first.click()  # in and out, unchanged
        await page.wait_for_timeout(1_500)
        if len(posts) != 1:
            raise AssertionError(f"{len(posts)} saves for one address (expected 1)")
        await page.route("**/api/settings", _answer({"detail": "Not a URL."}, status=422, methods=("POST",)))
        await field.fill("https://refused.example.com")
        await field.press("Enter")
        said = await _status_saying(page, "Failed to save the webhook address: Not a URL.")
    finally:
        await ctx.close()
        api("/api/settings", "POST", {"agent_name": api("/api/settings").get("agent_name") or "Synapse",
                                      "messaging_webhook_base_url": before})
    return f"one save for Enter + blur, none for an unchanged field; refused: {said}"

# ── the Phone tab (VOX1c) ────────────────────────────────────────────────────

#: A number as the channel list serves it (the Channels tab must not show it), and the Phone tab's reads.
_PHONE_NUMBERS = [
    {"id": "chn_ph1", "name": "Smoke support line", "platform": "twilio_voice", "status": "running", "enabled": True,
     "agent_ids": ["smoke_front"], "visitor_tools": [], "credentials": {}, "multi_agent_mode": False,
     "voice": {"number": "+14155550142", "number_sid": "PN" + "1" * 32, "language": "en-US",
               "signing_error": "Twilio refused the key for signing. Calls still work."}},
    {"id": "chn_ph2", "name": "Smoke after hours", "platform": "twilio_voice", "status": "stopped", "enabled": False,
     "agent_ids": ["smoke_front"], "visitor_tools": [], "credentials": {}, "multi_agent_mode": False,
     "voice": {"number": "+16285550199", "number_sid": "PN" + "2" * 32}},
]
_PHONE_OPTIONS = {
    "available": True, "reason": "", "fast_for_voice": ["groq.", "synapse.nova-micro"], "default_language": "en-US",
    "tts_providers": ["ElevenLabs", "Google", "Amazon"],
    "languages": [{"code": "en-US", "name": "English (United States)", "tts": ["ElevenLabs", "Google", "Amazon"],
                   "stt": {"Google": ["long"]}, "default_stt": {"provider": "Google", "model": "long"}},
                  {"code": "en-IN", "name": "English (India)", "tts": ["ElevenLabs", "Google", "Amazon"],
                   "stt": {"Google": ["long"]}, "default_stt": {"provider": "Google", "model": "long"}}],
}
_PHONE_AGENTS = [{"id": "smoke_front", "name": "Smoke front desk", "model": "anthropic.claude-sonnet", "type": "conversational",
                  "planning": True, "reasoning_effort": "medium"}]
_PHONE_CALL = {
    "id": "CA" + "1" * 32, "channel_id": "chn_ph1", "agent_id": "smoke_front",
    "session_id": "voice_chn_ph1_CA" + "1" * 32, "caller": "+14155551234", "called": "+14155550142",
    "started_at": "2026-10-11T09:00:00+00:00", "ended_at": "2026-10-11T09:03:12+00:00", "seconds": 192,
    "segments": [{"seconds": 192}], "outcome": "caller_hung_up", "approvals": ["ap_1"], "first_words_ms": [900, 1100],
    "transfer": None, "voicemail": None, "recording_sid": None, "error": None,
}
_PHONE_HISTORY = {
    "turns": [
        {"user": "My lamp arrived broken. Order four eight two one three.", "assistant": "I found it: the Arden lamp. I can",
         "tools": ["lookup_order"], "calls": [{"tool": "lookup_order", "args": {"order": "48213"}, "result": "delivered"}],
         "timestamp": "2026-10-11T09:00:14Z", "interrupted": True, "filler": ["One moment while I check that."]},
        {"user": "A refund, please.", "assistant": "I've asked someone on the team to approve that.", "tools": ["refund"],
         "calls": [], "timestamp": "2026-10-11T09:00:36Z"},
    ],
    "pending_approval": {"kind": "tool_approval", "tool_name": "refund", "tool_args": {"order": "48213"},
                         "approval_id": "ap_smoke_1"},
}


async def _phone_context(browser, *, numbers=None, calls=None, viewport=WIDE):
    ctx = await browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    await ctx.route("**/api/messaging/channels", _answer(numbers if numbers is not None else _PHONE_NUMBERS))
    await ctx.route("**/api/messaging/twilio_voice/options", _answer(_PHONE_OPTIONS))
    await ctx.route("**/api/agents", _answer(_PHONE_AGENTS))
    await ctx.route("**/api/messaging/twilio_voice/channels/*/calls*", _answer({"calls": calls if calls is not None else [_PHONE_CALL]}))
    await ctx.route("**/api/sessions/*/history*", _answer(_PHONE_HISTORY))
    await ctx.route("**/api/usage/logs*", _answer({"logs": [{"estimated_cost": 0.0123}], "count": 1}))
    await ctx.route("**/api/widgets/visitor-tools*", _answer({"tools": [{"name": "lookup_order", "category": "read_only"},
                                                                         {"name": "refund", "category": "destructive"}]}))
    return ctx


@check
async def check_channelsnovoice(browser) -> str:
    """The Channels tab lists no phone numbers (V29): they live on the Phone tab. A number first in the list used to
    throw as the screen opened it (F447)."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.route("**/api/messaging/channels", _answer([_PHONE_NUMBERS[0], {
        "id": "ch_a", "name": "Smoke chat", "platform": "slack", "status": "running", "agent_ids": [],
        "multi_agent_mode": False, "enabled": True, "credentials": {}}]))
    page = await ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        await page.goto(f"{APP}/channels", wait_until="domcontentloaded")
        await page.get_by_role("heading", name="Smoke chat").wait_for(timeout=30_000)
        if await page.locator('[data-channel-row="chn_ph1"]').count():
            raise AssertionError("a phone number is listed on the Channels tab")
        if errors:
            raise AssertionError(f"the page threw: {errors[0][:200]}")
    finally:
        await ctx.close()
    return "Channels opens its first chat channel; the number is not listed and nothing throws"


@check
async def check_phonetab(browser) -> str:
    """The Phone tab (V29, VOX1c): titled Phone under Channels' name (GUIDE1, F914) with its own tab current, the first number open (never an
    empty pane), its recent calls, V15's quiet notes for a slow agent with thinking and planning on, and the
    Restricted key's unsigned note."""
    ctx = await _phone_context(browser)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/channels/phone", wait_until="domcontentloaded")
        await page.get_by_role("heading", name="Smoke support line").wait_for(timeout=30_000)
        if (await page.locator("main h1").first.inner_text()).strip() != "Phone":
            raise AssertionError("the page is not titled by its tab")
        if await page.evaluate(_EYEBROW) != "Channels":
            raise AssertionError("the hub's name is not over the title")
        tab = page.get_by_role("link", name="Phone", exact=True).first
        if await tab.get_attribute("aria-current") != "page":
            raise AssertionError("the Phone tab is not the current tab")
        row = page.locator('[data-phone-row="chn_ph1"] button').first
        if await row.get_attribute("aria-current") != "true":
            raise AssertionError("the first number is not open")
        await page.locator(f'[data-call-row="{_PHONE_CALL["id"]}"]').wait_for(timeout=10_000)
        notes = await page.locator("[data-phone-notes]").inner_text()
        for words in ("faster model", "Thinking (medium) and planning"):
            if words not in notes:
                raise AssertionError(f"the agent's notes miss {words!r}: {notes[:200]}")
        if not await page.get_by_text("Calls are checked, not signed.").count():
            raise AssertionError("the unsigned note is missing")
    finally:
        await ctx.close()
    return "Phone under Channels' name with Phone current; the first number open with its calls, notes and the unsigned note"


@check
async def check_phonepane(browser) -> str:
    """One Save sends the number's whole voice block (the APIs replace it whole), and turning a number off says what
    Twilio kept (`left`), never hidden."""
    ctx = await _phone_context(browser)
    posted: list[dict] = []

    async def save(route):
        if route.request.method != "POST":
            return await route.fallback()
        posted.append(json.loads(route.request.post_data or "{}"))
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(posted[-1]))
    await ctx.route("**/api/messaging/channels", save)
    await ctx.route("**/api/messaging/channels/chn_ph1/disable", _answer(
        {"status": "stopped", "channel_id": "chn_ph1", "left": ["Twilio would not remove this number's signing key."]},
        methods=("POST",)))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/channels/phone", wait_until="domcontentloaded")
        await page.get_by_role("heading", name="Smoke support line").wait_for(timeout=30_000)
        await page.locator("#phone-pane-filler").fill("Bear with me.")
        await page.locator("[data-phone-savebar]").get_by_role("button", name="Save").click()
        for _ in range(50):
            if posted:
                break
            await asyncio.sleep(0.1)
        if not posted:
            raise AssertionError("Save sent nothing")
        voice = posted[0].get("voice") or {}
        missing = [k for k in ("notice", "filler", "filler_late", "transfer_number", "recording", "language", "voice",
                               "tts_provider", "max_minutes", "number", "number_sid") if k not in voice]
        if voice.get("filler") != "Bear with me." or missing:
            raise AssertionError(f"the save's voice block: filler {voice.get('filler')!r}, missing {missing}")
        await page.get_by_role("button", name="Turn off").click()
        await page.get_by_text("Twilio would not remove this number's signing key.").wait_for(timeout=10_000)
    finally:
        await ctx.close()
    return "Save sent the whole voice block with the change; Turn off showed what Twilio kept"


@check
async def check_calllog(browser) -> str:
    """A call's log over the number's pane, opened by its address as the bell links it (VOX1c): the transcript with
    the filler, the tool and the barge-in cut, the approval still waiting in the person tone, and the strip with
    Twilio's estimate said as one."""
    ctx = await _phone_context(browser)
    answers: list[dict] = []
    answered = {"turns": _PHONE_HISTORY["turns"] + [
        {"user": "A refund, please.", "assistant": "Done: the refund is on its way.", "tools": ["refund"],
         "calls": [{"tool": "refund", "args": {"order": "48213"}, "result": "refunded"}],
         "timestamp": "2026-10-11T09:20:00Z"}], "pending_approval": None}

    async def history(route):
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps(answered if answers else _PHONE_HISTORY))

    async def approve(route):
        answers.append(json.loads(route.request.post_data or "{}"))
        await route.fulfill(status=200, content_type="application/json", body='{"status": "allowed"}')
    await ctx.route("**/api/sessions/*/history*", history)
    await ctx.route("**/api/messaging/twilio_voice/approvals/*", approve)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/channels/phone?number=chn_ph1&call={_PHONE_CALL['id']}", wait_until="domcontentloaded")
        await page.get_by_role("heading", name="Call from +14155551234").wait_for(timeout=30_000)
        for sel, what in (("[data-call-filler]", "the filler"), ("[data-call-cut]", "the barge-in cut"),
                          ("[data-call-pending]", "the waiting approval")):
            if not await page.locator(sel).count():
                raise AssertionError(f"the log does not show {what}")
        if not await page.get_by_text("Twilio, estimated").count() or not await page.get_by_text("lookup_order").count():
            raise AssertionError("the strip's estimate or the tool is missing")
        # Answered from the log itself (Naveen at review): the approval names its question, the result follows.
        await page.locator("[data-call-pending]").get_by_role("button", name="Approve").click()
        await page.get_by_text("Done: the refund is on its way.").wait_for(timeout=15_000)
        if answers != [{"approve": True, "approval_id": "ap_smoke_1"}]:
            raise AssertionError(f"the answer sent {answers}")
        await page.get_by_role("button", name="Back to Smoke support line").click()
        await page.locator(f'[data-call-row="{_PHONE_CALL["id"]}"]').wait_for(timeout=10_000)
        if "call=" in page.url:
            raise AssertionError("Back left the call in the address")
    finally:
        await ctx.close()
    return ("the deep link opens the call's log with filler, tool, cut, waiting approval and estimate; Approve there "
            "sends its id and the result follows; Back returns")


@check
async def check_phoneconnect(browser) -> str:
    """Add a number (V30): a key checked by listing its numbers, a number picked, an agent chosen; the number opens."""
    ctx = await _phone_context(browser, numbers=[])
    made = {"id": "chn_new", "name": "+14155550142", "platform": "twilio_voice", "status": "running", "enabled": True,
            "agent_ids": ["smoke_front"], "visitor_tools": [], "credentials": {},
            "voice": {"number": "+14155550142", "number_sid": "PN" + "1" * 32}}
    listed: list[list] = [[]]

    async def channels(route):
        if route.request.method == "POST":
            listed[0] = [made]
            return await route.fulfill(status=200, content_type="application/json", body=json.dumps(made))
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(listed[0]))
    await ctx.route("**/api/messaging/channels", channels)
    await ctx.route("**/api/messaging/twilio_voice/numbers", _answer({"numbers": [
        {"sid": "PN" + "1" * 32, "number": "+14155550142", "voice_url": "https://demo.twilio.com/welcome/voice/",
         "voice_application_sid": ""}]}, methods=("POST",)))
    await ctx.route("**/api/messaging/channels/chn_new/assignments", _answer({"ok": True}, methods=("PUT",)))
    await ctx.route("**/api/messaging/channels/chn_new/enable", _answer(made, methods=("POST",)))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/channels/phone", wait_until="domcontentloaded")
        await page.get_by_text("Let callers talk to your agents").wait_for(timeout=30_000)
        await page.locator("[data-phone-add]").click()
        await page.locator("#phone-account-sid").fill("AC" + "a" * 32)
        await page.locator("#phone-key-sid").fill("SK" + "b" * 32)
        await page.locator("#phone-key-secret").fill("smoke-secret")
        await page.get_by_role("button", name="Check and continue").click()
        await page.locator(f'[data-twilio-number="PN{"1" * 32}"]').wait_for(timeout=10_000)
        await page.get_by_role("button", name="Continue").click()
        await page.get_by_role("button", name="Agent", exact=True).click()
        await page.get_by_role("option", name="Smoke front desk").click()
        await page.get_by_role("button", name="Connect number").click()
        await page.get_by_role("heading", name="+14155550142").wait_for(timeout=10_000)
    finally:
        await ctx.close()
    return "three steps against stubbed Twilio; the new number opens"


@check
async def check_widgetconfirm(browser) -> str:
    """A widget's Delete and Issue a new key ask in the kit's dialog, and cancelling keeps the editor (M2h-2: they
    were native confirm()s, and a cancelled Delete still cleared the editor)."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    deleted = []
    widget = {"id": "w_smoke", "name": "Smoke widget", "agent_id": "a1", "public_key": "pk_smoke",
              "allowed_origins": ["https://example.com"], "enabled": True, "greeting": "Hi", "accent": "#2563eb",
              "title": "", "launcher_label": "", "daily_message_cap": 500, "font": "system", "theme": "auto",
              "header_style": "accent", "launcher": "bubble", "logo": "", "position": "right", "visitor_tools": []}

    async def widgets(route):
        if route.request.method == "GET":
            return await route.fulfill(json=[widget])
        await route.continue_()

    async def one(route):
        if route.request.method == "DELETE":
            deleted.append(route.request.url)
            return await route.fulfill(json={})
        await route.continue_()
    await ctx.route("**/api/widgets", widgets)
    await ctx.route("**/api/widgets/w_smoke", one)
    await ctx.route("**/api/widgets/visitor-tools*", _answer({"tools": []}))
    await ctx.route("**/embed/preview/**", _answer("<html></html>", content_type="text/html"))
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/widgets", wait_until="domcontentloaded")
        await page.get_by_role("heading", name="Smoke widget").wait_for(timeout=30_000)
        for name in ("Delete", "Issue a new key"):
            await page.get_by_role("button", name=name, exact=True).first.click()
            dialog = page.get_by_role("alertdialog").or_(page.get_by_role("dialog"))
            await dialog.first.wait_for(timeout=5_000)
            await dialog.first.get_by_role("button", name="Cancel").click()
            await asyncio.sleep(0.5)
            if not await page.get_by_role("heading", name="Smoke widget").count():
                raise AssertionError(f"cancelling {name} closed the editor")
        if deleted:
            raise AssertionError("a cancelled Delete still deleted")
    finally:
        await ctx.close()
    return "Delete and Issue a new key ask in a dialog; Cancel keeps the widget open"


@check
async def check_onetoaststack(browser) -> str:
    """A page's toast and a live notification's stack in one host, never one box over the other (F520).

    The kit `Toast` (bottom-20 right-5) and the notifications' stack (bottom-20 right-6) were two hosts in one
    corner: BF5's F518 check showed the page's red reason and the bell's toast drawn together, one cut off.
    """
    live = {"id": 900102, "ts": time.time(), "kind": "run_failed", "title": "Smoke flow failed",
            "body": "", "run_id": "run_smoke_stack", "data": {}, "resolved": False}
    tool = {"name": "uismoke_toast", "generalName": "Smoke toast", "description": "toast", "tool_type": "http",
            "url": "", "method": "GET", "inputSchema": {"type": "object", "properties": {}}}
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})

    async def listing(route):
        await route.fulfill(json={"notifications": [], "last_id": 0})

    async def stream(route):
        await route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                            body=f"data: {json.dumps(live)}\n\n")

    async def tools(route):
        if route.request.method != "GET":
            return await route.continue_()
        await route.fulfill(status=200, content_type="application/json", body=json.dumps([tool]))
    await ctx.route("**/api/notifications?after=0*", listing)
    await ctx.route("**/api/notifications/stream*", stream)
    await ctx.route("**/api/tools/custom", tools)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/tools", wait_until="domcontentloaded")
        await page.get_by_role("heading", name=re.compile(r"Editing: uismoke_toast")).wait_for(timeout=20_000)
        await page.get_by_text("Smoke flow failed").first.wait_for(timeout=10_000)
        await page.get_by_role("button", name="Save", exact=True).first.click()
        await page.get_by_text("Name and URL are required").first.wait_for(timeout=5_000)
        boxes = await page.evaluate("""() => ['Smoke flow failed', 'Name and URL are required'].map(t => {
            const el = [...document.querySelectorAll('.shadow-elevated')].find(e => e.innerText.includes(t));
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {host: !!el.closest('[data-toast-stack]'), top: r.top, bottom: r.bottom, left: r.left,
                    right: r.right};
        })""")
        if None in boxes:
            raise AssertionError(f"a toast is missing while the other shows: {boxes}")
        notice, mine = boxes
        if not (notice["host"] and mine["host"]):
            raise AssertionError(f"the two toasts are not in one host: {boxes}")
        apart = (notice["bottom"] <= mine["top"] or mine["bottom"] <= notice["top"]
                 or notice["right"] <= mine["left"] or mine["right"] <= notice["left"])
        if not apart:
            raise AssertionError(f"the page's toast and the notification overlap: {boxes}")
    finally:
        await ctx.close()
    return "a page's toast and a live notification stack in one host and never overlap"


@check
async def check_persontoast(browser) -> str:
    """A run that needs someone toasts on the person wash, not as a warning (M2h-2, UX1: amber is a person)."""
    live = {"id": 900101, "ts": time.time(), "kind": "human_input", "title": "Smoke flow needs your input",
            "body": "", "run_id": "run_smoke_person", "data": {}, "resolved": False}
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})

    async def listing(route):
        await route.fulfill(json={"notifications": [], "last_id": 0})

    async def stream(route):
        await route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                            body=f"data: {json.dumps(live)}\n\n")
    await ctx.route("**/api/notifications?after=0*", listing)
    await ctx.route("**/api/notifications/stream*", stream)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents", wait_until="domcontentloaded")
        toast = page.locator(TOASTS).filter(has_text="Smoke flow needs your input")
        await toast.first.wait_for(timeout=15_000)
        cls = await toast.first.get_attribute("class") or ""
        if "bg-person-subtle" not in cls:
            raise AssertionError(f"the toast is not on the person wash: {cls}")
    finally:
        await ctx.close()
    return "a run that needs someone toasts on the person wash"



# ── helpers: a lane, a follow-up, "Save as agent", the switch (SUB1a, D279) ────

def _helper_frames(hid: str, name: str) -> list[dict]:
    return [
        {"type": "helper_started", "helper_id": hid, "helper_name": name, "task": "check the 12 orders",
         "tools": ["lookup_order"], "mode": "wait"},
        {"type": "helper_event", "helper_id": hid, "helper_name": name,
         "event": {"type": "tool_execution", "tool_name": "lookup_order", "args": {"order": "HP-2041"}}},
        {"type": "helper_event", "helper_id": hid, "helper_name": name,
         "event": {"type": "tool_result", "tool_name": "lookup_order", "preview": "shipped day 12"}},
    ]


@check
async def check_helperlane(browser) -> str:
    """A helper's lane shows its work as it happens, and its Stop stops that helper alone (SUB1a, S7).

    A helper used to run invisibly: only its answer came back, after it ended.
    """
    history = {"turns": [], "pending_approval": None,
               "running": {"turn_id": "t1", "message": "check last week's orders", "started_at": 0, "cursor": "t1-0"}}
    ctx, page = await _chat_page(browser, "uismoke-helper-lane", history)
    try:
        await _stream_open(page, "events")
        frames = "".join(_frame(e, f"t1-{i + 1}") for i, e in enumerate(_helper_frames("subtask_lane", "30-day check")))
        await page.evaluate(f"() => window.__streams.events.push({json.dumps(frames)})")
        lane = page.get_by_role("region", name="Helper: 30-day check", exact=True)
        await lane.wait_for(timeout=10_000)
        if "working" not in (await lane.inner_text()).lower():
            raise AssertionError(f"the helper's line does not say it is working: {await lane.inner_text()!r}")
        if "shipped day 12" in await lane.inner_text():
            raise AssertionError("the helper's calls show before its line is opened (Naveen: closed, like Tools used)")
        await lane.locator("button[aria-expanded]").first.click()
        text = await lane.inner_text()
        if "lookup_order" not in text or "shipped day 12" not in text:
            raise AssertionError(f"the opened line does not show its call: {text!r}")
        await lane.get_by_role("button", name="Stop the helper 30-day check").click()
        for _ in range(30):
            calls = await page.evaluate("() => window.__calls")
            if any("/helpers/subtask_lane/stop" in c["url"] and c["method"] == "POST" for c in calls):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Stop did not ask the server to stop that helper")
        if any("/cancel" in c["url"] for c in calls):
            raise AssertionError("a helper's Stop stopped the whole turn")
        ended = {"type": "helper_finished", "helper_id": "subtask_lane", "helper_name": "30-day check",
                 "status": "cancelled", "seconds": 4,
                 "record": {"id": "subtask_lane", "name": "30-day check", "task": "check the 12 orders",
                            "tools": ["lookup_order"], "mode": "wait", "status": "cancelled", "seconds": 4,
                            "calls": [{"tool": "lookup_order", "args": {"order": "HP-2041"}, "result": "shipped day 12"}],
                            "answer": ""}}
        tail = _frame(ended, "t1-4") + _frame({"type": "response", "content": "I checked one before you stopped it."},
                                               "t1-5") + _frame({"type": "done"}, "t1-6")
        await page.evaluate(f"() => window.__streams.events.push({json.dumps(tail)})")
        if not await _on_screen(page, "I checked one before you stopped it."):
            raise AssertionError("the answer after the stopped helper never showed")
        kept = page.get_by_role("region", name="Helper: 30-day check", exact=True)
        if "stopped" not in (await kept.inner_text()).lower():
            raise AssertionError("the helper's line does not say it was stopped")
        if "What it was asked" not in await kept.inner_text():
            await kept.locator("button[aria-expanded]").first.click()
        if "What it was asked" not in await kept.inner_text():
            raise AssertionError("opening the helper's line does not show what it did")
    finally:
        await ctx.close()
    return "a helper is one closed line that opens on its calls, and its Stop asks for that helper alone"


@check
async def check_helperfollowup(browser) -> str:
    """A background helper's result reads as the helper's, never as the person's message (SUB1a, S2)."""
    lane = {"id": "subtask_bg", "name": "30-day check", "task": "check the 12 orders", "tools": ["lookup_order"],
            "mode": "background", "status": "completed", "seconds": 100, "calls": [], "answer": "3 of the 12 are late.",
            "session_id": "helperc_smoke"}
    history = {"turns": [
        {"user": "check last week's orders", "assistant": "I've sent a helper; I'll come back with the late ones.",
         "tools": [], "timestamp": "", "helpers": [lane]},
        {"user": "3 of the 12 are late.", "assistant": "Three orders are late: HP-2044, HP-2049, HP-2051.",
         "tools": [], "timestamp": "",
         "automated": {"kind": "helper_result", "helper_id": "subtask_bg", "name": "30-day check", "status": "completed"}},
    ], "pending_approval": None, "running": None}
    ctx, page = await _chat_page(browser, "uismoke-helper-follow", history)
    try:
        note = page.get_by_role("region", name="From your helper: 30-day check")
        await note.wait_for(timeout=15_000)
        if "From your helper · 30-day check" not in await note.inner_text():
            raise AssertionError("the follow-up does not say which helper it is from")
        if not await _on_screen(page, "Three orders are late"):
            raise AssertionError("the agent's follow-up answer is not shown")
        if "3 of the 12 are late." in await page.evaluate("() => document.body.innerText"):
            raise AssertionError("the helper's result was drawn as a message, not under its note")
        if await note.get_by_role("button", name="Edit this message and send it again").count():
            raise AssertionError("a helper's result offers Edit as if the person had written it")
    finally:
        await ctx.close()
    return "a background helper's result is drawn under \"From your helper\", never as the person's message"


@check
async def check_helperstogether(browser) -> str:
    """Helpers that finish close together come back as one follow-up, which names them all (SUB1a, Naveen)."""
    lanes = [{"id": hid, "name": name, "task": "check stock", "tools": ["lookup_order"], "mode": "background",
              "status": "completed", "seconds": 4, "calls": [], "answer": "fine", "session_id": f"helperc_{hid}"}
             for hid, name in (("subtask_s11", "Stock: 11"), ("subtask_s12", "Stock: 12"))]
    history = {"turns": [
        {"user": "how are the new SKUs?", "assistant": "I've sent two helpers.", "tools": [], "timestamp": "",
         "helpers": lanes},
        {"user": "From your helper 'Stock: 11' (it finished):\n41 in stock.\n\nFrom your helper 'Stock: 12' "
                 "(it finished):\nout, 120 on order.",
         "assistant": "SKU-11 has 41 in stock; SKU-12 is out, with 120 on order.", "tools": [], "timestamp": "",
         "automated": {"kind": "helper_result", "helper_id": "subtask_s11", "name": "Stock: 11",
                       "status": "completed", "helper_ids": ["subtask_s11", "subtask_s12"],
                       "names": ["Stock: 11", "Stock: 12"], "statuses": ["completed", "completed"]}},
    ], "pending_approval": None, "running": None}
    ctx, page = await _chat_page(browser, "uismoke-helpers-together", history)
    try:
        note = page.get_by_role("region", name="From your helpers: Stock: 11 and Stock: 12")
        await note.wait_for(timeout=15_000)
        if "From your helpers · Stock: 11 and Stock: 12" not in await note.inner_text():
            raise AssertionError(f"the follow-up does not name both helpers: {await note.inner_text()!r}")
        if not await _on_screen(page, "SKU-11 has 41 in stock; SKU-12 is out"):
            raise AssertionError("the agent's one reply to both is not shown")
        if "out, 120 on order." in await page.evaluate("() => document.body.innerText"):
            raise AssertionError("the helpers' results were drawn as a message, not under their note")
    finally:
        await ctx.close()
    return "two helpers' results that came back together are one follow-up, \"From your helpers · A and B\""


@check
async def check_helpertoolsrow(browser) -> str:
    """A helper is its lane, not a `spawn_subtask` row too (F572): the tools-used row leaves out each send that
    drew a lane, reopened and live; a refused send stays, as "not run".

    SUB1b's dev shots read the lane, then "1 tool · spawn_subtask", which said nothing the lane did not.
    """
    lane = {"id": "subtask_row", "name": "Capital", "task": "find the capital", "tools": [], "mode": "wait",
            "status": "completed", "seconds": 3, "calls": [], "answer": "Paris."}
    calls = [{"tool": "spawn_subtask", "args": {"task": "find the capital"}, "result": "Paris."},
             {"tool": "lookup_order", "args": {"order": "HP-2041"}, "result": "shipped day 12"},
             {"tool": "spawn_subtask", "args": {"task": "again"}, "result": "", "refused": True,
              "reason": "a helper with that brief is already running"}]
    history = {"turns": [{"user": "what is the capital?", "assistant": "Paris, and HP-2041 shipped.", "tools": [],
                          "timestamp": "", "helpers": [lane], "calls": calls}],
               "pending_approval": None, "running": None}
    ctx, page = await _chat_page(browser, "uismoke-helper-tools-row", history)
    try:
        if not await _on_screen(page, "Paris, and HP-2041 shipped."):
            raise AssertionError("the reopened answer never showed")
        row = page.get_by_role("button", name=re.compile(r"Tools used"))
        label = (await row.first.inner_text()).strip()
        if "Tools used · 2" not in label or "1 not run" not in label:
            raise AssertionError(f"the reopened row does not count the lookup and the refused send alone: {label!r}")
        await row.first.click()
        await page.get_by_text("lookup_order", exact=True).first.wait_for(timeout=5_000)
        # The list the row opens on; the refused send is also under the answer's "Not run" (U68).
        listed = await page.locator("ul").filter(has_text="lookup_order").first.inner_text()
        if listed.count("spawn_subtask") != 1 or "already running" not in listed:
            raise AssertionError(f"the reopened row lists the send its lane shows, or drops the refused one: "
                                 f"{listed!r}")
    finally:
        await ctx.close()

    history = {"turns": [], "pending_approval": None,
               "running": {"turn_id": "t1", "message": "what is the capital?", "started_at": 0, "cursor": "t1-0"}}
    ctx, page = await _chat_page(browser, "uismoke-helper-tools-live", history)
    try:
        await _stream_open(page, "events")
        record = {**lane, "calls": []}
        events = [{"type": "tool_execution", "tool_name": "spawn_subtask", "args": {"task": "find the capital"}},
                  {"type": "helper_started", "helper_id": "subtask_row", "helper_name": "Capital",
                   "task": "find the capital", "tools": [], "mode": "wait"},
                  {"type": "helper_finished", "helper_id": "subtask_row", "helper_name": "Capital",
                   "status": "completed", "seconds": 3, "record": record},
                  {"type": "tool_result", "tool_name": "spawn_subtask", "preview": "Paris."},
                  {"type": "response", "content": "The capital is Paris."}, {"type": "done"}]
        frames = "".join(_frame(e, f"t1-{i + 1}") for i, e in enumerate(events))
        await page.evaluate(f"() => window.__streams.events.push({json.dumps(frames)})")
        if not await _on_screen(page, "The capital is Paris."):
            raise AssertionError("the live answer never showed")
        await page.get_by_role("region", name="Helper: Capital", exact=True).wait_for(timeout=10_000)
        text = await page.evaluate("() => document.body.innerText")
        if "spawn_subtask" in text or "Tools used" in text:
            raise AssertionError("the live turn lists its helper's send beside the helper's lane")
    finally:
        await ctx.close()
    return "a helper is its lane alone: no spawn_subtask in Tools used, reopened or live; a refused send stays"


@check
async def check_helpersave(browser) -> str:
    """"Save as agent" opens the agent editor with the helper made into a new agent, and saves nothing (S9)."""
    lane = {"id": "subtask_save", "name": "Order checker", "task": "Check each order against the 30-day rule.",
            "tools": [], "mode": "wait", "status": "completed", "seconds": 12, "calls": [], "answer": "done"}
    history = {"turns": [{"user": "check them", "assistant": "Checked.", "tools": [], "timestamp": "",
                          "helpers": [lane]}], "pending_approval": None, "running": None}
    posts = r"""
window.__agentWrites = [];
const _f2 = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || 'GET').toUpperCase();
  if (url.includes('/api/agents') && method !== 'GET') window.__agentWrites.push({url: url, method: method});
  return _f2(input, init);
};
"""
    ctx, page = await _chat_page(browser, "uismoke-helper-save", history, extra_init=posts)
    try:
        region = page.get_by_role("region", name="Helper: Order checker", exact=True)
        await region.wait_for(timeout=15_000)
        await region.locator("button[aria-expanded]").first.click()
        await region.get_by_role("button", name="Save as agent").click()
        await page.wait_for_url("**/agents**", timeout=20_000)
        await page.wait_for_function(
            "() => [...document.querySelectorAll('input')].some(i => i.value === 'Order checker')", timeout=20_000)
        body = await page.evaluate("() => document.body.innerText")
        boxes = await page.evaluate("() => [...document.querySelectorAll('textarea')].map(t => t.value).join(' ')")
        if "Check each order against the 30-day rule." not in body + boxes:
            raise AssertionError("the new agent does not carry the helper's brief as its instructions")
        if "not saved yet" not in body.lower():
            raise AssertionError("the agent made from the helper does not read as unsaved")
        writes = await page.evaluate("() => window.__agentWrites")
        if writes:
            raise AssertionError(f"opening it wrote an agent: {writes}")
    finally:
        await ctx.close()
    return "Save as agent opens the editor on a new, unsaved agent with the helper's name and brief"


@check
async def check_helperswitch(browser) -> str:
    """An agent's "Can send helpers" switch, its "Helper model", and no helper row in the Tools grid (SUB1a, S1)."""
    aid = f"agent_uismoke_helpers_{int(time.time())}"
    name = f"Helpers {aid[-6:]}"
    api("/api/agents", "POST", {**_agent(aid, name), "tools": ["all"]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents?id={aid}", wait_until="domcontentloaded")
        await page.wait_for_function(
            "(n) => [...document.querySelectorAll('input')].some(i => i.value === n)", arg=name, timeout=30_000)
        await asyncio.sleep(1.0)
        tools = page.locator('section[aria-labelledby="setup-tools"]')
        if await tools.get_by_text("spawn_subtask").count() or await tools.get_by_text("Sub-Agent").count():
            raise AssertionError("the Tools grid still has a row for helpers")
        switch = page.get_by_role("switch", name="Can send helpers", exact=True)
        if await switch.get_attribute("aria-checked") != "false":
            raise AssertionError("an agent with every tool reads as sending helpers")
        await switch.click()
        await page.locator('[aria-label="Helper model"]').first.wait_for(timeout=5_000)
        await page.get_by_role("button", name="Save agent").click()
        saved: dict = {}
        for _ in range(40):
            saved = next((a for a in api("/api/agents") if a.get("id") == aid), {})
            if "spawn_subtask" in (saved.get("tools") or []):
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"Save did not keep the switch: {saved.get('tools')}")
        if "all" not in saved["tools"]:
            raise AssertionError(f"switching helpers on took away every tool: {saved['tools']}")
        await asyncio.sleep(1.0)
        if "Nothing here provides spawn_subtask" in await page.evaluate("() => document.body.innerText"):
            raise AssertionError("the agent page says nothing provides its helpers (F549)")
    finally:
        await ctx.close()
        try:
            api(f"/api/agents/{aid}", "DELETE")
        except Exception:
            pass
    return "Can send helpers turns helpers on beside every tool and shows Helper model; the grid has no helper row"


# ── a Team agent's screens (TEAM1b, D286) ────────────────────────────────────

#: A team's member and lead routes answered as the backend does when the member or lead read the words.
TEAM_ROUTES_STUB = r"""
const _teamFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
  if (/\/api\/chat\/[^/?]+\/(members\/[^/?]+\/(stop|message)|lead\/message)$/.test(url)) {
    window.__calls.push({url: url, method: method, body: (init && init.body) || ''});
    return Promise.resolve(new Response(JSON.stringify({stopped: true, delivered: true}), {
      status: 200, headers: {'Content-Type': 'application/json'}}));
  }
  if (/\/api\/chat\/[^/?]+\/approve$/.test(url)) {
    window.__calls.push({url: url, method: method, body: (init && init.body) || ''});
    // The answer resumes the turn: a stream this script feeds, as the chat stub's own are.
    const body = new ReadableStream({start(c) {
      window.__streams.approve = {push: (frame) => c.enqueue(new TextEncoder().encode(frame)),
                                  close: () => { try { c.close(); } catch (e) {} }};
    }});
    return Promise.resolve(new Response(body, {status: 200, headers: {'Content-Type': 'text/event-stream'}}));
  }
  return _teamFetch(input, init);
};
"""

#: The run page's team routes (TEAM1e), recorded and answered: a member's Stop and Message, the lead's message, and
#: the run's own cancel, which a member's Stop must never call.
RUN_TEAM_ROUTES_STUB = r"""
window.__calls = [];
const _runTeamFetch = window.fetch;
window.fetch = function (input, init) {
  const url = String((input && input.url) || input || '');
  const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
  if (/\/api\/orchestrations\/runs\/[^?]*\/(members\/[^/?]+\/(stop|message)|lead\/message|cancel)$/.test(url)) {
    window.__calls.push({url: url, method: method, body: (init && init.body) || ''});
    return Promise.resolve(new Response(JSON.stringify({stopped: true, delivered: true}), {
      status: 200, headers: {'Content-Type': 'application/json'}}));
  }
  return _runTeamFetch(input, init);
};
"""

ANA = {"member_id": "m_ana", "member_name": "Supplier analyst"}
BEN = {"member_id": "m_ben", "member_name": "Invoice clerk"}
TEAM_SUMMARY = {"tasks": [
    {"card_id": "c_ana", "title": "Late deliveries in week 41", "member": "Supplier analyst", "member_id": "m_ana",
     "status": "done", "result": "2 late"},
    {"card_id": "c_ben", "title": "Invoice mismatches", "member": "Invoice clerk", "member_id": "m_ben",
     "status": "done", "result": "1 mismatch"}], "tasks_used": 2, "answered_alone": False, "spent_usd": 0.0312}
ANA_RECORD = {"id": "m_ana", "agent_id": "analyst", "name": "Supplier analyst", "task": "Late deliveries in week 41",
              "card_id": "c_ana", "model": "synapse.nova-pro", "status": "completed", "seconds": 41,
              "calls": [{"tool": "late_deliveries", "args": {"week": 41}, "result": "2 late"}],
              "answer": "Two suppliers were late: Fenwick Paper and Corda Inks."}
BEN_RECORD = {"id": "m_ben", "agent_id": "clerk", "name": "Invoice clerk", "task": "Invoice mismatches",
              "card_id": "c_ben", "model": "synapse.nova-pro", "status": "completed", "seconds": 52,
              "calls": [{"tool": "match_invoices", "args": {"week": 41}, "result": "48 matched"}],
              "answer": "INV-7731 is billed for 120 reams; 100 were received."}
HOLD = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "hold_payment",
        "tool_args": {"invoice": "INV-7731"}, "category": "destructive", "approval_id": "appr_ben",
        "prompt": "Hold payment on INV-7731?",
        "fields": [{"name": "approve", "type": "boolean"}, {"name": "reason", "type": "text"}]}


def _team_frames() -> list[dict]:
    """A lead with two members at work: the task list, their calls, a stream, a message each way."""
    wrap = lambda who, ev: {"type": "member_event", **who, "event": ev}  # noqa: E731
    return [
        {"type": "tool_execution", "tool_name": "assign", "args": {"member": "Supplier analyst", "task": "Late deliveries"}},
        {"type": "member_started", **ANA, "agent_id": "analyst", "task": "Late deliveries in week 41",
         "card_id": "c_ana", "model": "synapse.nova-pro"},
        {"type": "member_started", **BEN, "agent_id": "clerk", "task": "Invoice mismatches",
         "card_id": "c_ben", "model": "synapse.nova-pro"},
        {"type": "team_tasks", "tasks": [
            {"card_id": "c_ana", "title": "Late deliveries in week 41", "member": "Supplier analyst",
             "member_id": "m_ana", "status": "claimed", "result": ""},
            {"card_id": "c_ben", "title": "Invoice mismatches", "member": "Invoice clerk", "member_id": "m_ben",
             "status": "claimed", "result": ""}]},
        wrap(ANA, {"type": "tool_execution", "tool_name": "late_deliveries", "args": {"week": 41}}),
        wrap(ANA, {"type": "tool_result", "tool_name": "late_deliveries", "preview": "2 late"}),
        wrap(ANA, {"type": "answer_delta", "text": "Two suppliers were late"}),
        wrap(BEN, {"type": "tool_execution", "tool_name": "message",
                   "args": {"to": "lead", "text": "Count back-orders as open?"}}),
        {"type": "tool_execution", "tool_name": "message", "args": {"to": "Invoice clerk", "text": "Yes, separately."}},
    ]


async def _push(page, stream: str, events: list[dict], start: int = 1) -> int:
    frames = "".join(_frame(e, f"t1-{start + i}") for i, e in enumerate(events))
    await page.evaluate(f"() => window.__streams[{json.dumps(stream)}].push({json.dumps(frames)})")
    return start + len(events)


async def _called(page, pattern: str, tries: int = 30) -> dict | None:
    for _ in range(tries):
        calls = await page.evaluate("() => window.__calls")
        hit = next((c for c in calls if re.search(pattern, c["url"])), None)
        if hit:
            return hit
        await asyncio.sleep(0.1)
    return None


@check
async def check_teameditor(browser) -> str:
    """A delegate reads as a Team agent: Members, how they work, its own tools; an old one is not rewritten (TEAM1b).

    "Sub-Agents (Delegation Targets)" became Members (Q11); a Team agent gets "How members work", the
    number at once under this install's ceiling, a task budget and an optional spend limit (Q1, Q2a, Q8),
    and the Tools grid the editor used to hide (Q1b). A delegate saved before teams has no `team` block:
    it opens as "One at a time" and is not made dirty by opening (D204).
    """
    stamp = int(time.time())
    lead, member = f"agent_uismoke_team_{stamp}", f"agent_uismoke_member_{stamp}"
    name = f"Team {str(stamp)[-6:]}"
    api("/api/agents", "POST", _agent(member, f"Member {str(stamp)[-6:]}"))
    api("/api/agents", "POST", {**_agent(lead, name), "type": "delegate", "delegate_agent_ids": [member]})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/agents?id={lead}", wait_until="domcontentloaded")
        await page.wait_for_function(
            "(n) => [...document.querySelectorAll('input')].some(i => i.value === n)", arg=name, timeout=30_000)
        await asyncio.sleep(1.0)
        text = await page.evaluate("() => document.body.innerText")
        if "Sub-Agents" in text or "Delegation Targets" in text:
            raise AssertionError("the editor still says Sub-Agents (Delegation Targets)")
        members = page.locator('section[aria-labelledby="setup-members"]')
        if not await members.count():
            raise AssertionError("a Team agent has no Members section")
        if not await page.locator('section[aria-labelledby="setup-tools"]').count():
            raise AssertionError("a Team agent's own tool picker is hidden (Q1b)")
        one = members.get_by_role("radio", name=re.compile("One at a time"))
        if not await one.is_checked():
            raise AssertionError("a delegate saved before teams does not read as one at a time")
        if await page.get_by_role("button", name="Save agent").count():
            raise AssertionError("opening an old delegate made it dirty")
        if await members.get_by_label("Members working at once").count():
            raise AssertionError("one at a time shows the team's numbers")

        await members.get_by_role("radio", name=re.compile("As a team")).check()
        at_once = members.get_by_label("Members working at once")
        await at_once.wait_for(timeout=5_000)
        hint = await members.inner_text()
        if "on this install" not in hint:
            raise AssertionError(f"the at-once field does not say where its ceiling comes from: {hint!r}")
        await members.get_by_label("Spend limit per turn").fill("0.5")
        await members.get_by_label("Spend limit per turn").press("Tab")
        await page.get_by_role("button", name="Save agent").click()
        saved: dict = {}
        for _ in range(40):
            saved = next((a for a in api("/api/agents") if a.get("id") == lead), {})
            if (saved.get("team") or {}).get("mode") == "team":
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"Save did not keep the team: {saved.get('team')}")
        want = {"mode": "team", "at_once": 3, "task_budget": 6, "spend_limit_usd": 0.5}
        if saved["team"] != want:
            raise AssertionError(f"saved {saved['team']}, wanted {want}")
        if saved.get("delegate_agent_ids") != [member]:
            raise AssertionError(f"the members changed on save: {saved.get('delegate_agent_ids')}")

        # A new agent typed Team starts as a team (Q1).
        await page.get_by_role("button", name="Create New Agent", exact=True).click()
        await asyncio.sleep(1.0)
        await page.get_by_role("combobox", name="Agent type").click()
        await page.get_by_role("option", name="Team", exact=True).click()
        await asyncio.sleep(0.5)
        if not await page.get_by_role("radio", name=re.compile("As a team")).is_checked():
            raise AssertionError("a new Team agent does not start as a team")
    finally:
        await ctx.close()
        for aid in (lead, member):
            try:
                api(f"/api/agents/{aid}", "DELETE")
            except Exception:
                pass
    return "a delegate reads as a Team agent with Members and its own tools; an old one opens one at a time, unchanged"


@check
async def check_teamlanes(browser) -> str:
    """A live team: its task list, each member a line with its time that opens, Message and Stop for one member, "To" and "Stop the team", and a member that needs the person opening by itself (TEAM1b, Q5; Naveen: lines, not columns)."""
    history = {"turns": [], "pending_approval": None,
               "running": {"turn_id": "t1", "message": "Run the Monday supplier review", "started_at": 0,
                           "cursor": "t1-0"}}
    ctx, page = await _chat_page(browser, "uismoke-team-lanes", history, extra_init=TEAM_ROUTES_STUB)
    try:
        await _stream_open(page, "events")
        at = await _push(page, "events", _team_frames())
        ana = page.get_by_role("region", name="Member: Supplier analyst", exact=True)
        ben = page.get_by_role("region", name="Member: Invoice clerk", exact=True)
        await ana.wait_for(timeout=10_000)
        if "Two suppliers were late" in await ana.inner_text():
            raise AssertionError("a working member opens by itself; it should be one line until asked")
        if not re.search(r"\d+ s", await ana.inner_text()):
            raise AssertionError(f"a working member's line does not count its time: {await ana.inner_text()!r}")
        await ana.get_by_role("button", name=re.compile("^Supplier analyst")).click()
        await asyncio.sleep(0.3)
        if "Two suppliers were late" not in await ana.inner_text():
            raise AssertionError("a member's answer does not stream in its lane")
        if "late_deliveries" not in await ana.inner_text():
            raise AssertionError("a member's call is not in its lane")
        tasks = page.get_by_role("region", name="Task list", exact=True)
        if "Invoice mismatches" not in await tasks.inner_text():
            raise AssertionError("the task list does not show the members' tasks")
        notes = await page.get_by_role("list", name="Team messages").inner_text()
        if "Invoice clerk to the lead: Count back-orders as open?" not in notes or "Lead to Invoice clerk" not in notes:
            raise AssertionError(f"the exchanges are not one-line notes: {notes!r}")

        await ana.get_by_role("button", name="Stop Supplier analyst").click()
        if not await _called(page, r"/members/m_ana/stop$"):
            raise AssertionError("a lane's Stop did not ask for that member alone")
        if await _called(page, r"/cancel$", tries=3):
            raise AssertionError("a member's Stop stopped the whole team")

        await ben.get_by_role("button", name=re.compile("^Invoice clerk")).click()
        await ben.get_by_role("button", name="Message Invoice clerk").click()
        await ben.get_by_label("Your message to Invoice clerk").fill("List them separately")
        await ben.get_by_label("Your message to Invoice clerk").press("Enter")
        sent = await _called(page, r"/members/m_ben/message$")
        if not sent or "List them separately" not in sent["body"]:
            raise AssertionError(f"a lane's Message did not reach that member: {sent}")

        to = page.get_by_label("Send to", exact=True)
        if await to.input_value() != "lead":
            raise AssertionError("the composer does not start addressed to the lead")
        await page.get_by_label(re.compile("^Message ")).last.fill("Keep the note short")
        await page.get_by_role("button", name="Send to the lead").click()
        if not await _called(page, r"/lead/message$"):
            raise AssertionError("To: the lead did not reach the lead")
        if not await page.get_by_role("button", name="Stop the team").count():
            raise AssertionError("the composer's Stop does not say it stops the team")

        # A member's question while the others work: in its lane, marked, not yet answerable here (TEAM1c).
        at = await _push(page, "events", [{"type": "member_event", **BEN, "event": HOLD}], at)
        await asyncio.sleep(0.5)
        if "Permission needed" not in await ben.inner_text() or "needs you" not in await ben.inner_text():
            raise AssertionError("a member's question does not show in its lane, opened")
        if not await ben.get_by_role("button", name="Allow once").count():
            raise AssertionError("a live member's question cannot be answered in its lane (TEAM1c)")

        ended = [{"type": "member_finished", **ANA, "status": "completed", "seconds": 41, "record": ANA_RECORD},
                 {"type": "member_finished", **BEN, "status": "completed", "seconds": 52, "record": BEN_RECORD},
                 {"type": "response", "content": "The Monday note is ready.", "team": TEAM_SUMMARY,
                  "members": [ANA_RECORD, BEN_RECORD]},
                 {"type": "done"}]
        await _push(page, "events", ended, at)
        if not await _on_screen(page, "The Monday note is ready."):
            raise AssertionError("the lead's answer never showed")
        line = await page.get_by_label("This turn's team").inner_text()
        if "2 members" not in line or "$0.03" not in line:
            raise AssertionError(f"the turn's team line does not say what it took: {line!r}")
        kept = page.get_by_role("region", name="Member: Supplier analyst", exact=True)
        if "done · 41 s" not in await kept.inner_text():
            raise AssertionError("a finished member is not one line saying how it ended")
    finally:
        await ctx.close()
    return "a live team: tasks, each member a line with its time that opens, Stop and Message for one member, To the lead, Stop the team"


@check
async def check_teamkept(browser) -> str:
    """A reopened Team agent's turn: its task list, each member one line that opens, and "answered alone" (TEAM1b)."""
    history = {"turns": [
        {"user": "Run the Monday supplier review", "assistant": "The Monday note is ready.",
         "members": [ANA_RECORD, BEN_RECORD], "team": TEAM_SUMMARY,
         "calls": [{"tool": "assign", "args": {}, "result": "Assigned"}, {"tool": "lookup", "args": {}, "result": "ok"}]},
        {"user": "Thanks, what day is it?", "assistant": "Monday.",
         "team": {"tasks": [], "tasks_used": 0, "answered_alone": True, "spent_usd": 0.001}, "members": []},
    ], "pending_approval": None, "running": None}
    ctx, page = await _chat_page(browser, "uismoke-team-kept", history)
    try:
        if not await _on_screen(page, "The Monday note is ready."):
            raise AssertionError("the reopened turn never showed")
        ana = page.get_by_role("region", name="Member: Supplier analyst", exact=True)
        if "Two suppliers were late" in await ana.inner_text():
            raise AssertionError("a kept member is shown open; it should be one line")
        await ana.get_by_role("button").first.click()
        await asyncio.sleep(0.3)
        if "Two suppliers were late" not in await ana.inner_text():
            raise AssertionError("opening a kept member does not show its answer")
        if "late_deliveries" not in await ana.inner_text():
            raise AssertionError("opening a kept member does not show its calls")
        if "Invoice mismatches" not in await page.get_by_role("region", name="Task list").first.inner_text():
            raise AssertionError("a kept turn lost its task list")
        lines = await page.get_by_label("This turn's team").all_inner_texts()
        if not any("Answered alone" in t for t in lines):
            raise AssertionError(f"a lead that answered alone does not say so: {lines}")
        tools = await page.evaluate("() => document.body.innerText")
        if "Tools used · 2" in tools:
            raise AssertionError("the lead's assign is counted among Tools used beside its lanes")
    finally:
        await ctx.close()
    return "a reopened team turn: tasks, members one line each that open, and 'answered alone'"


@check
async def check_teamparked(browser) -> str:
    """A turn paused on a member's question asks it in that member's lane, once, and its answer resumes the team (TEAM1b, Q6 interim)."""
    history = {"turns": [], "pending_approval": None,
               "running": {"turn_id": "t1", "message": "Run the Monday supplier review", "started_at": 0,
                           "cursor": "t1-0"}}
    ctx, page = await _chat_page(browser, "uismoke-team-parked", history, extra_init=TEAM_ROUTES_STUB)
    try:
        await _stream_open(page, "events")
        at = await _push(page, "events", _team_frames())
        parked = {**HOLD, "member_id": "m_ben", "delegate_from": "lead", "delegate_agent_name": "Invoice clerk"}
        await _push(page, "events", [{"type": "member_finished", **ANA, "status": "completed", "seconds": 41,
                                      "record": ANA_RECORD}, parked, {"type": "done"}], at)
        ben = page.get_by_role("region", name="Member: Invoice clerk", exact=True)
        allow = ben.get_by_role("button", name="Allow once")
        await allow.wait_for(timeout=10_000)
        if await page.get_by_text("Permission needed").count() != 1:
            raise AssertionError("the member's question is asked twice: in its lane and as the chat's card")
        await allow.click()
        sent = await _called(page, r"/approve$")
        if not sent or '"approval_id":"appr_ben"' not in sent["body"].replace(" ", ""):
            raise AssertionError(f"Allow once did not answer that question: {sent}")
        await _stream_open(page, "approve")
        resumed = [{"type": "member_event", **BEN, "event": {"type": "tool_execution", "tool_name": "hold_payment",
                                                              "args": {"invoice": "INV-7731"}}},
                   {"type": "member_event", **BEN, "event": {"type": "tool_result", "tool_name": "hold_payment",
                                                              "preview": "held"}},
                   {"type": "member_finished", **BEN, "status": "completed", "seconds": 52, "record": BEN_RECORD},
                   {"type": "response", "content": "Payment held; the Monday note is ready.", "team": TEAM_SUMMARY,
                    "members": [ANA_RECORD, BEN_RECORD]},
                   {"type": "done"}]
        await _push(page, "approve", resumed)
        if not await _on_screen(page, "Payment held; the Monday note is ready."):
            raise AssertionError("the resumed team's answer never showed")
        regions = await page.get_by_role("region", name="Member: Invoice clerk", exact=True).count()
        if regions != 1:
            raise AssertionError(f"the resumed team drew the clerk {regions} times")
    finally:
        await ctx.close()
    return "a paused member's question is asked once, in its lane, and its answer resumes the team"


@check
async def check_teamrun(browser) -> str:
    """The run page draws a Team step's task list and lanes from its events, and each lane's Stop and Message reach
    that member of this run alone (TEAM1b; fed and given routes in TEAM1e, D290; F1013)."""
    oid = f"orch_uismoke_teamrun_{int(time.time())}"
    rid = f"run_uismoke_teamrun_{int(time.time())}"
    api("/api/orchestrations", "POST", human_gate_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(RUNLOG_STUB)
    await ctx.add_init_script(RUN_TEAM_ROUTES_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run now", exact=True).click()
        for _ in range(30):
            if await page.evaluate("() => !!window.__run.raw"):
                break
            await asyncio.sleep(0.1)
        # The engine's first event names the run: what a lane's Stop and Message address.
        start = {"type": "orchestration_start", "run_id": rid, "orchestration_id": oid,
                 "orchestration_name": "UI smoke team run"}
        for event in [start, *_team_frames()]:
            await page.evaluate(f"() => window.__run.raw({json.dumps(event)})")
        lane = page.get_by_role("region", name="Member: Supplier analyst", exact=True)
        await lane.wait_for(timeout=10_000)
        await lane.get_by_role("button", name=re.compile("^Supplier analyst")).click()
        await asyncio.sleep(0.3)
        if "late_deliveries" not in await lane.inner_text():
            raise AssertionError("the run page's lane does not show the member's call")
        log = await page.evaluate("() => (document.querySelector('[data-run-log]') || {}).innerText || ''")
        if "late_deliveries" in log:
            raise AssertionError("a member's call is written into the step's log as the step's own")

        run_path = rf"/api/orchestrations/runs/{re.escape(rid)}"
        await lane.get_by_role("button", name="Stop Supplier analyst").click()
        if not await _called(page, rf"{run_path}/members/m_ana/stop$"):
            raise AssertionError(f"a lane's Stop did not ask this run for that member alone: "
                                 f"{await page.evaluate('() => window.__calls')}")
        if await _called(page, r"/cancel$", tries=3):
            raise AssertionError("a member's Stop cancelled the whole run")

        ben = page.get_by_role("region", name="Member: Invoice clerk", exact=True)
        await ben.get_by_role("button", name=re.compile("^Invoice clerk")).click()
        await ben.get_by_role("button", name="Message Invoice clerk").click()
        await ben.get_by_label("Your message to Invoice clerk").fill("List them separately")
        await ben.get_by_label("Your message to Invoice clerk").press("Enter")
        sent = await _called(page, rf"{run_path}/members/m_ben/message$")
        if not sent or "List them separately" not in sent["body"]:
            raise AssertionError(f"a lane's Message did not reach that member of this run: {sent}")
    finally:
        await ctx.close()
        drop_fixture(oid)
    return "the run page draws a team's tasks and lanes from its events; a lane's Stop and Message reach that member of this run alone"


@check
async def check_runstateleak(browser) -> str:
    """Another workflow opened while a run streams is not marked as running (F1026; TABS1, D319).

    Switching workflows kept the earlier run's stream, its run id and its question: the
    next event from that run put "running" and Stop on the workflow just opened, whose
    own Run now was gone. A workflow's run state belongs to that workflow.
    """
    stamp = int(time.time())
    a, b = f"orch_uismoke_leak_a_{stamp}", f"orch_uismoke_leak_b_{stamp}"
    api("/api/orchestrations", "POST", {**human_gate_orchestration(a), "name": "ui-smoke leak A"})
    api("/api/orchestrations", "POST", {**human_gate_orchestration(b), "name": "ui-smoke leak B"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(RUNLOG_STUB)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={a}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run now", exact=True).click()
        for _ in range(30):
            if await page.evaluate("() => !!window.__run.raw"):
                break
            await asyncio.sleep(0.1)
        await page.evaluate(f"""() => window.__run.raw({{type: 'step_start', orch_step_id: '{a}_p0',
            step_name: 'Line 1', step_type: 'print'}})""")
        await asyncio.sleep(0.5)
        # Open B the way the editor offers: its workflow switcher.
        await page.get_by_role("button", name="Workflow", exact=True).click()
        await page.get_by_role("option", name="ui-smoke leak B").click()
        await asyncio.sleep(1.0)
        # A's run goes on, and says so.
        await page.evaluate(f"""() => window.__run.raw({{type: 'step_start', orch_step_id: '{a}_p1',
            step_name: 'Line 2', step_type: 'print'}})""")
        await asyncio.sleep(1.0)
        stop = await page.get_by_role("button", name="Stop", exact=True).count()
        run_now = await page.get_by_role("button", name="Run now", exact=True).count()
    finally:
        await ctx.close()
        drop_fixture(a)
        drop_fixture(b)
    if stop or not run_now:
        raise AssertionError("an earlier workflow's run marked the workflow opened after it as running")
    return "a run streaming in one workflow leaves the next workflow opened idle, with its own Run now"


# ── workflow and run tabs (TABS1 v2, D319) ──────────────────────────────────

def _wf_strip(page):
    return page.get_by_role("tablist", name="Open workflows")


async def _wf_titles(page) -> list[str]:
    return [(await t.get_attribute("aria-label")) or "" for t in await _wf_strip(page).get_by_role("tab").all()]


async def _wf_canvas(page, timeout: int = 30_000) -> None:
    await page.locator(".react-flow__node").first.wait_for(timeout=timeout)
    await asyncio.sleep(1.0)


async def _wf_open_from_picker(page, name: str) -> None:
    await page.get_by_role("button", name="Open a workflow in a new tab").first.click()
    await page.get_by_role("option", name=re.compile(re.escape(name))).first.click()
    await asyncio.sleep(1.0)


def _start_real_run(oid: str) -> str:
    """Start a model-free run (print steps, then a person's step) and return its id."""
    req = urllib.request.Request(
        f"{API}/api/orchestrations/{oid}/run", method="POST", data=json.dumps({"message": ""}).encode(),
        headers={"Content-Type": "application/json", "X-Synapse-Internal": TOKEN})
    with urllib.request.urlopen(req, timeout=60) as r:
        for raw in r.read().decode().splitlines():
            if raw.startswith("data: "):
                event = json.loads(raw[6:])
                if event.get("run_id"):
                    return event["run_id"]
    raise AssertionError("the run never said its id")


@check
async def check_wftabsdraft(browser) -> str:
    """A workflow's unsaved edit and its undo survive its tab going behind and coming back (TABS1 v2, F930)."""
    stamp = int(time.time())
    a, b = f"orch_uismoke_wfa_{stamp}", f"orch_uismoke_wfb_{stamp}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(a, 2), "name": "ui-smoke tabs A"})
    api("/api/orchestrations", "POST", {**laid_out_orchestration(b, 2), "name": "ui-smoke tabs B"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    name = page.locator("input[aria-label='Workflow name']")
    try:
        await page.goto(f"{APP}/workflows?id={a}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await name.fill("ui-smoke tabs A, edited")
        await asyncio.sleep(1.0)
        await _wf_open_from_picker(page, "ui-smoke tabs B")
        await _wf_canvas(page)
        on_b = await name.input_value()
        titles = await _wf_titles(page)
        nodes = await page.locator(".react-flow__renderer").count()
        await _wf_strip(page).get_by_role("tab", name=re.compile("tabs A")).click()
        await _wf_canvas(page)
        back = await name.input_value()
        undo = page.get_by_role("button", name=re.compile(r"^Undo"))
        can_undo = await undo.count() and await undo.first.is_enabled()
    finally:
        await ctx.close()
        drop_fixture(a)
        drop_fixture(b)
    if on_b != "ui-smoke tabs B":
        raise AssertionError(f"the second tab shows {on_b!r}, not its own workflow")
    if not any("tabs A" in t and "unsaved changes" in t for t in titles):
        raise AssertionError(f"the tab behind does not say it holds unsaved changes: {titles}")
    if nodes != 1:
        raise AssertionError(f"{nodes} canvases are mounted; only the tab in front should have one")
    if back != "ui-smoke tabs A, edited":
        raise AssertionError(f"coming back, the edit read {back!r}")
    if not can_undo:
        raise AssertionError("coming back, the edit cannot be undone")
    return "an unsaved edit and its undo come back with their tab; one canvas is mounted"


@check
async def check_wftabsclose(browser) -> str:
    """Closing a tab with unsaved work asks first; Cancel keeps it, Discard closes it (TABS1 v2, F930)."""
    oid = f"orch_uismoke_wfclose_{int(time.time())}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(oid, 2), "name": "ui-smoke tabs close"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    ask = page.get_by_role("dialog", name="Discard unsaved changes?")
    problems = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await page.locator("input[aria-label='Workflow name']").fill("ui-smoke tabs close, edited")
        await asyncio.sleep(0.8)
        close = page.get_by_role("button", name=re.compile(r"^Close ui-smoke tabs close"))
        await close.click()
        await asyncio.sleep(0.5)
        if not await ask.count():
            problems.append("closing an unsaved tab did not ask")
        else:
            await ask.get_by_role("button", name="Cancel").click()
            await asyncio.sleep(0.4)
            if len(await _wf_titles(page)) != 1:
                problems.append("Cancel closed the tab anyway")
            await close.click()
            await asyncio.sleep(0.5)
            await ask.get_by_role("button", name="Discard changes").click()
            await asyncio.sleep(0.8)
            if await _wf_strip(page).count() and await _wf_titles(page):
                problems.append("Discard changes did not close the tab")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if problems:
        raise AssertionError("; ".join(problems))
    return "an unsaved tab asks before closing; Cancel keeps it, Discard closes it"


@check
async def check_wftabscap(browser) -> str:
    """At ten tabs a new one is refused with a sentence, and none is closed for the person (TABS1 v2, F930)."""
    stamp = int(time.time())
    ids = [f"orch_uismoke_wfcap{i}_{stamp}" for i in range(11)]
    for i, oid in enumerate(ids):
        api("/api/orchestrations", "POST", {**laid_out_orchestration(oid, 1), "name": f"ui-smoke cap {i:02d}"})
    seeded = {"tabs": [{"id": f"wt_cap{i}", "kind": "workflow", "ref": oid, "key": f"workflow\u0000{oid}",
                        "title": f"ui-smoke cap {i:02d}", "usedAt": i + 1} for i, oid in enumerate(ids[:10])],
              "activeId": "wt_cap0"}
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    await ctx.add_init_script(f"try {{ localStorage.setItem('synapseTabs:workflows', {json.dumps(json.dumps(seeded))}); }} catch (e) {{}}")
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2)
        await _wf_open_from_picker(page, "ui-smoke cap 10")
        said = await page.get_by_text("Close a tab to open another: 10 are open.").count()
        kept = await page.evaluate("""() => (JSON.parse(localStorage.getItem('synapseTabs:workflows') || '{}').tabs || [])
            .map(t => t.ref)""")
    finally:
        await ctx.close()
        for oid in ids:
            drop_fixture(oid)
    if not said:
        raise AssertionError("an eleventh tab was not refused with its sentence")
    if sorted(kept) != sorted(ids[:10]):
        raise AssertionError(f"the open tabs changed at the cap: {kept}")
    return "at ten tabs a new one is refused in a sentence and nothing is closed"


@check
async def check_wftabsrun(browser) -> str:
    """A run opened from its workflow is a tab beside it, its page inside the editor (TABS1 v2, F1022, F1025)."""
    oid = f"orch_uismoke_wfrun_{int(time.time())}"
    api("/api/orchestrations", "POST", {**human_gate_orchestration(oid), "name": "ui-smoke tabs run"})
    run_id = _start_real_run(oid)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        # The workflow with its run on the canvas, as a reopened run link opens it.
        await page.goto(f"{APP}/workflows?run={run_id}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await asyncio.sleep(SETTLE)
        link = page.locator(f'a[href="/runs/{run_id}"]').first
        await link.wait_for(timeout=15_000)
        await link.click()
        await asyncio.sleep(SETTLE)
        url = page.url
        titles = await _wf_titles(page)
        front = await _wf_strip(page).get_by_role("tab", selected=True).get_attribute("aria-label") or ""
        text = await page.evaluate("() => document.querySelector('main').innerText")
    finally:
        await ctx.close()
        drop_fixture(oid)
    if "/runs/" in url:
        raise AssertionError("the run link left the editor instead of opening a tab")
    if len(titles) != 2 or not titles[1].startswith("Run"):
        raise AssertionError(f"the run did not open as a tab beside its workflow: {titles}")
    if not front.startswith("Run"):
        raise AssertionError(f"the run's tab is not in front: {front!r}")
    if "ui-smoke tabs run" not in text:
        raise AssertionError("the run's page did not draw inside its tab")
    return "a run link from its workflow opens the run as a tab beside it, on the same page"


@check
async def check_wftabsrail(browser) -> str:
    """Tabs take only links inside the workflows page: the menu still navigates (TABS1 v2, F1025)."""
    oid = f"orch_uismoke_wfrail_{int(time.time())}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(oid, 1), "name": "ui-smoke tabs rail"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await page.click('nav[aria-label="Primary"] a[href="/runs"]')
        try:
            await page.wait_for_url("**/runs", timeout=10_000)
            left = True
        except Exception:
            left = False
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not left:
        raise AssertionError("a link in the app's menu was taken by the workflow tabs")
    return "a link in the app's menu navigates as before"


@check
async def check_wftabsreload(browser) -> str:
    """Open workflow tabs and the front one come back after a reload (TABS1 v2, F930)."""
    stamp = int(time.time())
    a, b = f"orch_uismoke_wfra_{stamp}", f"orch_uismoke_wfrb_{stamp}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(a, 1), "name": "ui-smoke reload A"})
    api("/api/orchestrations", "POST", {**laid_out_orchestration(b, 1), "name": "ui-smoke reload B"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={a}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await _wf_open_from_picker(page, "ui-smoke reload B")
        await _wf_canvas(page)
        before = await _wf_titles(page)
        await page.goto(f"{APP}/workflows", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2)
        after = await _wf_titles(page)
        front = await _wf_strip(page).get_by_role("tab", selected=True).get_attribute("aria-label") or ""
    finally:
        await ctx.close()
        drop_fixture(a)
        drop_fixture(b)
    if len(before) != 2 or after != before:
        raise AssertionError(f"tabs before the reload {before}, after {after}")
    if "reload B" not in front:
        raise AssertionError(f"the front tab did not come back in front: {front!r}")
    return "two workflow tabs and the front one survive a reload"


@check
async def check_wftabsaddress(browser) -> str:
    """The address opens a tab while the page is open, the first workflow's too (TABS1 v2, F930): the page opened
    on A, B was opened beside it, and Jump to › A brings A's tab back in front. The editor keeps `?id=` in step
    without a navigation, so to the page's own props that jump changed nothing (stack 1's baseline, cmdk)."""
    stamp = int(time.time())
    a, b = f"orch_uismoke_wfaa_{stamp}", f"orch_uismoke_wfab_{stamp}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(a, 1), "name": f"ui-smoke address A {stamp}"})
    api("/api/orchestrations", "POST", {**laid_out_orchestration(b, 1), "name": f"ui-smoke address B {stamp}"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={a}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await _wf_open_from_picker(page, f"ui-smoke address B {stamp}")
        await _wf_canvas(page)
        await page.get_by_role("button", name=re.compile(r"^Jump to")).click()
        await page.locator("[cmdk-input]").wait_for(timeout=5_000)
        await page.keyboard.type(f"address A {stamp}")
        await asyncio.sleep(0.6)
        await page.keyboard.press("Enter")
        await asyncio.sleep(SETTLE + 1)
        front = await _wf_strip(page).get_by_role("tab", selected=True).get_attribute("aria-label") or ""
        titles = await _wf_titles(page)
        url = page.url
    finally:
        await ctx.close()
        drop_fixture(a)
        drop_fixture(b)
    if "address A" not in front:
        raise AssertionError(f"Jump to › A left {front!r} in front ({url})")
    if len(titles) != 2:
        raise AssertionError(f"the jump opened another tab: {titles}")
    return "Jump to › the workflow the page opened on brings its tab back, and opens no other"


#: The canvas's pan and zoom, as React Flow draws them.
VIEWPORT = "() => (document.querySelector('.react-flow__viewport') || {}).style?.transform || ''"


@check
async def check_wftabsview(browser) -> str:
    """A workflow tab comes back where its canvas stood; a tab opened fresh fits its workflow (TABS1 v2, Naveen)."""
    stamp = int(time.time())
    a, b = f"orch_uismoke_wfva_{stamp}", f"orch_uismoke_wfvb_{stamp}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(a, 3), "name": "ui-smoke view A"})
    api("/api/orchestrations", "POST", {**laid_out_orchestration(b, 3), "name": "ui-smoke view B"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={a}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        fitted_a = await page.evaluate(VIEWPORT)
        for _ in range(3):
            await page.get_by_role("button", name="Zoom in").first.click()
            await asyncio.sleep(0.3)
        await page.mouse.move(700, 500)
        await page.mouse.down()
        await page.mouse.move(560, 430, steps=6)
        await page.mouse.up()
        await asyncio.sleep(0.6)
        moved_a = await page.evaluate(VIEWPORT)
        await _wf_open_from_picker(page, "ui-smoke view B")
        await _wf_canvas(page)
        # A tab opened fresh shows its whole workflow: every step inside the canvas.
        outside = await page.evaluate("""() => {
            const box = document.querySelector('.react-flow').getBoundingClientRect();
            return [...document.querySelectorAll('.react-flow__node')].filter(n => {
                const r = n.getBoundingClientRect();
                return r.left < box.left - 1 || r.right > box.right + 1 || r.top < box.top - 1 || r.bottom > box.bottom + 1;
            }).length;
        }""")
        await _wf_strip(page).get_by_role("tab", name=re.compile("view A")).click()
        await _wf_canvas(page)
        await asyncio.sleep(0.6)
        back_a = await page.evaluate(VIEWPORT)
    finally:
        await ctx.close()
        drop_fixture(a)
        drop_fixture(b)
    if moved_a == fitted_a:
        raise AssertionError("zooming and panning did not move the canvas: the check proves nothing")
    if back_a != moved_a:
        raise AssertionError(f"coming back the canvas stood at {back_a!r}, not where it was left ({moved_a!r})")
    if outside:
        raise AssertionError(f"a tab opened fresh did not fit its workflow: {outside} steps outside the canvas")
    return "a tab comes back to its own zoom and pan exactly; a fresh tab fits its workflow"


@check
async def check_wftabspickrun(browser) -> str:
    """The new tab's picker lists recent runs, live ones first; picking one opens it as a tab (TABS1 v2, Naveen)."""
    oid = f"orch_uismoke_wfpick_{int(time.time())}"
    api("/api/orchestrations", "POST", {**human_gate_orchestration(oid), "name": "ui-smoke pick run"})
    run_id = _start_real_run(oid)
    other = f"orch_uismoke_wfpick2_{int(time.time())}"
    api("/api/orchestrations", "POST", {**laid_out_orchestration(other, 1), "name": "ui-smoke pick other"})
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/workflows?id={other}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        await page.get_by_role("button", name="Open a workflow in a new tab").first.click()
        await asyncio.sleep(1.5)
        listed = page.get_by_role("group", name="Recent runs")
        first = await listed.get_by_role("option").first.inner_text() if await listed.count() else ""
        await listed.get_by_role("option", name=re.compile("ui-smoke pick run")).first.click()
        await asyncio.sleep(SETTLE)
        titles = await _wf_titles(page)
        front = await _wf_strip(page).get_by_role("tab", selected=True).get_attribute("aria-label") or ""
    finally:
        await ctx.close()
        drop_fixture(oid)
        drop_fixture(other)
    if not first:
        raise AssertionError("the new tab's picker has no Recent runs")
    if "ui-smoke pick run" not in first or "Waiting on you" not in first:
        raise AssertionError(f"the live run waiting on a person is not first: {first!r}")
    if not front.startswith("Run") or len(titles) != 2:
        raise AssertionError(f"picking the run did not open it as a tab in front: {titles}")
    return "the picker lists recent runs, the live one first, and picking one opens it as a tab"


def odd_shape_orchestration(oid: str) -> dict:
    """The workflow HOT2 read on prod (10-24, rc4.3's builder), its values cut to their shape: a human step's
    `human_fields` saved as an object holding the list. The store refuses it now (F1047), so it reaches the page the
    way it reached prod's, as the API's answer."""
    def step(sid, name, kind, nxt, x, **extra):
        return {"id": sid, "name": name, "type": kind, "next_step_id": nxt, "position_x": x, "position_y": 0.0,
                "input_keys": [], "output_key": f"{sid}_out", "human_fields": [], "route_map": {},
                "parallel_branches": [], "loop_step_ids": [], **extra}
    return {
        "id": oid, "name": "ui-smoke odd shape", "description": "", "avatar": "default", "trigger": "manual",
        "entry_step_id": "step_account", "state_schema": {}, "max_total_turns": 100, "timeout_minutes": 30,
        "steps": [
            step("step_account", "Create Twitter Developer Account", "llm", "step_keys", -600),
            step("step_keys", "Generate API Keys", "human", "step_tool", -340,
                 human_prompt="Please generate and input your Twitter API keys.",
                 human_fields={"fields": [{"name": "api_key", "type": "str"}, {"name": "api_secret_key", "type": "str"}]}),
            step("step_tool", "Create Custom Python Tool", "llm", "step_end", -80),
            step("step_end", "End", "end", None, 180, output_key=None),
        ],
    }


@check
async def check_oddshape(browser) -> str:
    """A workflow saved in a shape the editor did not expect opens, says which step and what, and stays editable (F1054).

    On prod a builder-made workflow whose human step held `human_fields: {"fields": [...]}` threw inside the canvas's
    checks, and with no boundary anywhere the page went to Next's error card (HOT2, D323). The API's own answer is
    replaced with that document, so the page reads exactly what prod's did, with nothing written to the store.
    """
    oid = f"orch_uismoke_odd_{int(time.time())}"
    doc = odd_shape_orchestration(oid)
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    thrown: list[str] = []
    page.on("pageerror", lambda e: thrown.append(str(e)))

    async def answer(route):
        if route.request.method != "GET":
            await route.continue_()
            return
        if route.request.url.split("?")[0].endswith(f"/api/orchestrations/{oid}"):
            await route.fulfill(json=doc)
            return
        real = await route.fetch()
        listed = await real.json()
        await route.fulfill(response=real, json=[*(listed if isinstance(listed, list) else []), doc])

    await page.route(re.compile(r"/api/orchestrations(/orch_uismoke_odd_\d+)?(\?.*)?$"), answer)
    problems: list[str] = []
    try:
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        try:
            await _wf_canvas(page)
        except Exception:
            body = await page.locator("body").inner_text()
            raise AssertionError(f"the canvas never drew: {body[:200]!r}; thrown: {thrown[:2]}")
        drawn = await page.locator(".react-flow__node").count()
        if drawn != len(doc["steps"]):
            problems.append(f"{drawn} of {len(doc['steps'])} steps drew")
        if await page.locator("[data-editor-boundary]").count():
            problems.append("a part of the editor fell to its boundary on a shape the reader knows")
        # What was read differently is said, on its step.
        await page.get_by_role("button", name=re.compile(r"to check$")).first.click()
        await asyncio.sleep(0.6)
        said = page.get_by_text(re.compile("form fields were saved inside an object")).first
        if not await said.count():
            problems.append("the step's warning does not say its form fields were read differently")
        await page.keyboard.press("Escape")
        # The step opens with its fields as the person reads them, and the step beside it is still editable.
        await open_step(page, oid, "Generate API Keys")
        values = await page.evaluate("() => [...document.querySelectorAll('input')].map(i => i.value)")
        if "api_key" not in values or "api_secret_key" not in values:
            problems.append(f"the human step's card does not show its two fields: {values[:12]}")
        await open_step(page, oid, "Create Custom Python Tool")
        if not await page.get_by_label(re.compile(r"^name$", re.I)).first.is_editable():
            problems.append("the step beside it is not editable")
        # A step that throws anyway (a Retrieve step's query saved as an object, which the reader does not open) is a
        # card naming it, and the page and every other step still stand.
        doc["steps"].insert(2, {"id": "step_find", "name": "Find notes", "type": "retrieve", "next_step_id": "step_tool",
                                "position_x": -210, "position_y": 180.0, "retrieve": {"kb_ids": ["kb_notes"], "query": {"q": 1}}})
        doc["steps"][1]["next_step_id"] = "step_find"
        await page.goto(f"{APP}/workflows?id={oid}", wait_until="domcontentloaded")
        await _wf_canvas(page)
        card = page.locator("[data-editor-boundary]")
        named = await card.first.inner_text() if await card.count() else ""
        if "Find notes" not in named:
            problems.append(f"the step that throws is not a card naming it: {named!r}")
        if await page.locator(".react-flow__node").count() != len(doc["steps"]):
            problems.append("the steps beside the one that throws did not all draw")
        if not await page.get_by_role("button", name=re.compile(r"to check$")).count():
            problems.append("the bar did not stand beside the step that throws")
    finally:
        await ctx.close()
    if thrown:
        problems.append(f"the page threw: {thrown[0][:200]}")
    if problems:
        raise AssertionError("; ".join(problems))
    return ("the odd-shaped workflow opens, every step draws, its step says what was read, and the rest stays editable; "
            "a step that throws anyway is a card naming it")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
