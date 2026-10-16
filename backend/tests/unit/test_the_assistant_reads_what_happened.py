"""The assistant reads what happened, not only what is set up (ASSIST1, D291, F680).

Naveen's questions: "what's the status of my workflows", "did the agent finish the job", "what did the other agent
do", "summarise today". `read_workspace` answered what exists and how it is set; these topics answer what has been
going on, from the same records the run page, the bell and Chat read:

- `today`: what ran over a window (a day by default): how many by status, each failure and why, what waits on a
  person, the schedules that ran, what it cost;
- `run`: one run, by its id or its workflow's name (the latest), step by step with the start of what each wrote;
- `waiting`: what waits on a person, each with what answers it;
- `agent_activity`: an agent's recent conversations (the last question and answer, scrubbed of keys) and the
  workflows that use it with how their last run went.

A viewer reads all of it; nothing here writes, and no secret comes back.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import hooks
from core.product_tools.roles import Role

KEY = "sk-" + "proj-" + "Z" * 40


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _ago(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
async def viewer():
    """The assistant's run for a viewer, in a host with roles."""
    async def source(user_id):
        return Role.VIEWER if user_id == "v1" else None

    hooks.set_role_source(source)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_v", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": "v1"}})).checkpoint()
    yield "builder_v"
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _read(as_run=None, **args) -> dict:
    raw, _ = await execute_builder_tool_with_events("read_workspace", args, _Server(), run_id=as_run)
    return json.loads(raw)


async def _run(run_id, orch_id, status, *, started, error=None, cost=0.0, history=None, state=None, **kw):
    run = OrchestrationRun(run_id=run_id, orchestration_id=orch_id, status=status, started_at=started,
                           ended_at=started if status != "running" else None, total_cost_usd=cost,
                           step_history=history or [], shared_state=state or {}, **kw)
    if error:
        run.step_history = [*run.step_history, {"step_id": "s1", "step_name": "Send", "status": "failed",
                                                "error": error}]
    await SharedState(run).checkpoint()


async def _workflow(**kw) -> dict:
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    orch = make_orchestration(**kw)
    await save_orchestration(orch)
    return orch


@pytest.fixture
async def workflows():
    invoices = await _workflow(id="orch_inv", name="Invoice chaser")
    digest = await _workflow(id="orch_dig", name="Morning digest")
    await _run("run_old", "orch_inv", "completed", started=_ago(days=3))
    await _run("run_ok", "orch_inv", "completed", started=_ago(hours=2), cost=0.012,
               history=[{"step_id": "s1", "step_name": "Draft", "step_type": "agent", "status": "completed",
                         "output_key": "draft"}],
               state={"draft": "Dear Acme, your invoice 1042 is 30 days overdue."})
    await _run("run_bad", "orch_dig", "failed", started=_ago(hours=1), error="Slack refused: channel_not_found")
    await _run("run_wait", "orch_dig", "paused", started=_ago(minutes=20), waiting_for_human=True,
               human_prompt="Send the digest?")
    return invoices, digest


async def test_today_says_what_ran_what_failed_and_what_waits(workflows, viewer):
    got = await _read(viewer, topic="today")
    assert "error" not in got, got
    assert got["runs"] == {"completed": 1, "failed": 1, "paused": 1}  # run_old is three days back
    [failed] = got["failed"]
    assert failed["workflow"] == "Morning digest" and "channel_not_found" in failed["why"]
    assert [w["workflow"] for w in got["waiting_for_a_person"]] == ["Morning digest"]
    assert got["model_dollars"] == pytest.approx(0.012)

    week = await _read(viewer, topic="today", days=7)
    assert week["runs"]["completed"] == 2


async def test_a_run_by_its_workflows_name_is_the_latest_step_by_step(workflows, viewer):
    got = await _read(viewer, topic="run", name="invoice")
    assert got["run_id"] == "run_ok" and got["workflow"] == "Invoice chaser" and got["status"] == "completed"
    [step] = got["steps"]
    assert step["step_name"] == "Draft" and step["output"].startswith("Dear Acme")
    assert got["model_dollars"] == pytest.approx(0.012)

    by_id = await _read(viewer, topic="run", run_id="run_bad")
    assert by_id["status"] == "failed" and "channel_not_found" in json.dumps(by_id)


async def test_a_run_that_is_not_the_workspaces_is_not_read(workflows, viewer):
    got = await _read(viewer, topic="run", run_id="builder_v")  # the assistant's own run
    assert "error" in got
    assert "error" in await _read(viewer, topic="run", name="no such workflow")


async def test_waiting_names_what_answers_each(viewer):
    from core.product_tools import approvals

    async def bell():
        return [{"kind": "run_question", "run_id": "run_wait", "title": "Send the digest?",
                 "answer_with": "answer_run"}]

    approvals.set_waiting_source(bell)
    try:
        got = await _read(viewer, topic="waiting")
    finally:
        approvals.set_waiting_source(None)
    assert got["items"][0]["run_id"] == "run_wait" and got["items"][0]["answer_with"] == "answer_run"


async def test_what_an_agent_did_is_its_conversations_and_its_workflows_runs(seed_agent, viewer):
    from core.store import sessions

    bot = await seed_agent(id="agent_support", name="Support bot")
    await _workflow(id="orch_tri", name="Ticket triage", entry_step_id="s1", steps=[
        {"id": "s1", "name": "Triage", "type": "agent", "agent_id": "agent_support", "next_step_id": None}])
    await _run("run_tri", "orch_tri", "completed", started=_ago(hours=3))
    await sessions.append_turn("chat_1", bot["id"], [
        {"role": "user", "content": "Refund order 77?"},
        {"role": "assistant", "content": f"Refunded order 77. (Used key {KEY}.)"}])
    await sessions.append_turn("builder:t:x", bot["id"], [
        {"role": "user", "content": "a builder sub-agent's prompt"}, {"role": "assistant", "content": "x"}])

    got = await _read(viewer, topic="agent_activity", name="support")
    assert got["agent"] == "Support bot"
    [talk] = got["conversations"]
    assert talk["last_question"] == "Refund order 77?" and "Refunded order 77" in talk["last_answer"]
    assert KEY not in json.dumps(got)
    [flow] = got["workflows"]
    assert flow["workflow"] == "Ticket triage" and flow["last_run"]["status"] == "completed"

    assert "error" in await _read(viewer, topic="agent_activity", name="nobody by that name")


async def test_the_new_topics_are_offered_to_the_model():
    from core.builder_readers import SCHEMA, TOPICS

    for topic in ("today", "run", "waiting", "agent_activity"):
        assert topic in TOPICS and topic in SCHEMA["function"]["parameters"]["properties"]["topic"]["enum"]
    assert "run_id" in SCHEMA["function"]["parameters"]["properties"]
