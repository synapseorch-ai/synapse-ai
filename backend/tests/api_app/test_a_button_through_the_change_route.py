"""A button's act that reaches the change route puts its card up with no plan review, through the engine (F755).

The dev case (BWA1's assist batch, Nova Pro): "Pause the Monday invoices schedule", already off, routed to manage; the
planner wrote `set_schedule_enabled — disables …`; the person was asked to approve a change that changed nothing. Here
the same plan, through the builder's own chat: the gate after the plan sends it to the answer, whose `prepare_action`
reads the schedule as it is. Off already: said so, no card, no review. On: a card, and nothing changes until it is
pressed. A plan with a real change still waits for its review.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import seed_native_builder
from core.store import schedules as store

from test_builder_routes import route, stream, tool_call

PLAN = '**Plan**\n- `set_schedule_enabled — Disables the "Monday invoices" schedule`'


@pytest.fixture
async def builder():
    await seed_native_builder()


async def _monday(enabled: bool) -> None:
    await store.save({"id": "sched_mon", "name": "Monday invoices", "enabled": enabled, "target_type": "orchestration",
                      "target_id": "orch_x", "prompt": "", "schedule_type": "cron", "cron_expression": "0 9 * * 1"})


def _types(events) -> list[str]:
    return [e.get("type") for e in events]


async def test_a_schedule_already_off_is_said_to_be_with_no_review_and_no_card(client, builder, fake_llm):
    await _monday(False)
    fake_llm.script([route("manage"), PLAN,
                     tool_call("prepare_action", action="schedule_off", schedule="Monday invoices"),
                     "The Monday invoices schedule is already off."])
    events = await stream(client, "/api/builder/chat", {"message": "Pause the Monday invoices schedule"})

    assert "human_input_required" not in _types(events), "no plan review"
    assert "action_needed" not in _types(events)
    assert "already off" in json.dumps(events)
    assert (await store.get("sched_mon"))["enabled"] is False


async def test_a_schedule_that_is_on_gets_a_card_and_stays_on(client, builder, fake_llm):
    await _monday(True)
    fake_llm.script([route("manage"), PLAN,
                     tool_call("prepare_action", action="schedule_off", schedule="Monday invoices"),
                     "Press Turn off on the card to pause it."])
    events = await stream(client, "/api/builder/chat", {"message": "Pause the Monday invoices schedule"})

    assert "human_input_required" not in _types(events)
    [card] = [e for e in events if e.get("type") == "action_needed"]
    assert card["buttons"][0]["tool"] == "set_schedule_enabled" and card["buttons"][0]["arguments"]["enabled"] is False
    assert (await store.get("sched_mon"))["enabled"] is True, "nothing happens until it is pressed"
    # The answer acts on the button the plan named, not on its own reading of the question: the plan is in its prompt.
    reporter = json.dumps(fake_llm.calls[2], default=str, ensure_ascii=False)
    assert 'Disables the \\"Monday invoices\\" schedule' in reporter, reporter[:400]


async def test_a_plan_with_a_real_change_still_waits_for_its_review(client, builder, fake_llm):
    await _monday(True)
    fake_llm.script([route("manage"), '- `update_schedule — move "Monday invoices" to 10am`', route("writes")])
    events = await stream(client, "/api/builder/chat", {"message": "Move the Monday invoices schedule to 10am"})

    assert "human_input_required" in _types(events)
    assert "action_needed" not in _types(events)
