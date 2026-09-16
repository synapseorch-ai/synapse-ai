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
                  return {found: true, hits, box: {x: Math.round(r.x), y: Math.round(r.y),
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
        return "Answer is clickable at 1600 and 400"
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


# ── runner ───────────────────────────────────────────────────────────────────

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
