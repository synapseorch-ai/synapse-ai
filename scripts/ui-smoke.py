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
LAYOUT_PAGES = ("/dashboard", "/orchestrations", "/agents", "/vault", "/usage")


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
        f"{len(CONTRAST_PAGES)} screens, a run log and a chat turn in both themes meet AA"
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
            items = await page.get_by_role("menuitem").all_inner_texts()
            if not any("Beta" in t for t in items) or not any("Alpha" in t for t in items):
                raise AssertionError(f"at {width[0]}px the switcher lists {items}")
            await page.get_by_role("menuitem", name=re.compile("Beta")).focus()
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
        if await boxes.count() != 6:
            raise AssertionError(f"Settings → Memory has {await boxes.count()} checkboxes, not its six cards")
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
