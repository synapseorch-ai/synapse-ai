"""A schedule's missed-run policy and what it runs are changes Synapse AI makes (F1267, REACH1; D340 §6).

The Schedules screen sets what a schedule does with a time it missed (`missed_run_policy`: run it as soon as it can,
or skip it) and what it starts; Synapse AI could set neither. Both are now ops of the one table, through the same
`update_schedule` the builder moves a schedule's time with (F578: the same schedule, its id and history kept):

- `set_schedule` and `create_schedule` take `missed_run_policy`, run_immediately or skip, and nothing else;
- `retarget_schedule {runs}` points a schedule at another workflow or agent, found by name or id as a new
  schedule's is, and one not here is refused;
- the review says each by name; undo puts back the policy, and what it started;
- a deleted schedule comes back with its policy, not the default.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import change_ops as C
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


_N = {"n": 0}


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_f1267_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    return run, checked, await _call("apply_change", {}, run)


async def _workflow(name):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    flow = make_orchestration(name=name)
    await save_orchestration(flow)
    return flow


async def _schedule(target_id, **kw):
    from core.store import schedules

    await schedules.save({"id": "sched_n", "name": "Every night at 2", "enabled": True,
                          "target_type": "orchestration", "target_id": target_id, "prompt": "Go",
                          "schedule_type": "cron", "cron_expression": "0 2 * * *", **kw})


class TestTheTable:
    @pytest.mark.parametrize("value", ["run_immediately", "skip"])
    def test_a_policy_is_one_of_the_screens_two(self, value):
        assert C.issues("set_schedule", {"kind": "schedule", "id": "s"}, {"missed_run_policy": value}) == []

    def test_anything_else_is_refused_by_name(self):
        assert C.issues("set_schedule", {"kind": "schedule", "id": "s"}, {"missed_run_policy": "sometimes"}) == [
            "`set_schedule`: `missed_run_policy` is run_immediately or skip"]

    def test_the_policy_is_what_update_schedule_sets(self):
        assert C.call(C.OPS["set_schedule"], "s1", {"missed_run_policy": "run_immediately"}, {}) == (
            "update_schedule", {"schedule_id": "s1", "missed_run_policy": "run_immediately"})

    def test_a_new_schedule_takes_it_too(self):
        _, args = C.call(C.OPS["create_schedule"], "", {"runs": {"kind": "workflow", "id": "w1"}, "prompt": "Go",
                                                        "cron_expression": "0 9 * * 1",
                                                        "missed_run_policy": "run_immediately"}, None, name="Mondays")
        assert args["missed_run_policy"] == "run_immediately"

    def test_a_retarget_sets_only_what_it_starts(self):
        assert C.call(C.OPS["retarget_schedule"], "s1", {"runs": {"kind": "agent", "id": "a1"}}, {}) == (
            "update_schedule", {"schedule_id": "s1", "target_type": "agent", "target_id": "a1"})

    def test_a_retarget_needs_what_it_runs(self):
        assert "`retarget_schedule` needs runs" in C.issues("retarget_schedule", {"kind": "schedule", "id": "s"}, {})

    def test_the_schedule_tools_take_the_policy(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS

        for name in ("create_schedule", "update_schedule"):
            schema = next(t for t in BUILDER_TOOL_SCHEMAS if t["function"]["name"] == name)
            assert schema["function"]["parameters"]["properties"]["missed_run_policy"]["enum"] == list(C.MISSED)


async def test_a_policy_is_set_and_undone(client):
    from core.store import schedules

    flow = await _workflow("Nightly digest")
    await _schedule(flow["id"])
    first, checked, done = await _change({"ops": [{"op": "set_schedule", "target": {"kind": "schedule",
                                                                                    "id": "sched_n"},
                                                   "fields": {"missed_run_policy": "run_immediately"}}]})
    assert checked["review"] == '- Change the schedule "Every night at 2" — missed_run_policy: run_immediately'
    assert done["applied"] == ["set_schedule"]
    assert (await schedules.get("sched_n"))["missed_run_policy"] == "run_immediately"
    await _change({"undo_of": first.run_id})
    assert (await schedules.get("sched_n"))["missed_run_policy"] == "skip"


async def test_a_schedule_is_pointed_at_another_workflow_by_name_and_back(client):
    from core.store import schedules

    old, new = await _workflow("Nightly digest"), await _workflow("Weekly report")
    await _schedule(old["id"])
    first, checked, done = await _change({"ops": [{"op": "retarget_schedule",
                                                   "target": {"kind": "schedule", "name": "Every night at 2"},
                                                   "fields": {"runs": {"kind": "workflow", "name": "weekly report"}}}]})
    assert checked["review"] == '- Change what runs on the schedule "Every night at 2" — starts: "Weekly report"'
    assert done["applied"] == ["retarget_schedule"]
    moved = await schedules.get("sched_n")
    assert (moved["target_type"], moved["target_id"], moved["cron_expression"]) == ("orchestration", new["id"],
                                                                                    "0 2 * * *")
    _, undo_checked, _ = await _change({"undo_of": first.run_id})
    assert 'starts: "Nightly digest"; undo: what it started before' in undo_checked["review"]
    assert (await schedules.get("sched_n"))["target_id"] == old["id"]


async def test_a_retarget_at_nothing_here_is_refused(client):
    flow = await _workflow("Nightly digest")
    await _schedule(flow["id"])
    _, checked, done = await _change({"ops": [{"op": "retarget_schedule", "target": {"kind": "schedule",
                                                                                     "id": "sched_n"},
                                               "fields": {"runs": {"kind": "workflow", "name": "No such flow"}}}]})
    assert done is None and checked["say"] == 'No workflow is called "No such flow".'


async def test_a_deleted_schedule_comes_back_with_its_policy(client):
    from core.store import schedules

    flow = await _workflow("Nightly digest")
    await _schedule(flow["id"], missed_run_policy="run_immediately")
    first, _, _ = await _change({"ops": [{"op": "delete_schedule", "target": {"kind": "schedule", "id": "sched_n"}}]})
    await _change({"undo_of": first.run_id})
    [back] = [s for s in await schedules.load() if s["name"] == "Every night at 2"]
    assert back["missed_run_policy"] == "run_immediately" and back["enabled"] is False
