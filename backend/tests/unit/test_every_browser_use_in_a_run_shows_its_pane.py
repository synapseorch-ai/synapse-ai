"""Every browser use in a run shows its pane, live, in sharper pictures (F1356; Naveen 11-04: "rework how the run page
shows the browser, so people can actually see it and benefit"; eb).

The run page opens its browser pane on a `browser_session` event and lists each `browser_action`, which the browser
component queues for its owner (`BrowserTools.take_events`). Only the agent loop drained them (react_engine): a Tool
step calling browser_* itself, and a Browser step replaying a recording, showed no pane at all. Now both drain:

- a Tool step after its browser call, as the agent loop does, its events carried with the step's own id;
- a Browser step while its replay runs (every half second, then once more at its end), in the order the component
  queued them, each with the step's id — and a step cancelled mid-replay leaves no replay running and says nothing
  after its end.

And the picture: the screencast and the first frame are JPEG 75 (they were 60), and the screenshot kept for each action
85, so the run page's large view is worth looking at. The skip-not-lag offer to a viewer is as it was.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from core.browser import client as browser_client

SESSION = {"type": "browser_session", "session_id": "bs1", "owner": "run:run_f1356"}


def _action(n: int) -> dict:
    return {"type": "browser_action", "session_id": "bs1", "n": n, "actor": "agent", "action": "click",
            "target": f"Order {n}", "url": "https://portal.example/orders", "shot_ref": str(n)}


class _Tools:
    """The browser component: queues its owner's events as a call or a replay goes, as `BrowserTools` does."""

    def __init__(self):
        self.events: dict[str, list[dict]] = {}
        self.finished = self.cancelled = False

    def take_events(self, key):
        return self.events.pop(key, []) if key else []

    def _queue(self, key, event):
        self.events.setdefault(key, []).append(event)

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        key = browser_client.owner.get()
        self._queue(key, SESSION)
        self._queue(key, _action(1))
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Navigated.")], isError=False)

    async def replay_for_step(self, key, doc, steps, inputs, *, carry_on=False):
        try:
            self._queue(key, SESSION)
            for n in (1, 2, 3):
                await asyncio.sleep(0.6)
                self._queue(key, _action(n))
            await asyncio.sleep(0.6)
            self.finished = True
            return {"outputs": {"orders": 3}}
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def latest_replay(self, key):
        return None


@pytest.fixture
def tools(monkeypatch):
    fake = _Tools()
    monkeypatch.setattr(browser_client, "tools", fake)
    return fake


class TestAToolStep:
    async def test_its_browser_call_carries_the_panes_events(self, tools, client):
        from core.orchestration.steps import ToolStepExecutor

        server = types.SimpleNamespace(agent_sessions={browser_client.SERVER_NAME: tools}, memory_store=None,
                                       tool_router={"browser_navigate": (browser_client.SERVER_NAME,
                                                                         "browser_navigate")})
        carried: list[dict] = []
        out = await ToolStepExecutor()._execute_tool("browser_navigate", {"url": "https://portal.example"},
                                                     types.SimpleNamespace(server_module=server),
                                                     carried, run_id="run_f1356")
        assert out == "Navigated."
        assert [e["type"] for e in carried] == ["browser_session", "browser_action"], carried


@pytest.fixture
def replay(monkeypatch, tools):
    """A Browser step on a recording with no inputs, in a run of its own."""
    from core.browser import picks
    from core.browser import recordings as recs
    from core.models_orchestration import OrchestrationRun, StepConfig

    async def get(rid):
        return {"id": rid, "name": "Orders", "steps": []}

    async def settings_for(*a, **k):
        return {}

    monkeypatch.setattr(recs, "get", get)
    monkeypatch.setattr(picks, "settings_for", settings_for)
    step = StepConfig(id="read_orders", name="Read the orders", type="browser", recording_id="rec_1",
                      output_key="orders")
    run = OrchestrationRun(run_id="run_f1356", orchestration_id="orch_f1356", shared_state={})
    return step, run


def _replaying(step, run):
    from core.orchestration.steps import BrowserStepExecutor

    return BrowserStepExecutor().execute(step, run, types.SimpleNamespace(global_variables={}))


class TestABrowserStep:
    async def test_its_replay_shows_live_in_order_with_its_steps_id(self, tools, replay, client):
        step, run = replay
        seen: list[tuple[dict, bool]] = []
        async for event in _replaying(step, run):
            seen.append((event, tools.finished))
        browser = [(e, done) for e, done in seen if e.get("type", "").startswith("browser_")]
        assert [e["type"] for e, _ in browser] == ["browser_session"] + ["browser_action"] * 3, seen
        assert [e.get("n") for e, _ in browser[1:]] == [1, 2, 3], "in the order the component queued them"
        assert all(e.get("orch_step_id") == "read_orders" for e, _ in browser), browser
        assert browser[1][1] is False, "an action reached the run page while the replay still ran"
        final = [e for e, _ in seen if e.get("type") == "final"]
        assert final and json.loads(final[0]["response"]) == {"orders": 3}
        assert seen.index((final[0], True)) > max(seen.index(x) for x in browser), "its events come before its end"

    async def test_cancelled_mid_replay_it_leaves_nothing_running_and_says_nothing_after(self, tools, replay, client):
        step, run = replay
        seen: list[dict] = []

        async def consume():
            async for event in _replaying(step, run):
                seen.append(event)

        task = asyncio.create_task(consume())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(e.get("type") == "browser_session" for e in seen):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        at_cancel = len(seen)
        await asyncio.sleep(1.5)
        assert tools.cancelled and not tools.finished, "the replay went with its step"
        assert len(seen) == at_cancel, "nothing said after the step's end"
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("browser-replay")]


class _Page:
    url = "https://portal.example/orders"

    def __init__(self):
        self.qualities: list[int] = []

    async def screenshot(self, *, type, quality, **_kw):
        self.qualities.append(quality)
        return b"\xff\xd8jpeg"


class _CDP:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def on(self, *_a):
        return None

    async def send(self, method, params=None):
        self.sent.append((method, dict(params or {})))


def _session():
    from core.browser.host import Session

    s = object.__new__(Session)
    s.page, s._cdp, s.viewers, s.log, s.shots, s.id = _Page(), None, [], [], {}, "bs1"
    s.wheel, s.banner = "agent", None
    cdp = _CDP()
    s.ctx = types.SimpleNamespace(new_cdp_session=lambda page: asyncio.sleep(0, result=cdp), pages=[s.page])
    return s, cdp


class TestThePicture:
    async def test_the_screencast_is_jpeg_75(self):
        s, cdp = _session()
        await s._start_screencast()
        started = [p for m, p in cdp.sent if m == "Page.startScreencast"]
        assert started and started[0]["format"] == "jpeg" and started[0]["quality"] == 75, cdp.sent

    async def test_the_first_frame_is_jpeg_75(self):
        s, _ = _session()
        await s.first_frame()
        assert s.page.qualities == [75]

    async def test_the_screenshot_kept_for_an_action_is_jpeg_85(self):
        s, _ = _session()
        await s._record("agent", "click", "Order 1")
        assert s.page.qualities == [85] and s.shots
