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


async def test_a_new_skill_is_reviewed_then_made(client, builder, fake_llm):
    """The hand-over probe's class (10-23): one new thing, made after its review by the maker, not planned as a
    workflow and saved as nothing."""
    from core import skills

    fake_llm.script([route("manage"), tool_call("submit_change", ops=[
        {"op": "create", "target": {"kind": "skill", "name": "Polite replies"},
         "fields": {"what": "answer patients politely, in two sentences or fewer"}}])])
    events = await stream(client, "/api/builder/chat", {"message": "Create a skill called Polite replies"})
    [asked] = [e for e in events if e["type"] == "human_input_required"]
    assert 'Make the skill "Polite replies"' in asked["prompt"]
    assert await skills.get_skill("polite-replies") is None, "nothing is made before the yes"

    fake_llm.reset()
    fake_llm.script([tool_call("create_skill", name="polite-replies", description="Polite, short replies",
                               body="Answer patients politely, in two sentences or fewer."), "Made it."])
    after = await stream(client, "/api/builder/resume", {"run_id": asked["run_id"], "response": {"feedback": "yes"}})
    assert (await skills.get_skill("polite-replies"))["body"].startswith("Answer patients politely")
    assert "polite-replies" in _summary(after).lower() or "Polite" in _summary(after), _summary(after)


async def test_a_maker_that_would_go_on_making_makes_one(client, builder, fake_llm):
    """P2's after-batch, cnt-05 on Gemini 3 Flash: one approved agent, and the maker called create_agent seven times.
    Its step ends at the first create that worked (E2's finish tools), so a second call is never made."""
    from core.store.resources import load_agents

    fake_llm.script([route("manage"), tool_call("submit_change", ops=[
        {"op": "create", "target": {"kind": "agent", "name": "Claude Builder"},
         "fields": {"what": "an agent on Claude Opus that helps build workflows"}}])])
    events = await stream(client, "/api/builder/chat", {"message": "Make an agent called Claude Builder"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    made = dict(name="Claude Builder", description="Helps build workflows", type="conversational", tools=[],
                system_prompt="You help build workflows.")
    fake_llm.reset()
    fake_llm.script([tool_call("create_agent", **made), tool_call("create_agent", **made), "Made it."])
    await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": "approve"}})
    assert [a["name"] for a in await load_agents() if a.get("name") == "Claude Builder"] == ["Claude Builder"]


async def test_a_change_the_planner_has_no_op_for_is_said_in_our_words(client, builder, fake_llm):
    """F958 (P2's dev after-pass, chg-05 on Nova Pro and gpt-oss): "re-point the Patient chat channel" has no op, so
    the planner answered in words twice and the person read "Agent step 'Plan the Change' ended without calling
    submit_change or ask_person or cannot…". The step ends in code's `cannot` instead, said in our words."""
    fake_llm.script([route("manage"), "I'll move the channel to the new agent for you.", "Done, it's moved."])
    events = await stream(client, "/api/builder/chat",
                          {"message": "Point the Patient chat channel at the Triage agent"})

    said = _summary(events)
    # Neutral: the fallback fires on any reply in words, a weak model's "Sure, renamed it" to a change it could make
    # too, so it claims no reason code does not know (SAI2's review, 3).
    assert said == "I couldn't turn that into something I can make or change here. Say it another way, or do it on its own page."
    assert "ended without calling" not in json.dumps(events)
    assert not [e for e in events if e.get("type") in ("orchestration_error", "step_error")]


async def test_a_name_given_before_a_pick_survives_both_pauses(client, builder, fake_llm):
    """SAI2's re-check of F960 (1): what the person answered is kept on the run across its pauses, not only in one
    process's memory. Asked the name, they say "Morning digest"; two workflows match, so a pick pauses it again; their
    "the second one" must not leave the name they gave out as unasked."""
    from core.store.resources import load_orchestrations, save_orchestration
    from tests._fakes.seed import make_orchestration

    for oid, name in (("orch_nd_sales", "Nightly digest for sales"), ("orch_nd_support", "Nightly digest for support")):
        await save_orchestration(make_orchestration(id=oid, name=name))
    rename = {"op": "rename_workflow", "fields": {"name": "Morning digest"}}
    fake_llm.script([route("manage"), tool_call("ask_person", question="What should it be called?", about="what")])
    first = await stream(client, "/api/builder/chat", {"message": "rename the nightly digest"})
    run_id = next(e for e in first if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    fake_llm.script([tool_call("submit_change", ops=[{**rename, "target": {"kind": "workflow",
                                                                           "name": "Nightly digest"}}])])
    second = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"answer": "Morning digest"}})
    assert "Which one do you mean?" in next(e for e in second if e["type"] == "human_input_required")["prompt"]

    fake_llm.reset()
    fake_llm.script([tool_call("submit_change", ops=[{**rename, "target": {"kind": "workflow",
                                                                           "id": "orch_nd_support"}}])])
    third = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"answer": "the second one"}})
    [review] = [e for e in third if e["type"] == "human_input_required"]
    assert review["orch_step_id"] == "step_manage_review", review
    assert 'Rename the workflow "Nightly digest for support" — to "Morning digest"' in review["prompt"]
    assert [o["name"] for o in await load_orchestrations() if o["id"] == "orch_nd_support"] == [
        "Nightly digest for support"], "nothing is renamed before the yes"
