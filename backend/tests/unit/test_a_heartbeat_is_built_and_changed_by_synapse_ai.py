"""A schedule's Heartbeat is something Synapse AI builds and changes, as the Schedules screen sets it (F1728).

A heartbeat runs at its time only while its board has an open task (`run_when: has_work`), a new task starts it
within a minute (`wake_on_new_task`), and `max_runs_per_day` limits it (F1727). Each is a field of the one op table:

- `set_schedule` and `create_schedule` take the three, and refuse anything else by name;
- waiting for work needs Boards on, said before the person's yes rather than as the save's refusal after it;
- a plan's schedule carries `run_when` and its limit to the schedule it makes, and its card says what a heartbeat
  does when there is no work;
- undo puts the Heartbeat back, and a deleted schedule comes back with it;
- the builder's schedule tools and its schedules reader carry the fields too.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes.knowledge_env import use_saved_settings
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.models_schedule import MAX_RUNS_PER_DAY, RUN_WHEN
from core.native_builder import change_ops as C
from core.native_builder import objects as O
from core.native_builder.compile import compile_plan
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

SCHEDULE = {"kind": "schedule", "id": "s"}


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


@pytest.fixture
async def boards_on(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings
    await save_settings({"boards_enabled": True})


_N = {"n": 0}


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_f1728_{_N['n']}", orchestration_id="orch_native_builder",
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

    await schedules.save({"id": "sched_h", "name": "Support inbox", "enabled": True,
                          "target_type": "orchestration", "target_id": target_id, "prompt": "Work the open tasks",
                          "schedule_type": "interval", "interval_value": 15, "interval_unit": "minutes", **kw})


def _set(fields):
    return {"ops": [{"op": "set_schedule", "target": {"kind": "schedule", "id": "sched_h"}, "fields": fields}]}


class TestTheTable:
    @pytest.mark.parametrize("value", RUN_WHEN)
    async def test_run_when_is_one_of_two(self, value, boards_on):
        assert C.issues("set_schedule", SCHEDULE, {"run_when": value}) == []

    def test_waiting_for_work_with_boards_off_names_the_switch(self):
        assert C.issues("set_schedule", SCHEDULE, {"run_when": "has_work"}) == [
            "`set_schedule`: Waiting for work reads its boards, and Boards are off. Turn on Boards in Settings "
            "first, or run it at its time."]

    def test_anything_else_is_refused_by_name(self):
        assert C.issues("set_schedule", SCHEDULE, {"run_when": "sometimes"}) == [
            "`set_schedule`: `run_when` is always or has_work"]
        assert C.issues("set_schedule", SCHEDULE, {"wake_on_new_task": "yes"}) == [
            "`set_schedule`: `wake_on_new_task` is true or false"]
        for bad in (-1, MAX_RUNS_PER_DAY + 1, 2.5, True):
            assert C.issues("set_schedule", SCHEDULE, {"max_runs_per_day": bad}) == [
                f"`set_schedule`: `max_runs_per_day` is a whole number from 0 to {MAX_RUNS_PER_DAY:,} (0 is no limit)"]

    def test_the_fields_are_what_update_schedule_sets(self):
        assert C.call(C.OPS["set_schedule"], "s1", {"run_when": "always", "wake_on_new_task": False,
                                                    "max_runs_per_day": 0}, {}) == (
            "update_schedule", {"schedule_id": "s1", "run_when": "always", "wake_on_new_task": False,
                                "max_runs_per_day": 0})

    def test_a_new_schedule_keeps_a_limit_of_zero_and_a_wake_turned_off(self):
        _, args = C.call(C.OPS["create_schedule"], "", {"runs": {"kind": "workflow", "id": "w1"}, "prompt": "Go",
                                                        "interval_value": 15, "interval_unit": "minutes",
                                                        "run_when": "has_work", "wake_on_new_task": False,
                                                        "max_runs_per_day": 0}, None, name="Inbox")
        assert (args["run_when"], args["wake_on_new_task"], args["max_runs_per_day"]) == ("has_work", False, 0)

    def test_the_schedule_tools_take_the_heartbeat(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS

        for name in ("create_schedule", "update_schedule"):
            props = next(t for t in BUILDER_TOOL_SCHEMAS if t["function"]["name"] == name)["function"]["parameters"][
                "properties"]
            assert props["run_when"]["enum"] == list(RUN_WHEN)
            assert props["wake_on_new_task"]["type"] == "boolean"
            assert props["max_runs_per_day"]["type"] == "integer"


class TestAPlansSchedule:
    def _plan(self, **schedule):
        return O.Plan.from_draft({
            "kind": "create", "title": "Support inbox", "summary": "Each request on the board is answered.",
            "steps": [{"id": "answer", "type": "agent", "name": "Answer", "does": "Answers a request",
                       "agent": "Clerk", "prompt": "Answer the open request."}],
            "agents": [{"name": "Clerk", "purpose": "Answers requests", "tools": []}],
            "schedule": {"schedule_type": "interval", "interval_value": 15, "interval_unit": "minutes",
                         "says": "every 15 minutes", "input": "Work the open requests", **schedule}})

    def test_a_heartbeat_reaches_the_schedule_it_makes(self, boards_on):
        compiled = compile_plan(self._plan(run_when="has_work", max_runs_per_day=40))
        assert (compiled.schedule["run_when"], compiled.schedule["max_runs_per_day"]) == ("has_work", 40)
        assert compiled.issues == []

    def test_a_plan_with_no_heartbeat_runs_at_its_time(self):
        compiled = compile_plan(self._plan())
        assert (compiled.schedule["run_when"], compiled.schedule["max_runs_per_day"]) == ("always", 0)

    def test_with_boards_off_a_heartbeat_is_an_issue(self, monkeypatch):
        use_saved_settings(monkeypatch)
        issues = compile_plan(self._plan(run_when="has_work")).issues
        assert any("Boards are off" in i for i in issues)

    def test_a_heartbeat_outside_the_two_is_an_issue(self):
        assert any("run_when" in i for i in O.schedule_issues(self._plan(run_when="when_idle").schedule))

    def test_the_card_says_what_a_quiet_day_costs_and_the_limit(self):
        text = render(self._plan(run_when="has_work", max_runs_per_day=40))
        assert ("**Only when there's work:** at each time it checks its board and runs only while a task is open, "
                "so a quiet day costs nothing; a new task starts it within a minute. At most 40 runs a day.") in text

    def test_a_schedule_that_always_runs_says_nothing_of_it(self):
        assert "Only when there's work" not in render(self._plan())


async def test_a_heartbeat_is_set_and_undone(client, boards_on):
    from core.store import schedules

    flow = await _workflow("Support inbox")
    await _schedule(flow["id"])
    first, checked, done = await _change(_set({"run_when": "has_work", "max_runs_per_day": 40}))
    assert checked["outcome"] == "review", checked
    assert done["applied"] == ["set_schedule"]
    row = await schedules.get("sched_h")
    assert (row["run_when"], row["max_runs_per_day"]) == ("has_work", 40)
    await _change({"undo_of": first.run_id})
    row = await schedules.get("sched_h")
    assert (row["run_when"], row["max_runs_per_day"]) == ("always", 0)


async def test_waiting_for_work_with_boards_off_is_sent_back_before_the_person_sees_it(client, monkeypatch):
    """The planner's submit is where a change's fields meet the save's rules: it hears why, and fixes or asks."""
    use_saved_settings(monkeypatch)
    from core.store import schedules

    flow = await _workflow("Support inbox")
    await _schedule(flow["id"])
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_f1728_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    sent = await _call("submit_change", _set({"run_when": "has_work"}), run)
    assert sent["error"] == ("Fix these and call submit_change again: op 1: `set_schedule`: Waiting for work reads "
                             "its boards, and Boards are off. Turn on Boards in Settings first, or run it at its time.")
    assert (await schedules.get("sched_h")).get("run_when", "always") == "always"


async def test_a_deleted_heartbeat_comes_back_with_it(client, boards_on):
    from core.store import schedules

    flow = await _workflow("Support inbox")
    await _schedule(flow["id"], run_when="has_work", wake_on_new_task=False, max_runs_per_day=12)
    first, _, _ = await _change({"ops": [{"op": "delete_schedule", "target": {"kind": "schedule", "id": "sched_h"}}]})
    await _change({"undo_of": first.run_id})
    [back] = [s for s in await schedules.load() if s["name"] == "Support inbox"]
    assert (back["run_when"], back["wake_on_new_task"], back["max_runs_per_day"]) == ("has_work", False, 12)
    assert back["enabled"] is False


async def test_the_schedules_reader_says_its_heartbeat_in_its_shape(client, boards_on):
    from core.builder_readers import _schedules, shape_problems

    flow = await _workflow("Support inbox")
    await _schedule(flow["id"], run_when="has_work", max_runs_per_day=12)
    answer = await _schedules({})
    [item] = answer["items"]
    assert (item["run_when"], item["wake_on_new_task"], item["max_runs_per_day"]) == ("has_work", True, 12)
    assert shape_problems("schedules", answer) == []


def test_the_cli_sends_the_heartbeat(monkeypatch):
    from synapse.client import commands

    sent = {}

    class _Api:
        def __init__(self, target):
            pass

        def patch(self, path, body):
            sent.update(body)
            return {"id": "sched_h", "name": "Support inbox", **body}

        def close(self):
            pass

    monkeypatch.setattr(commands, "resolve", lambda *a, **k: None)
    monkeypatch.setattr(commands, "ApiClient", _Api)
    args = types.SimpleNamespace(schedule_id="sched_h", cron=None, every=None, name=None, prompt=None,
                                 description=None, missed=None, run_when="has_work", wake="off", max_runs_per_day=40,
                                 json=True, url=None, api_key=None)
    assert commands.schedules_update(args) == 0
    assert sent == {"run_when": "has_work", "wake_on_new_task": False, "max_runs_per_day": 40}
