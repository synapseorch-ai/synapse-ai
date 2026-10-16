"""The assistant never acts by itself: it puts up a card, and the person's click does it (ASSIST1, D291, F683).

Naveen: anything a person can do in the app, through the gates the app already has. So `prepare_action` resolves
what was asked to exact ids in code (a workflow by name, a run that still waits, a schedule), checks the person's
role, and shows a card whose buttons each carry a confirmation token bound to that person, that act and those exact
arguments (`product_tools.confirm`). Nothing has happened yet. The click posts the token to `/api/assistant/act`,
which rebuilds the person from the request and calls the same product tool through `registry.invoke`: the role is
checked again there, a token works once, and it does nothing for other arguments.

A run's question is answered on the card through the run's own answer door, as on the run page.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh_claims():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    confirm.set_claim_store(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _assistant_run(user_id=None, run_id="builder_card"):
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": user_id}})).checkpoint()
    return run_id


async def _prepare(args, run_id="builder_card"):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id=run_id)
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _workflow(**kw):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    orch = make_orchestration(**kw)
    await save_orchestration(orch)
    return orch


async def _settled(orch_id):
    """The run the click started, to its end: a run outliving its test's loop would end in an error there."""
    import asyncio

    for _ in range(100):
        runs = await _runs_of(orch_id)
        if runs and all(r.get("status") in ("completed", "failed", "cancelled") for r in runs):
            return runs
        await asyncio.sleep(0.05)
    raise AssertionError(f"the run never ended: {runs}")


async def _runs_of(orch_id):
    from core.orchestration.run_view import list_runs
    return await list_runs(limit=50, orchestration_id=orch_id)


async def test_a_card_does_nothing_until_it_is_pressed(client):
    await _workflow(id="orch_inv", name="Invoice chaser")
    await _assistant_run()

    said, [card] = await _prepare({"action": "run_workflow", "workflow": "invoice chaser", "message": "March"})
    assert "nothing has been done" in said["say"].lower()
    assert card["title"] == 'Run "Invoice chaser"' and not card["danger"]
    [button] = card["buttons"]
    assert button["tool"] == "run_orchestration" and button["arguments"]["orchestration_id"] == "orch_inv"
    assert await _runs_of("orch_inv") == []  # shown, not done

    done = await client.post("/api/assistant/act", json=button)
    assert done.status_code == 200, done.text
    assert done.json()["done"] is True
    await _settled("orch_inv")

    again = await client.post("/api/assistant/act", json=button)
    assert again.status_code == 409 and "already" in again.json()["detail"].lower()


async def test_a_token_does_nothing_for_other_arguments(client):
    await _workflow(id="orch_a", name="Alpha")
    await _workflow(id="orch_b", name="Beta")
    await _assistant_run()
    _, [card] = await _prepare({"action": "run_workflow", "workflow": "Alpha", "message": "x"})
    button = card["buttons"][0]
    forged = {**button, "arguments": {**button["arguments"], "orchestration_id": "orch_b"}}
    refused = await client.post("/api/assistant/act", json=forged)
    assert refused.status_code in (400, 403, 409)
    assert await _runs_of("orch_b") == []


async def test_a_person_without_the_role_gets_no_card(seed_orchestration):
    async def source(user_id):
        return Role.VIEWER

    hooks.set_role_source(source)
    await _workflow(id="orch_inv", name="Invoice chaser")
    await _assistant_run(user_id="v1")
    said, cards = await _prepare({"action": "run_workflow", "workflow": "Invoice chaser", "message": "x"})
    assert cards == [] and "builder" in said["error"] and "viewer" in said["error"]


async def test_what_is_asked_is_resolved_exactly_or_not_at_all():
    await _workflow(id="orch_1", name="Invoice chaser")
    await _workflow(id="orch_2", name="Invoice chaser (EU)")
    await _assistant_run()

    said, cards = await _prepare({"action": "run_workflow", "workflow": "invoice", "message": "x"})
    assert cards == [] and "Invoice chaser (EU)" in said["error"]  # two match: it asks which

    said, cards = await _prepare({"action": "run_workflow", "workflow": "payroll", "message": "x"})
    assert cards == [] and "error" in said


async def test_stopping_a_run_that_has_ended_is_not_offered():
    await _workflow(id="orch_inv", name="Invoice chaser")
    await SharedState(OrchestrationRun(run_id="run_done", orchestration_id="orch_inv", status="completed")).checkpoint()
    await SharedState(OrchestrationRun(run_id="run_live", orchestration_id="orch_inv", status="running")).checkpoint()
    await _assistant_run()

    said, cards = await _prepare({"action": "cancel_run", "run_id": "run_done"})
    assert cards == [] and "completed" in said["error"]

    _, [card] = await _prepare({"action": "cancel_run", "run_id": "run_live"})
    assert card["danger"] and card["buttons"][0]["tool"] == "cancel_run"


async def test_a_tool_call_waiting_gets_allow_and_refuse():
    from core.product_tools import approvals

    async def bell():
        return [{"kind": "tool_approval", "session_id": "sess_1", "approval_id": "ap_1", "tool_name": "send_email",
                 "title": "Support bot wants to run send_email", "answer_with": "answer_tool_approval"}]

    approvals.set_waiting_source(bell)
    await _assistant_run()
    try:
        _, [card] = await _prepare({"action": "answer_tool_call", "approval_id": "ap_1"})
    finally:
        approvals.set_waiting_source(None)
    allow, refuse = card["buttons"]
    assert (allow["label"], refuse["label"]) == ("Allow", "Refuse")
    assert allow["arguments"]["approve"] is True and refuse["arguments"]["approve"] is False
    assert allow["token"] != refuse["token"] and allow["arguments"]["session_id"] == "sess_1"


async def test_a_runs_question_is_answered_through_its_own_door():
    await _workflow(id="orch_inv", name="Invoice chaser")
    await SharedState(OrchestrationRun(run_id="run_q", orchestration_id="orch_inv", status="paused",
                                       waiting_for_human=True, human_prompt="Send these 3 reminders?",
                                       human_fields=[{"name": "approve", "type": "boolean"}])).checkpoint()
    await _assistant_run()
    _, [card] = await _prepare({"action": "answer_run", "run_id": "run_q"})
    assert card["question"]["run_id"] == "run_q" and card["question"]["prompt"] == "Send these 3 reminders?"
    assert card["buttons"] == []  # the card's answer form posts to the run's door, as the run page does


async def test_turning_a_schedule_off_is_a_card_and_the_click_turns_it_off(client):
    from core.store import schedules as store

    await store.save({"id": "sched_n", "name": "Nightly invoices", "enabled": True, "target_type": "orchestration",
                      "target_id": "orch_x", "prompt": "", "schedule_type": "cron", "cron_expression": "0 2 * * *"})
    await _assistant_run()
    _, [card] = await _prepare({"action": "schedule_off", "schedule": "nightly"})
    assert card["title"] == 'Turn off "Nightly invoices"'
    assert (await store.get("sched_n"))["enabled"] is True

    assert (await client.post("/api/assistant/act", json=card["buttons"][0])).status_code == 200
    assert (await store.get("sched_n"))["enabled"] is False


async def test_prepare_action_is_a_card_and_the_reporter_and_operator_hold_it():
    from core.native_builder.seed import bundled_definitions

    assert builder_access.KINDS["prepare_action"] == "card"
    agents = {a["id"]: a for a in bundled_definitions()["agents"]}
    for agent in ("agent_native_builder_reporter", "agent_native_builder_operator"):
        assert "prepare_action" in agents[agent]["tools"]
