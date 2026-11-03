"""A browser turn answered from the run, not the pane, does what the pane does, only on an explicit yes (F1303, CAP3's
cap-05 A2; 79 11-03).

cap-05's run parked on the person's turn to allow typing on the portal ("The agent wants to type on
portal.localhost"), and the answer came through the run's own human-input (as an API caller or a chat channel answers),
not the browser pane. The engine re-opened the step, but nothing told the browser: its wheel stayed with the person, so
every later call answered "The person is still driving the browser" until the run timed out. Now an answer from the run
to a turn of this run's own browser carries out the turn's own action through the browser, as the pane's buttons do:
on an explicit yes, allow typing on the host the browser itself asked about (`allow_typing`), or hand the browser back.
A no, or anything that is not a yes, hands the wheel back with nothing allowed, and the step hears that the person
declined. A held send is the person's to answer in the browser, never from the run; a turn of another run's browser, or
one already answered in the pane, is not touched.
"""
import types

import pytest

from _fakes import seed as S
from core.browser import client as browser_client
from core.browser import recordings

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order"}
HOST = "portal.example.com"


def _steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": f"https://{HOST}/"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"}}]


class _Browser:
    """The run's browser: answers in order, keeps the parked turn as the client does, and what it was asked."""

    def __init__(self, *answers: dict, owner: str | None = None):
        self.answers = list(answers)
        self.calls: list[tuple] = []
        self.parked: dict | None = None
        self.owner = owner

    async def replay_for_step(self, key, doc, steps, inputs, *, carry_on=False):
        self.calls.append(("replay", key, carry_on))
        return {**self.answers.pop(0), "session_id": "bs1"}

    async def park_turn(self, key, handover, **where):
        self.calls.append(("park", key, handover.get("kind")))
        self.parked = {**handover, **where}

    async def take_parked(self, key):
        self.calls.append(("taken", key))
        parked, self.parked = self.parked, None
        return parked

    async def owner_of(self, session_id):
        return self.owner or (self.calls[0][1] if self.calls else None)

    async def viewer(self, session_id, path, body=None):
        self.calls.append(("viewer", session_id, path, dict(body or {})))
        return 200, {}


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _flow(rec_id: str) -> dict:
    return S.make_orchestration(id="orch_f1303", entry_step_id="b", steps=[
        {"id": "b", "name": "Find it", "type": "browser", "recording_id": rec_id,
         "recording_inputs": {"order number": "{state.user_input}"}, "output_key": "orders", "next_step_id": None}])


def _turn(kind: str = "allow_typing") -> dict:
    handover = {"kind": kind, "reason": f"The agent wants to type on {HOST}.", "session_id": "bs1"}
    if kind == "allow_typing":
        handover["host"] = HOST
    return {"handover": handover, "at": 2, "step_id": "s2"}


async def _parked(monkeypatch, run_id: str, kind: str = "allow_typing", owner: str | None = None) -> _Browser:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    rec = await recordings.create("Find an order", _steps(), by="you")
    browser = _Browser(_turn(kind), {"done": 2, "outputs": {}}, owner=owner)
    monkeypatch.setattr(browser_client, "tools", browser)
    flow = _flow(rec["id"])
    await S.seed_orchestrations([flow])
    engine = OrchestrationEngine(Orchestration.model_validate(flow), _server())
    events = [e async for e in engine.run("A-1001", run_id=run_id)]
    assert events[-1].get("kind") == "browser_turn" and ("park", f"run:{run_id}", kind) in browser.calls
    return browser


async def _settle(run_id: str) -> None:
    """The resume the answer spawned, to its end."""
    from core.orchestration.runner import _active_tasks

    task = _active_tasks.get(run_id)
    if task is not None:
        await task


async def _answer(client, run_id: str, answer) -> None:
    """Answered as the app's own Answer control answers (the in-app human-input route): the person, signed in."""
    await client.post(f"/api/orchestrations/runs/{run_id}/human-input", json={"response": answer})
    await _settle(run_id)


def _viewer(browser: _Browser) -> list[tuple]:
    return [c for c in browser.calls if c[0] == "viewer"]


@pytest.mark.parametrize("yes", [{"approve": True}, "yes", {"response": "ok"}])
async def test_a_yes_allows_typing_on_the_host_the_browser_asked_about(client, monkeypatch, yes):
    browser = await _parked(monkeypatch, "run_f1303_yes")
    await _answer(client, "run_f1303_yes", yes)
    assert _viewer(browser) == [("viewer", "bs1", "/allow-site", {"host": HOST})], browser.calls
    replay = [c for c in browser.calls if c[0] == "replay"]
    assert replay[-1][2] is True, "the next browser call runs, after the allow"
    assert browser.calls.index(("viewer", "bs1", "/allow-site", {"host": HOST})) < browser.calls.index(replay[-1])


@pytest.mark.parametrize("no", [{"approve": False}, "no", "maybe later"])
async def test_anything_but_a_yes_allows_nothing_and_gives_the_wheel_back(client, monkeypatch, no):
    browser = await _parked(monkeypatch, "run_f1303_no")
    await _answer(client, "run_f1303_no", no)
    assert [c[2] for c in _viewer(browser)] == ["/wheel/hand-back"], browser.calls
    assert not [c for c in browser.calls if c[0] == "viewer" and c[2] == "/allow-site"], "typing stays off"


async def test_another_turn_is_handed_back_on_a_yes(client, monkeypatch):
    browser = await _parked(monkeypatch, "run_f1303_turn", kind="asked")
    await _answer(client, "run_f1303_turn", {"approve": True, "note": "signed in"})
    assert [c[2] for c in _viewer(browser)] == ["/wheel/hand-back"], browser.calls


async def test_a_held_send_is_the_persons_in_the_browser_only(client, monkeypatch):
    browser = await _parked(monkeypatch, "run_f1303_send", kind="send")
    await _answer(client, "run_f1303_send", {"approve": True})
    assert _viewer(browser) == [], "a run's yes never sends what the network holds"


async def test_another_runs_browser_is_not_touched(client, monkeypatch):
    browser = await _parked(monkeypatch, "run_f1303_other", owner="run:someone_else")
    await _answer(client, "run_f1303_other", {"approve": True})
    assert _viewer(browser) == [] and ("taken", "run:run_f1303_other") not in browser.calls


async def test_the_panes_own_answer_is_left_as_it_did_it(client, monkeypatch):
    browser = await _parked(monkeypatch, "run_f1303_pane")
    await _answer(client, "run_f1303_pane", {"browser_turn": "answered", "note": "done"})
    assert _viewer(browser) == [], "the pane allowed it and resumed the run itself"
