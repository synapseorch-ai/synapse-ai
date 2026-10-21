"""A button's act that reaches the change route puts its card up with no plan review, through the engine (F755;
re-pointed at the typed Change by SAI2-P2, D308).

The dev case (BWA1's assist batch, Nova Pro): "Pause the Monday invoices schedule", already off, routed to manage; the
planner wrote `set_schedule_enabled — disables …`; the person was asked to approve a change that changed nothing. Here
the same request through the builder's own chat: the planner finishes with one act, `schedule_off`, and the check
(code) puts its card up through `prepare_action`'s own code, which reads the schedule as it is. Off already: said so,
no card, no review. On: a card, and nothing changes until it is pressed. No model reads the planner's change. A
change with a real write waits for its review, and once approved is made and said.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import seed_native_builder
from core.store import schedules as store

from test_builder_routes import route, stream, tool_call

OFF = tool_call("submit_change", ops=[{"op": "schedule_off", "target": {"kind": "schedule",
                                                                        "name": "Monday invoices"}}])


@pytest.fixture
async def builder():
    await seed_native_builder()


async def _monday(enabled: bool) -> None:
    await store.save({"id": "sched_mon", "name": "Monday invoices", "enabled": enabled, "target_type": "orchestration",
                      "target_id": "orch_x", "prompt": "", "schedule_type": "cron", "cron_expression": "0 9 * * 1"})


def _types(events) -> list[str]:
    return [e.get("type") for e in events]


def _summary(events) -> str:
    return next(e for e in events if e["type"] == "orchestration_complete")["final_state"]["summary"]


async def test_a_schedule_already_off_is_said_to_be_with_no_review_and_no_card(client, builder, fake_llm):
    await _monday(False)
    fake_llm.script([route("manage"), OFF])
    events = await stream(client, "/api/builder/chat", {"message": "Pause the Monday invoices schedule"})

    assert "human_input_required" not in _types(events), "no plan review"
    assert "action_needed" not in _types(events)
    assert _summary(events) == '"Monday invoices" is already off.'
    assert (await store.get("sched_mon"))["enabled"] is False


async def test_a_schedule_that_is_on_gets_a_card_and_stays_on(client, builder, fake_llm):
    await _monday(True)
    fake_llm.script([route("manage"), OFF])
    events = await stream(client, "/api/builder/chat", {"message": "Pause the Monday invoices schedule"})

    assert "human_input_required" not in _types(events)
    [card] = [e for e in events if e.get("type") == "action_needed"]
    assert card["buttons"][0]["tool"] == "set_schedule_enabled" and card["buttons"][0]["arguments"]["enabled"] is False
    assert (await store.get("sched_mon"))["enabled"] is True, "nothing happens until it is pressed"
    assert "A card is up" in _summary(events)
    # The card is code's, made from the planner's op: no model reads the change after it is written.
    assert len(fake_llm.calls) == 2


async def test_a_change_with_a_real_write_still_waits_for_its_review(client, builder, fake_llm):
    await _monday(True)
    fake_llm.script([route("manage"), tool_call("submit_change", ops=[
        {"op": "set_schedule", "target": {"kind": "schedule", "name": "Monday invoices"},
         "fields": {"cron_expression": "0 10 * * 1"}}])])
    events = await stream(client, "/api/builder/chat", {"message": "Move the Monday invoices schedule to 10am"})

    [asked] = [e for e in events if e["type"] == "human_input_required"]
    assert asked["orch_step_id"] == "step_manage_review"
    assert 'Change the schedule "Monday invoices" — cron_expression: 0 10 * * 1' in asked["prompt"]
    assert "action_needed" not in _types(events)
    assert (await store.get("sched_mon"))["cron_expression"] == "0 9 * * 1"


async def test_an_approved_change_is_made_and_said(client, builder, fake_llm):
    await _monday(True)
    fake_llm.script([route("manage"), tool_call("submit_change", ops=[
        {"op": "set_schedule", "target": {"kind": "schedule", "name": "Monday invoices"},
         "fields": {"cron_expression": "0 10 * * 1"}}])])
    events = await stream(client, "/api/builder/chat", {"message": "Move the Monday invoices schedule to 10am"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    after = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": "approve"}})

    assert (await store.get("sched_mon"))["cron_expression"] == "0 10 * * 1"
    saved = [e for e in after if e.get("type") == "resource_saved"]
    assert [(e["kind"], e["id"]) for e in saved] == [("schedule", "sched_mon")]
    assert "Monday invoices" in _summary(after) and "Nothing in your workspace changed" not in _summary(after)
    assert fake_llm.calls == [], "approving and applying call no model"


async def test_a_change_the_person_did_not_approve_is_planned_again_not_made(client, builder, fake_llm):
    await _monday(True)
    fake_llm.script([route("manage"), tool_call("submit_change", ops=[
        {"op": "set_schedule", "target": {"kind": "schedule", "name": "Monday invoices"},
         "fields": {"cron_expression": "0 10 * * 1"}}])])
    events = await stream(client, "/api/builder/chat", {"message": "Move the Monday invoices schedule to 10am"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    fake_llm.script([tool_call("submit_change", ops=[
        {"op": "set_schedule", "target": {"kind": "schedule", "name": "Monday invoices"},
         "fields": {"cron_expression": "0 11 * * 1"}}])])
    after = await stream(client, "/api/builder/resume", {"run_id": run_id,
                                                        "response": {"feedback": "make it 11am instead"}})

    [asked] = [e for e in after if e["type"] == "human_input_required"]
    assert "0 11 * * 1" in asked["prompt"]
    assert "make it 11am instead" in json.dumps(fake_llm.calls[0], default=str)  # the planner read the reply
    assert (await store.get("sched_mon"))["cron_expression"] == "0 9 * * 1"
