"""The AI builder changes a schedule in place (F578, BWA1, D274).

BWA1's after pass, on both models: "run the nightly schedule at 3am instead" was planned as `delete_schedule` and
`create_schedule`, because the builder could list, create, enable and delete a schedule but not edit one. The
schedule lost its id and its history, and the new one was created paused (F155), so it stopped running.
`update_schedule` patches it as the routes' PATCH does, and never its enabled state.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from _fakes import seed as S
from core.builder_tools import execute_builder_tool
from core.native_builder import bundled_definitions
from core.store import schedules as schedule_store


async def _call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, SimpleNamespace(agent_sessions={})))


async def _nightly() -> dict:
    await S.seed_orchestrations([S.make_orchestration(id="orch_digest", name="Nightly digest"),
                                 S.make_orchestration(id="orch_other", name="Other")])
    made = await _call("create_schedule", {
        "name": "Every night at 2", "target_type": "orchestration", "target_id": "orch_digest", "prompt": "today",
        "schedule_type": "cron", "cron_expression": "0 2 * * *"})
    schedule = await schedule_store.get(made["id"])
    # As a person left it: on, and it has run.
    await schedule_store.save({**schedule, "enabled": True, "last_run_at": "2026-10-14T02:00:00Z"})
    return await schedule_store.get(made["id"])


async def test_its_time_moves_and_it_keeps_its_id_history_and_state():
    before = await _nightly()

    got = await _call("update_schedule", {"schedule_id": before["id"], "cron_expression": "0 3 * * *"})

    after = await schedule_store.get(before["id"])
    assert got["status"] == "updated" and got["changed"] == ["cron_expression"]
    assert after["cron_expression"] == "0 3 * * *"
    assert after["next_run_at"].endswith("T03:00:00Z")
    assert (after["enabled"], after["last_run_at"], after["created_at"]) == (
        True, "2026-10-14T02:00:00Z", before["created_at"])
    assert [s["id"] for s in await schedule_store.load()] == [before["id"]], "the same schedule, not another"


async def test_it_never_turns_a_schedule_on_or_off():
    before = await _nightly()
    await _call("update_schedule", {"schedule_id": before["id"], "prompt": "yesterday", "enabled": False})
    after = await schedule_store.get(before["id"])
    assert after["prompt"] == "yesterday" and after["enabled"] is True


async def test_a_bad_time_or_target_changes_nothing():
    before = await _nightly()
    bad_time = await _call("update_schedule", {"schedule_id": before["id"], "cron_expression": "every day"})
    gone = await _call("update_schedule", {"schedule_id": before["id"], "target_id": "orch_nope"})
    empty = await _call("update_schedule", {"schedule_id": before["id"]})
    missing = await _call("update_schedule", {"schedule_id": "sched_nope", "prompt": "x"})
    assert all("error" in r for r in (bad_time, gone, empty, missing))
    assert await schedule_store.get(before["id"]) == before


async def test_its_target_can_move_to_another_workflow():
    before = await _nightly()
    await _call("update_schedule", {"schedule_id": before["id"], "target_id": "orch_other"})
    assert (await schedule_store.get(before["id"]))["target_id"] == "orch_other"


def test_a_new_time_is_a_change_op_that_moves_it_and_the_planner_is_told_so():
    """The change route moves a schedule with its `set_schedule` op, which is `update_schedule` and a write
    (reviewed), never a delete and a create (SAI2-P2)."""
    from core.native_builder import change_ops

    defs = bundled_definitions()
    op = change_ops.OPS["set_schedule"]
    assert op.tool == "update_schedule" and op.is_write and "cron_expression" in op.fields
    planner = next(a for a in defs["agents"] if a["system_role"] == "planner")
    assert "change it with set_schedule; never delete it and make a new one" in planner["system_prompt"]
