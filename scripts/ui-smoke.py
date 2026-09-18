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
                    if (el.closest('button[aria-label="Open the AI builder"]')) return 'the AI-builder launcher';
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
        launcher = page.locator('button[aria-label="Open the AI builder"]')
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
)

#: Known, owned, and deliberately not fixed here. `--danger` is one token doing
#: two jobs — it is the colour of 60 pieces of danger *text* on a dark ground
#: and the ground of 18 danger *buttons* under white text — and no single value
#: serves both. Splitting it changes the kit, which is synced to the other
#: product, so it belongs to the synced-UI batch and not to this one. Anything
#: not named here is a regression.
#: Matched on the colours themselves, not on class names: a class attribute is
#: long, generated and truncated, while the pair of colours IS the defect.
CONTRAST_WAIVED = (
    ("#ffffff", "#f87171",
     "white on --danger: one token is the colour of 60 pieces of danger text on a "
     "dark ground and the ground of 18 danger buttons under white text, and no "
     "single value serves both. Splitting it changes the kit, which is synced."),
)

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
        try:
            for path in CONTRAST_PAGES:
                await page.goto(f"{APP}{path}", wait_until="domcontentloaded")
                await asyncio.sleep(SETTLE)
                for row in await page.evaluate(CONTRAST):
                    if any(row["fg"] == fg and row["bg"] == bg
                           for fg, bg, _ in CONTRAST_WAIVED):
                        continue
                    bad.append(
                        f"{theme} {path}: {row['ratio']}:1 (needs {row['need']}) "
                        f"{row['fg']} on {row['bg']} — {row['txt'][:24]!r}"
                    )
        finally:
            await ctx.close()
    if bad:
        raise AssertionError(f"{len(bad)} under AA — " + "; ".join(bad[:5]))
    return (
        f"{len(CONTRAST_PAGES)} screens in both themes meet AA"
        f" ({len(CONTRAST_WAIVED)} waived, owned by the synced-UI batch)"
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
