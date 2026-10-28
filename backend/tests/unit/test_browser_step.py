"""The Browser workflow step (BRW1, D328, F1146; DESIGN §v5.4): a recording replayed by code in the run's browser.

Naveen 2026-10-29: a workflow run is never watched, so a recorded send goes only where the step allows it (and only
to where the recording sent: the component's list, `test_browser_host_e2e`); anything else is the person's turn. A
changed page is the step's Browser agent's to find, or the person's; either way the path is a pending edit. A
recording made from a workflow's step goes with the workflow, unless something else uses it.
"""
import types

import pytest

from _fakes import seed as S
from core.browser import client as browser_client
from core.browser import recordings

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order"}


def _steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": "https://portal.example.com/"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"}},
            {"id": "s3", "action": "click", "target": {"role": "button", "name": "Submit"}, "may_send": True}]


class _Browser:
    """The run's browser as the step sees it: answers in order, and what it was asked."""

    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    async def replay_for_step(self, key, doc, steps, inputs, *, carry_on=False):
        self.calls.append(("replay", key, [(s["id"], s.get("may_send")) for s in steps], dict(inputs), carry_on))
        return {**self.answers.pop(0), "session_id": "bs1"}

    async def latest_replay(self, key):
        self.calls.append(("latest", key))
        return {**self.answers.pop(0), "session_id": "bs1"}

    async def hand_to_person(self, key, reason):
        self.calls.append(("hand", key, reason))
        return {"kind": "asked", "reason": reason, "session_id": "bs1"}

    async def park_turn(self, key, handover, **where):
        self.calls.append(("park", key, handover.get("kind")))


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _flow(rec_id: str, **step) -> dict:
    return S.make_orchestration(id="orch_brw", entry_step_id="b", steps=[
        {"id": "b", "name": "Find it", "type": "browser", "recording_id": rec_id,
         "recording_inputs": {"order number": "{state.user_input}"}, "output_key": "orders", "next_step_id": None,
         **step}])


async def _start(flow, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    await S.seed_orchestrations([flow])
    engine = OrchestrationEngine(Orchestration.model_validate(flow), _server())
    return [e async for e in engine.run("A-1001", run_id=run_id)]


async def _hand_back(run_id):
    from core.orchestration.engine import OrchestrationEngine
    return [e async for e in OrchestrationEngine.resume(run_id, {"browser_turn": "answered", "note": "done"},
                                                        _server())]


async def _state(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _paused(events) -> bool:
    return bool(events) and events[-1].get("type") == "human_input_required" and events[-1].get("kind") == "browser_turn"


async def test_a_browser_step_replays_with_the_runs_inputs_and_keeps_what_it_read(monkeypatch):
    rec = await recordings.create("Find an order", _steps(), by="you")
    browser = _Browser({"done": 3, "outputs": {"orders": [{"id": "A-1001"}]}})
    monkeypatch.setattr(browser_client, "tools", browser)
    await _start(_flow(rec["id"]), "run_b1")
    run = await _state("run_b1")
    assert run.status == "completed" and run.shared_state["orders"] == {"orders": [{"id": "A-1001"}]}
    (call,) = browser.calls
    assert call[0] == "replay" and call[1] == "run:run_b1" and call[3] == {"order number": "A-1001"}


async def test_in_a_run_only_the_steps_allowed_to_send_on_their_own_may_and_any_other_send_is_the_persons(
        monkeypatch):
    """Rule (2): the step's list decides, not what the recording carried; a send it doesn't allow pauses the run."""
    rec = await recordings.create("Find an order", _steps(), by="you")
    turn = {"handover": {"kind": "press", "reason": "Press 'Submit' yourself", "session_id": "bs1"}, "at": 3,
            "step_id": "s3"}
    browser = _Browser(turn, {"done": 3, "outputs": {}})
    monkeypatch.setattr(browser_client, "tools", browser)
    events = await _start(_flow(rec["id"], may_send_steps=[]), "run_b2")
    assert browser.calls[0][2] == [("s1", False), ("s2", False), ("s3", False)], "the recording's own flag is not enough"
    assert _paused(events) and ("park", "run:run_b2", "press") in browser.calls
    await _hand_back("run_b2")
    assert browser.calls[-1][0] == "replay" and browser.calls[-1][4] is True, "after their turn it carries on"
    assert (await _state("run_b2")).status == "completed"

    allowed = _Browser({"done": 3, "outputs": {}})
    monkeypatch.setattr(browser_client, "tools", allowed)
    await _start(_flow(rec["id"], may_send_steps=["s3"]), "run_b3")
    assert allowed.calls[0][2][2] == ("s3", True)


async def test_a_changed_page_pauses_for_the_person_and_their_way_waits_for_approval(monkeypatch):
    rec = await recordings.create("Find an order", _steps(), by="you")
    miss = {"miss": {"n": 3, "step_id": "s3", "line": "Click Submit", "why": "its target is not on the page"}, "done": 2}
    fixed = [{"id": "p1", "action": "click", "actor": "person", "target": {"name": "Send"}, "value": None}]
    browser = _Browser(miss, {"done": 3, "outputs": {}, "fix": {"step_id": "s3", "n": 3, "steps": fixed}})
    monkeypatch.setattr(browser_client, "tools", browser)
    events = await _start(_flow(rec["id"]), "run_b4")
    assert _paused(events) and browser.calls[1][0] == "hand" and "Click Submit" in browser.calls[1][2]
    await _hand_back("run_b4")
    doc = await recordings.get(rec["id"])
    assert doc["version"] == 1 and [s["id"] for s in doc["steps"]] == ["s1", "s2", "s3"], "unchanged until approved"
    (edit,) = doc["pending_edits"]
    assert edit["step_id"] == "s3" and edit["run_id"] == "run_b4" and edit["by"] != "agent"
    assert (await _state("run_b4")).status == "completed"


async def test_a_changed_page_is_the_steps_agents_to_find_and_its_way_waits_for_approval(monkeypatch):
    from core.orchestration import steps as steps_mod
    rec = await recordings.create("Find an order", _steps(), by="you")
    miss = {"miss": {"n": 3, "step_id": "s3", "line": "Click Submit", "why": "gone"}, "done": 2}
    fixed = [{"id": "a1", "action": "click", "actor": "agent", "target": {"name": "Send"}, "value": None}]
    browser = _Browser(miss, {"done": 3, "outputs": {"x": 1}, "fix": {"step_id": "s3", "n": 3, "steps": fixed}})
    monkeypatch.setattr(browser_client, "tools", browser)
    asked: list = []

    async def fix(self, step, run, engine, doc, progress):
        asked.append((step.agent_id, progress.get("browser_miss", {}).get("n")))
        return
        yield
    monkeypatch.setattr(steps_mod.BrowserStepExecutor, "_fix", fix)
    await S.seed_agents([{"id": "portal", "name": "Portal", "type": "browser", "tools": [], "system_prompt": "x"}])
    await _start(_flow(rec["id"], on_miss="agent", agent_id="portal"), "run_b5")
    assert asked == [("portal", 3)] and browser.calls[-1][0] == "latest"
    (edit,) = (await recordings.get(rec["id"]))["pending_edits"]
    assert edit["by"] == "agent" and (await _state("run_b5")).shared_state["orders"] == {"x": 1}


async def test_a_workflow_takes_its_own_recordings_with_it_and_leaves_the_rest(client):
    """Rule (3): made from its step → deleted with it, pictures too; used elsewhere → kept; a library one → never."""
    from core.browser import kept
    from core.store.resources import save_agent, save_orchestration
    own = await recordings.create("Mine", _steps(), by="you", owner_workflow="orch_del", shots={"r1-s1": "AAAA"})
    shared = await recordings.create("Also an agent's", _steps(), by="you", owner_workflow="orch_del")
    library = await recordings.create("Library", _steps(), by="you")
    flow = _flow(own["id"])
    flow.update(id="orch_del", steps=[{**flow["steps"][0], "next_step_id": "b2"},
                                     {**flow["steps"][0], "id": "b2", "recording_id": library["id"]}])
    await save_orchestration(flow)
    await save_agent({"id": "ag1", "name": "A", "type": "browser", "tools": [], "system_prompt": "x",
                      "browser": {"recordings": [shared["id"]]}})
    assert kept.read_recording_shot(own["id"], "r1-s1") is not None
    assert (await client.delete("/api/orchestrations/orch_del")).status_code == 200
    with pytest.raises(recordings.NotFound):
        await recordings.get(own["id"])
    assert kept.read_recording_shot(own["id"], "r1-s1") is None
    assert (await recordings.get(shared["id"]))["owner_workflow"] is None, "kept, now a library recording"
    assert (await recordings.get(library["id"]))["id"] == library["id"]


async def test_a_browser_step_saves_only_with_a_recording_and_a_browser_agent_to_find_the_way(client):
    from core.store.resources import save_agent
    rec = await recordings.create("Find an order", _steps(), by="you")
    await save_agent({"id": "chatty", "name": "C", "type": "conversational", "tools": [], "system_prompt": "x"})
    base = {"id": "orch_save", "name": "W", "entry_step_id": "b"}
    step = {"id": "b", "name": "B", "type": "browser", "next_step_id": None}
    for bad in ({}, {"recording_id": "rec_nope"}, {"recording_id": rec["id"], "on_miss": "agent"},
                {"recording_id": rec["id"], "on_miss": "agent", "agent_id": "chatty"}):
        resp = await client.post("/api/orchestrations", json={**base, "steps": [{**step, **bad}]})
        assert resp.status_code == 422, bad
    ok = await client.post("/api/orchestrations", json={**base, "steps": [{**step, "recording_id": rec["id"]}]})
    assert ok.status_code == 200, ok.text
