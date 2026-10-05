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
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
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
    await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
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
            await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE + 2.0)
            await page.get_by_role("button", name="Run", exact=True).click()
            # Settle on content, never networkidle: the notifications stream is
            # held open for the life of the page.
            for _ in range(25):
                await asyncio.sleep(1.0)
                if await page.evaluate(
                    "() => [...document.querySelectorAll('button')]"
                    ".some(b => /Waiting for your input/.test(b.innerText || ''))"
                ):
                    break
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
  const panel = document.querySelector('aside[aria-label="AI builder"]');
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


async def _open_builder(browser, width=WIDE):
    ctx = await browser.new_context(viewport={"width": width[0], "height": width[1]})
    await ctx.add_init_script(BUILDER_STUB)
    page = await ctx.new_page()
    await page.goto(APP, wait_until="domcontentloaded")
    await asyncio.sleep(SETTLE + 1.0)
    await page.get_by_role("button", name="Open the AI builder").click()
    await asyncio.sleep(0.5)
    await page.locator('aside[aria-label="AI builder"] textarea').fill("build me something")
    # Send is disabled until React has seen the change event `fill` dispatches.
    await page.get_by_role("button", name="Send", exact=True).wait_for()
    for _ in range(20):
        if not await page.get_by_role("button", name="Send", exact=True).is_disabled():
            break
        await asyncio.sleep(0.1)
    await page.get_by_role("button", name="Send", exact=True).click()
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
LAYOUT_PAGES = ("/dashboard", "/orchestrations", "/agents", "/vault", "/usage", "/knowledge")


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

        # The launcher's own label, where a person would look for it.
        await page.goto(f"{APP}/dashboard", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        # By its name's start: with a builder question waiting it says so (F214).
        launcher = page.locator('button[aria-label^="Open the AI builder"]')
        box = await launcher.bounding_box()
        if box is None:
            raise AssertionError("no AI-builder launcher on /dashboard")
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
        raise AssertionError("the launcher offers no tooltip at all")
    if tip["bottom"] > WIDE[1] or tip["right"] > WIDE[0] or tip["x"] < 0 or tip["y"] < 0:
        raise AssertionError(
            f"the launcher's tooltip {tip['text']!r} is outside the window "
            f"(bottom {round(tip['bottom'])} of {WIDE[1]})"
        )
    gap = box["x"] - tip["right"]
    if gap > 200:
        raise AssertionError(
            f"the launcher's tooltip {tip['text']!r} opens {round(gap)}px from the button "
            f"it describes — it is anchored to something else"
        )
    return f"no page scrolls, and the launcher's tooltip opens {round(gap)}px from it"


# ── contrast ─────────────────────────────────────────────────────────────────

#: Both themes, because the defect this exists for was reported as a light-mode
#: one and was in fact in both: the light palette is an inversion of the dark
#: one, so it inherits the dark one's mistakes exactly.
CONTRAST_PAGES = (
    "/", "/agents", "/orchestrations", "/schedules", "/tools", "/vault", "/runs",
    "/evals", "/mcp-servers", "/skills", "/kitchen-sink",
    "/settings/general", "/settings/models", "/settings/scale", "/settings/widgets",
    "/settings/messaging", "/settings/memory", "/settings/api-keys",
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
            await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
            await asyncio.sleep(SETTLE + 2.0)
            await page.get_by_role("button", name="Run", exact=True).click()
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

    "Build with AI" on an orchestration screen tells the builder which one, so
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

        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Build with AI").first.click()
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run", exact=True).click()
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
        card = page.locator("div.group", has_text=GLOBALS_TOOL["generalName"]).first
        await card.get_by_role("button", name=re.compile(r"^edit$", re.I)).click()
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
        sections = ("Redis Connection", "Postgres", "Workers", "Observability")
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
        await page.get_by_role("button", name=re.compile("^Environment Variables Reference")).first.click()
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
        await page.get_by_role("button", name="Save Changes").click()
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
        await page.click('a[href="/orchestrations"]')
        if not await asked():
            problems.append("leaving through the rail discarded an unsaved edit without asking")
        else:
            await dialog.get_by_role("button", name=re.compile("cancel|keep", re.I)).first.click()
            await asyncio.sleep(0.6)
            if not page.url.endswith("/agents"):
                problems.append(f"keeping the edit still left, for {page.url}")
            await page.click('a[href="/orchestrations"]')
            await asyncio.sleep(0.8)
            await dialog.get_by_role("button", name=re.compile("discard", re.I)).first.click()
            await asyncio.sleep(1.5)
            if not page.url.endswith("/orchestrations"):
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
        await page.goto(f"{APP}/mcp-servers", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)
        opener = page.get_by_role("button", name=re.compile(r"^(add|new|connect)", re.I)).first
        if await opener.count():
            await opener.click()
            await asyncio.sleep(0.8)
        await page.get_by_role("button", name=re.compile(r"Remote \(URL\)")).first.click()
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        await page.get_by_role("button", name="Guardrails", exact=True).first.click()
        await asyncio.sleep(0.8)
        turns = await page.get_by_label("Max Total Turns").count()
        cost = await page.get_by_label(re.compile("max cost", re.I)).count()
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not turns:
        raise AssertionError("the Guardrails tab did not open (no Max Total Turns)")
    if cost:
        raise AssertionError("Guardrails still offers Max Cost (USD), which nothing enforces")
    return "Guardrails offers turns and a timeout, and no cost cap that does nothing"


# ── reasoning effort ─────────────────────────────────────────────────────────

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
        levels = await page.evaluate("""() => {
          const label = [...document.querySelectorAll('label, span, p')]
            .find(e => (e.textContent || '').trim() === 'Reasoning');
          if (!label) return null;
          return [...label.parentElement.querySelectorAll('button')].map(b => ({
            name: (b.textContent || '').trim().toLowerCase(), pressed: b.getAttribute('aria-pressed')}));
        }""")
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
        await page.goto(f"{APP}/mcp-servers", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        rows = await page.evaluate("""(names) => names.map(n => {
          const tag = [...document.querySelectorAll('span')].find(s => (s.textContent || '').trim() === n);
          const row = tag && tag.closest('div.group');
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
        levels = await page.evaluate("""() => {
          const label = [...document.querySelectorAll('label, span, p')]
            .find(e => (e.textContent || '').trim() === 'Reasoning');
          if (!label) return null;
          return [...label.parentElement.querySelectorAll('button')].map(b => ({
            name: (b.textContent || '').trim().toLowerCase(), pressed: b.getAttribute('aria-pressed')}));
        }""")
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
    "/", "/agents", "/orchestrations", "/schedules", "/tools", "/vault", "/runs",
    "/evals", "/mcp-servers", "/skills",
    "/settings/general", "/settings/models", "/settings/agents", "/settings/widgets",
    "/settings/databases", "/settings/schedules", "/settings/messaging", "/settings/scale",
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


NODES_UNDER_PALETTE = r"""
() => {
  const pal = document.querySelector('.react-flow__panel.top.left');
  if (!pal) return {palette: false, hits: []};
  const p = pal.getBoundingClientRect();
  const hits = [];
  for (const n of document.querySelectorAll('.react-flow__node')) {
    const r = n.getBoundingClientRect();
    const overlap = r.left < p.right && r.right > p.left && r.top < p.bottom && r.bottom > p.top;
    if (overlap) hits.push(`${n.innerText.split('\n')[0]} at x ${Math.round(r.left)}–${Math.round(r.right)}, palette to ${Math.round(p.right)}`);
  }
  return {palette: true, hits};
}
"""


@check
async def check_canvasfit(browser) -> str:
    """Fitting the graph leaves room for the step palette that floats over it.

    Fit-view used the whole canvas, and the palette is drawn on top of its left
    edge, so an orchestration opened with its first step under the palette —
    and a click on that step pressed the palette (F31). Checked on open and on
    the Fit view button, with the palette open (its default).
    """
    oid = f"orch_uismoke_fit_{int(time.time())}"
    api("/api/orchestrations", "POST", laid_out_orchestration(oid))
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        opened = await page.evaluate(NODES_UNDER_PALETTE)
        await page.get_by_role("button", name="Fit view").click()
        await asyncio.sleep(0.8)
        refit = await page.evaluate(NODES_UNDER_PALETTE)
    finally:
        await ctx.close()
        drop_fixture(oid)
    if not opened["palette"]:
        raise AssertionError("no step palette on the canvas")
    problems = [f"on open: {h}" for h in opened["hits"]] + [f"after Fit view: {h}" for h in refit["hits"]]
    if problems:
        raise AssertionError("steps under the palette — " + "; ".join(problems))
    return "every step clear of the palette, on open and after Fit view"


@check
async def check_palettelink(browser) -> str:
    """A step added from the palette follows the step that is selected.

    The palette only appended: four steps added one after another ran as one,
    the first completing and the run ending, while the editor said only
    "Unreachable from the entry point" (F47). Now a selected linear step with
    no next step points at the new one and hands it its output key, so adding
    steps in a row builds a chain. Read from what Save actually sends.
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
        palette = page.locator(".react-flow__panel.top.left")
        for _ in range(2):
            await palette.get_by_role("button", name="Print", exact=True).click()
            await asyncio.sleep(0.4)
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
    return "two steps added from the palette chain on from the selected step"


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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        await page.evaluate(r"""() => { window.__saveKey = null;
            window.addEventListener('keydown', (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's')
                    setTimeout(() => { window.__saveKey = e.defaultPrevented; });
            }, true); }""")
        name = page.get_by_role("textbox", name="Orchestration name", exact=True)
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
        # The orchestration screen: run it, answer it, read what went out.
        await page.keyboard.press("Escape")
        await page.get_by_role("button", name="Run", exact=True).click()
        for _ in range(25):
            await asyncio.sleep(1.0)
            if await page.get_by_text("Waiting for your input").count():
                break
        await page.get_by_text("Waiting for your input").first.click()
        await asyncio.sleep(0.6)
        dialog = page.get_by_role("dialog")
        try:
            await fill_human_fields(dialog)
            await dialog.get_by_role("button", name="Submit", exact=True).click()
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run", exact=True).click()
        for _ in range(25):
            await asyncio.sleep(1.0)
            if await page.get_by_text("Waiting for your input").count():
                break
        else:
            raise AssertionError("the run never reached its question")

        await page.get_by_role("button", name="Cancel", exact=True).click()
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
    window.__firstPaint = { light: h.classList.contains('light-mode'), scheme: getComputedStyle(h).colorScheme };
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
  const label = [...document.querySelectorAll('main span')]
    .find((s) => /tracking-\\[0\\.2em\\]/.test(s.className));
  return {
    label: label ? label.textContent : null,
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
        "() => (document.querySelector('aside[aria-label=\"AI builder\"]') || {}).innerText || ''")


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
        launcher = page.get_by_role("button", name=re.compile("Open the AI builder"))
        named = await launcher.get_attribute("aria-label") or ""
        if "waiting" not in named:
            raise AssertionError(f"a plan waiting after a reload does not show on the launcher ({named!r})")
        await launcher.click()
        await asyncio.sleep(0.5)
        text = await _panel_text(page)
        for shown in ("a weekly digest of the tickets", "Here is the proposed plan: one step that summarises."):
            if shown not in text:
                raise AssertionError(f"after a reload the panel does not show {shown!r}")
        await page.locator('aside[aria-label="AI builder"] textarea').fill("approve")
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
        await page.get_by_role("button", name=re.compile("Open the AI builder")).click()
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
            hidden_rows = await page.evaluate(
                "() => [...document.querySelectorAll('header button')]"
                ".filter(b => /^(Alpha|Beta)/.test(b.innerText.trim())).length")
        finally:
            await ctx.close()
        if switched[-1:] != [{"agent_id": "agent_beta"}]:
            raise AssertionError(f"at {width[0]}px choosing Beta sent {switched}")
        if hidden_rows:
            raise AssertionError(f"at {width[0]}px the closed switcher leaves {hidden_rows} agent rows in the header")
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
    back over it, and the next message went into the old conversation.
    """
    history = {"turns": [{"user": "an old question", "assistant": "an old answer", "tools": [], "timestamp": ""}],
               "pending_approval": None, "running": None}
    ctx, page = await _chat_page(browser, "uismoke-old-conversation", history, extra_init=HOLD_HISTORY)
    try:
        for _ in range(40):
            if await page.evaluate("() => typeof window.__releaseHistory === 'function'"):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("the page never asked for its conversation's history")
        await page.get_by_role("button", name="New Chat").click()
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
  const label = [...document.querySelectorAll('main span')].find((s) => /tracking-\\[0\\.2em\\]/.test(s.className));
  const tag = [...document.querySelectorAll('main div')].find((d) => /-top-3/.test(d.className) && /tracking-wider/.test(d.className));
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return {left: r.left, right: r.right, height: r.height, text: el.textContent.slice(0, 30)}; };
  return {
    pane: pane ? {scroll: pane.scrollWidth, client: pane.clientWidth, right: pane.getBoundingClientRect().right} : null,
    label: box(label), tag: box(tag), bubble: box(tag && tag.parentElement), vw: innerWidth,
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
        if not label or label["left"] < 0 or label["right"] - label["left"] > 600 or label["height"] > 30:
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
  const points = [[r.left + r.width / 2, r.top + r.height / 2], [r.right - 3, r.bottom - 3],
                  [r.right - 3, r.top + 3], [r.left + 3, r.bottom - 3], [r.left + 3, r.top + 3]];
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run", exact=True).click()
        await page.get_by_text("Done — status: failed").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        live = await page.evaluate(STEP_REASON)
        if not any("smoke boom" in line for line in live["log"]):
            raise AssertionError(f"the run log does not say why: {live['log']}")
        if live["card"] != ["Exception: smoke boom"]:
            raise AssertionError(f"the failed step's card says {live['card']}")
        if not live["run"]:
            raise AssertionError("the run log never named its run")

        await page.goto(f"{APP}/orchestrations?run={live['run']}", wait_until="domcontentloaded")
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
  return card ? (card.className.match(/border-(warning|success|danger|accent|border-strong)/) || [card.className])[0] : null;
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 2.0)
        await page.get_by_role("button", name="Run", exact=True).click()
        await page.get_by_text("Waiting for your input").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        run_id = await page.evaluate("() => (document.body.innerText.match(/Started run (\\S+)/) || [])[1] || null")
        if await page.evaluate(QUESTION_FRAME, asking) != "border-warning":
            raise AssertionError("the question never showed as waiting, so this checks nothing")

        await page.get_by_role("button", name="Cancel", exact=True).click()
        await asyncio.sleep(2.0)
        if await page.evaluate(QUESTION_FRAME, asking) == "border-warning":
            raise AssertionError("the step that asked still shows as waiting after the cancel")
        mine = [n for n in api("/api/notifications")["notifications"] if n.get("run_id") == run_id]
        asked = [n for n in mine if n.get("kind") == "human_input"]
        if not asked or not all(n.get("resolved") and n.get("resolved_as") == "cancelled" for n in asked):
            raise AssertionError(f"the question's notification is {asked}")
        if not any(n.get("title", "").endswith("cancelled") for n in mine):
            raise AssertionError(f"no notification says the run was cancelled: {[n.get('title') for n in mine]}")

        await page.goto(f"{APP}/orchestrations?run={run_id}", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 4.0)
        if await page.get_by_text("Waiting for your input").count():
            raise AssertionError("reopened from its link, the cancelled run asks again")
        if await page.evaluate(QUESTION_FRAME, asking) == "border-warning":
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

        await page.goto(f"{APP}/orchestrations", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE + 1.0)
        await page.get_by_role("button", name=re.compile(r"^Recent")).click()
        try:
            await page.get_by_role("button", name=f"View run {run_id}").wait_for(timeout=20_000)
        except Exception:
            raise AssertionError("a run in the Recent table is not a control a keyboard can reach")
        await page.get_by_role("button", name=re.compile(r"^Orchestrations")).click()
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
    """An agent's tools are checkboxes a keyboard can tick, and "all" can be narrowed (F184, F235).

    The Capabilities cards were `div`s with a click handler: no role, no state,
    out of the tab order, and nothing else in the product gives an agent tools.
    Beside that, with the agent on "all" a group's box could never be turned
    off: it re-added the group's names next to "all".
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
        box = page.get_by_role("checkbox", name="Time & Date", exact=True)
        if not await box.count():
            raise AssertionError("a Capabilities card is not a checkbox")
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
        return "tool groups and their tools are checkboxes a keyboard ticks, and \"all\" narrows"
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
        await page.goto(f"{APP}/runs", wait_until="domcontentloaded")
        tab = page.get_by_role("button", name=re.compile("Orchestration Logs"))
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

    The API reference (Settings → API Keys) and the Scale deployment guide slide
    off-screen when closed but stayed in the page: 13 and 10 controls a keyboard
    tabbed into and a screen reader read while nothing was visible, and the API
    reference's close × had no name.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        for path, title, opener in (
            ("/settings/api_keys", "API Reference", re.compile("view docs", re.I)),
            ("/settings/scale", "Scale Deployment Guide", re.compile("deploy docs", re.I)),
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
        return "closed drawers are out of reach; open ones have named buttons"
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
        await page.goto(f"{APP}/orchestrations?run=run_smoke_kb", wait_until="domcontentloaded")
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
    said they are read "once OCR is configured", and nothing in OSS configures one."""
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    for pattern, handler in _SMOKE_KB_ROUTES:
        await ctx.route(pattern, handler)
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/knowledge", wait_until="domcontentloaded")
        zone = page.get_by_text(re.compile(r"^Drop files here")).first
        await zone.wait_for(timeout=30_000)
        said = " ".join((await zone.inner_text()).split())
        if "OCR is configured" in said or "this install has no OCR" not in said:
            raise AssertionError(f"the drop zone says {said!r}")
        return "the drop zone says this install reads no scanned pages or images"
    finally:
        await ctx.close()


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
        await page.get_by_role("button", name="Save Changes", exact=True).click()
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
    """The API reference never shows a stream example the API refuses (F199).

    V2's React example opened `new EventSource(…/stream?token=YOUR_API_KEY)`.
    The key is read only from the Authorization header, which EventSource cannot
    send: that example got 401 on v1 and v2 alike. Every stream example now reads
    with fetch and the header, and V1 says how to follow a run at all.
    """
    ctx = await browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/settings/api_keys", wait_until="domcontentloaded")
        opener = page.get_by_role("button", name=re.compile("view docs", re.I))
        await opener.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await opener.click()
        await asyncio.sleep(0.8)
        drawer = page.locator(".fixed", has=page.locator("h2", has_text="API Reference")).last
        shown = {}
        for version in ("V1", "V2"):
            await drawer.get_by_role("button", name=version, exact=True).click()
            await asyncio.sleep(0.4)
            # Sections start closed; open every one so each code block is in the page.
            # A header is a button around an upper-case title; a closed one has no body after it.
            await drawer.evaluate("""(root) => {
              for (const b of root.querySelectorAll('button')) {
                if (b.querySelector(':scope > span.uppercase') && !b.nextElementSibling) b.click();
              }
            }""")
            await asyncio.sleep(0.4)
            shown[version] = await drawer.locator("pre").all_inner_texts()
        for version, blocks in shown.items():
            if any("new EventSource(" in b or "?token=" in b for b in blocks):
                raise AssertionError(f"{version}'s docs still open a stream the API refuses (EventSource / ?token=)")
        if not any("/events/stream" in b for b in shown["V1"]):
            raise AssertionError("V1's docs never say how to follow a run")
        hook = next((b for b in shown["V2"] if "function useOrchestrationRun" in b), "")
        if "Authorization" not in hook or "Last-Event-ID" not in hook:
            raise AssertionError("V2's React example does not send the key in its header, or cannot resume")
        return "every stream example sends the key in its header, and V1 shows how to follow a run"
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
    New or Build with AI landed on Save or Run. Below `sm` the rarer three now
    sit in a ⋯ menu, the actions take a second row, and the step palette, which
    covered most of the canvas, starts folded.
    """
    oid = f"orch_uismoke_phone_{int(time.time())}"
    api("/api/orchestrations", "POST", print_orchestration(oid, "ui-smoke phone toolbar"))
    ctx = await browser.new_context(viewport={"width": 360, "height": 780})
    page = await ctx.new_page()
    try:
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        names = ["Back", "Orchestration", "New orchestration", "Build with AI", "Undo (Ctrl+Z)",
                 "Save", "Run", "More actions"]
        reach = await page.evaluate(TOOLBAR_REACH, names)
        bad = {k: v for k, v in reach.items() if v != "ok"}
        if bad:
            raise AssertionError(f"at 360 px these editor controls cannot be tapped: {bad}")
        await page.get_by_role("button", name="More actions").click()
        items = await page.get_by_role("menuitem").all_inner_texts()
        if [i.strip() for i in items] != ["Deploy as Agent", "Duplicate", "Delete"]:
            raise AssertionError(f"the ⋯ menu holds {items}")
        await page.keyboard.press("Escape")
        if not await page.get_by_role("button", name="Show step palette").count():
            raise AssertionError("with no choice stored, the step palette opens over a phone's canvas")
        return "at 360 px every editor action can be tapped, the rare ones behind ⋯, and the palette starts folded"
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        async with page.expect_response(
            lambda r: r.url.endswith("/api/orchestrations") and r.request.method == "POST", timeout=20_000,
        ) as made:
            await page.get_by_role("button", name="Duplicate", exact=True).click()
        copy_id = (await (await made.value).json())["id"]
        await asyncio.sleep(1.0)
        if f"id={copy_id}" not in page.url:
            raise AssertionError(f"after Duplicate the address is {page.url}, not the copy {copy_id}")
        await page.reload(wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        shown = await page.get_by_role("button", name="Orchestration", exact=True).inner_text()
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
          (cls.match(/border-(warning|success|danger|accent|border-strong)/) || ['?'])[0]
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run", exact=True).click()
        await page.get_by_text("Waiting for your input").first.wait_for(timeout=60_000)
        await asyncio.sleep(0.5)
        run_id = await page.evaluate("() => (document.body.innerText.match(/Started run (\\S+)/) || [])[1] || null")
        await page.get_by_text("Waiting for your input").first.click()
        await asyncio.sleep(0.8)
        await page.get_by_role("textbox", name="Your answer").fill("yes")
        await page.get_by_role("button", name="Submit", exact=True).click()
        await page.get_by_text("Done — status: completed").first.wait_for(timeout=60_000)
        await asyncio.sleep(1.5)
        live = await page.evaluate(NODE_FRAMES)
        if any(v != "border-success" for v in live.values()):
            raise AssertionError(f"after the resumed run completed, the canvas shows {live}")
        await page.goto(f"{APP}/orchestrations?run={run_id}", wait_until="domcontentloaded")
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
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
            for path, shows in (("/orchestrations", "Needs input"), ("/tools", "Smoke n8n"), ("/mcp-servers", "Smoke off"),
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
        await page.get_by_role("combobox").first.click()
        await page.get_by_role("option", name=f"ui-smoke scored {stamp}").click()
        button = page.get_by_role("button", name=re.compile("score earlier runs", re.I))
        try:
            await button.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("an orchestration with a rubric and nothing scored offers no backfill")
        await button.click()
        await page.get_by_text("Scored 2 of 2 runs.").wait_for(timeout=10_000)
        if not backfills:
            raise AssertionError("the backfill button sent nothing")

        row = page.get_by_role("button", name=re.compile(f"^Smoke set {stamp}")).locator("xpath=..")
        await row.get_by_role("button", name="Run", exact=True).click()
        await asyncio.sleep(0.8)
        if native:
            raise AssertionError(f"running a set asks with a native dialog: {native[0][:80]!r}")
        dialog = page.get_by_role("dialog")
        text = await dialog.inner_text()
        if "not be scored" not in text or "judge call" in text:
            raise AssertionError(f"a set with no rubric is confirmed as {text[:160]!r}")
        await dialog.get_by_role("button", name="Run set").click()
        await row.get_by_role("button", name=re.compile("Stop")).wait_for(timeout=10_000)
        stopped = False
        for _ in range(30):
            await asyncio.sleep(1.0)
            if not await row.get_by_role("button", name=re.compile("Stop")).count():
                stopped = True
                break
        if not stopped:
            raise AssertionError("a set run from a collapsed card still shows Stop after it has ended")

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

        await page.goto(f"{APP}/runs", wait_until="domcontentloaded")
        tab = page.get_by_role("button", name=re.compile("Orchestration Logs"))
        await tab.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await tab.click()
        row = page.get_by_role("button", name=re.compile("^" + re.escape(f"ui-smoke tz {stamp}")))
        await row.wait_for(timeout=20_000)
        await asyncio.sleep(1.0)
        shown = await row.inner_text()
        raw = next((log["started_at"] for log in api("/api/logs/orchestrations?limit=50") if log["run_id"] == run_id), None)
        if not raw:
            raise AssertionError("the fixture run wrote no log, so this checks nothing")
        if raw in shown:
            raise AssertionError(f"Runs & Logs prints the log's UTC stamp {raw!r} as it was written")
        local = await page.evaluate("(s) => new Date(s.replace(' ', 'T') + 'Z').toLocaleString()", raw)
        if local not in shown:
            raise AssertionError(f"Runs & Logs shows {shown!r} for {raw} UTC, not the reader's {local!r}")
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
        row = await reach("/settings/messaging", "Smoke channel", "a channel")
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
        await page.get_by_role("button", name=re.compile("history", re.I)).first.click()
        drawer = page.get_by_role("dialog", name="Chat history")
        try:
            await drawer.wait_for(timeout=10_000)
        except Exception:
            raise AssertionError("the chat History drawer is not a named dialog")
        focused = await page.evaluate("() => document.activeElement.getAttribute('aria-label')")
        if focused != "Close chat history":
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
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
            header = await page.evaluate("() => document.querySelector('main header, header')?.innerText || ''")
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
    """The answer dialog says what its context box holds, and names every field (U65).

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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run", exact=True).click()
        strip = page.get_by_role("button", name=re.compile("Waiting for your input"))
        await strip.wait_for(timeout=30_000)
        await strip.click()
        dialog = page.get_by_role("dialog", name="Human input required")
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
        await page.goto(f"{APP}/orchestrations?id={oid}", wait_until="domcontentloaded")
        await page.locator(".react-flow__node").first.wait_for(timeout=30_000)
        await asyncio.sleep(SETTLE)
        await page.get_by_role("button", name="Run", exact=True).click()
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
        await page.goto(f"{APP}/mcp-servers", wait_until="domcontentloaded")
        await asyncio.sleep(SETTLE)

        async def add(name: str, url: str) -> str:
            # A saved add resets the form to its default type (stdio).
            await page.get_by_role("button", name=re.compile(r"Remote \(URL\)")).click()
            await page.get_by_label("Unique ID").fill(name)
            await page.get_by_label("Server URL").fill(url)
            await page.get_by_role("button", name="Connect Server").click()
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
    await page.locator("div.group", has_text="Smoke python tool").first \
        .get_by_role("button", name=re.compile("^edit$", re.I)).click()
    await page.get_by_role("button", name="Run", exact=True).click()


async def _openapi_preview(page):
    await page.get_by_role("button", name=re.compile("Import OpenAPI")).first.click()
    await page.get_by_placeholder(re.compile(r'^\{"openapi"')).fill('{"openapi": "3.0.0", "paths": {}}')
    await page.get_by_role("button", name=re.compile("Preview tools")).click()


async def _paste_credentials(page):
    await page.get_by_role("button", name=re.compile("Re-upload credentials")).first.click()
    await page.get_by_placeholder(re.compile(r'^\{"installed"')).fill(
        '{"installed": {"client_id": "x", "project_id": "y"}}')


async def _vault_error(page):
    await page.get_by_text("note.txt", exact=True).first.click()
    editor = page.get_by_placeholder("Start typing...")
    # The file's text lands after the editor does; typing first is overwritten
    # by it, and SAVE stays disabled (a clean file has nothing to save).
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
    await page.get_by_role("button", name=re.compile("^Test search")).click()
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
    ("a Retrieve step's trace", "/orchestrations?run=run_smoke_kb", [], _SMOKE_KB_RUN_ROUTES,
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
    ("a failed run", "/orchestrations?run=run_smoke_fail", [], _SMOKE_RUN_ROUTES, None, "Nested runs (1)"),
    ("a step's full response", "/orchestrations?run=run_smoke_fail", [], _SMOKE_RUN_ROUTES,
     lambda p: p.get_by_title("View full response").first.click(), "— full response"),
    ("Recent Runs' Resume", "/orchestrations?id=orch_smoke_c", [], _SMOKE_RUN_ROUTES,
     lambda p: p.get_by_role("button", name=re.compile(r"^Recent Runs")).first.click(), "Resume"),
    ("usage", "/usage", [], _SMOKE_USAGE, None, "Top orchestration runs by savings"),
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
    ("messaging last error", "/settings/messaging", [], [("**/api/messaging/channels", _answer([
        {"id": "ch_smoke_err", "name": "Smoke broken channel", "platform": "telegram", "status": "error",
         "last_error": "Unauthorized: bot token rejected (401)", "agent_ids": [], "multi_agent_mode": False,
         "enabled": True, "credentials": {}}]))],
     lambda p: p.get_by_role("button", name=re.compile("Smoke broken channel")).first.click(), "Last Error"),
    ("a compacted log", "/runs", [], [
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
     lambda p: p.get_by_role("button", name="Run Now").first.click(), "Schedule triggered successfully"),
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
     _vault_error, "ERROR"),
    ("settings saved toast", "/settings/personal_details", [],
     [("**/api/personal-details", _answer({}, methods=("POST",)))],
     lambda p: p.get_by_role("button", name=re.compile("Save Changes")).first.click(), "Personal details saved"),
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

    Naveen: help opens "when clicking" — never as a tour on a first visit — so a fresh browser loads pages and
    nothing opens, and nothing is fetched from the docs site either. Then the panel: a dialog named for the page,
    closed by Escape, its ✕ and the scrim, each handing focus back to the "?" that opened it (F382's drawer).
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


#: Every screen in the rail and Settings (H1). Each panel must be the page's own: headed like the page's `<h1>`,
#: never the fallback, with three to five steps. Chat has no `<h1>`, so its heading is named.
HELP_PAGES = (
    "/dashboard", "/runs", "/evals", "/usage", "/orchestrations", "/agents", "/skills", "/schedules", "/tools",
    "/mcp-servers", "/vault", "/knowledge",
    "/settings/general", "/settings/models", "/settings/personal_details", "/settings/variables", "/settings/memory",
    "/settings/workspace", "/settings/messaging", "/settings/widgets", "/settings/repos", "/settings/db_configs",
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
            if expect is None:
                await page.locator("main h1").first.wait_for(timeout=30_000)
                expect = (await page.locator("main h1").first.inner_text()).strip()
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
        await walk(page, f"/orchestrations?id={oid}", "Editing an orchestration",
                   lambda p: p.locator(".react-flow").first)
        await walk(page, "/knowledge", "Working in a knowledge base",
                   lambda p: p.get_by_role("button", name="Upload files"))
    finally:
        await ctx.close()
        drop_fixture(oid)
    if bad:
        raise AssertionError(f"{len(bad)} — " + "; ".join(bad))
    return f"{len(HELP_PAGES) + 3} pages and states each have a \"?\" whose panel is their own, with 3–5 steps"

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
                note = await fn(browser)
                print(f"ok {name}: {note} ({time.time() - started:.1f}s)")
            except Exception as exc:                      # a broken check fails alone
                failed += 1
                print(f"FAIL {name}: {exc}")
        await browser.close()
    print(f"\n{len(names) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
