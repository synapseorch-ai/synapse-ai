"""A build that stops at a tool nothing here provides says which tool, and what to connect (F768, BWA1, D274).

BWA1's self-hosted measure (2026-10-20, Gemini Flash, res-08 "send our monthly newsletter", Mailchimp): the plan kept
the Mailchimp step (F763), the person approved, the saver found no `send_mailchimp_campaign` and stopped ("Tool not
found"), and the person read only "Nothing in your workspace changed." — not which tool, nor what to connect. That is
the build waiting until the app is connected, as Naveen chose (F763's b); the words must say so. `finish_build`, the
code step every build ends in, reads the run's own record: no workflow saved, and the plan check still names tools
nothing provides, so the summary says them, with the provider the research found.
"""
from __future__ import annotations

import json

import pytest

from core.models_orchestration import OrchestrationRun
from core.orchestration import run_writes
from core.orchestration.state import SharedState

WHY = "Nothing was built: this plan uses `send_mailchimp_campaign`, which nothing in this workspace provides yet."


def test_a_stopped_build_is_said_instead_of_nothing_changed():
    assert run_writes.render([], None, WHY) == WHY
    removed = [{"kind": "agent", "action": "created", "id": "a1", "name": "Mailer"},
               {"kind": "agent", "action": "deleted", "id": "a1", "name": "Mailer", "reason": "the build stopped"}]
    both = run_writes.render(removed, None, WHY)
    assert both.startswith('Removed the agent "Mailer" again') and both.endswith(WHY)
    assert run_writes.render([], None, None) == run_writes.NOTHING_CHANGED


def test_the_record_keeps_the_reason_and_re_renders():
    state: dict = {}
    run_writes.note_stopped(state, {"type": "build_stopped", "reason": WHY})
    assert state[run_writes.TEXT_KEY] == WHY
    run_writes.record(state, {"kind": "agent", "action": "created", "id": "a2", "name": "Reader"})
    assert WHY in state[run_writes.TEXT_KEY] and '"Reader"' in state[run_writes.TEXT_KEY]


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _finish(state: dict) -> tuple[dict, list[dict]]:
    from core.builder_tools import _current_run, execute_builder_tool_with_events

    run_id = "builder_f768" + str(abs(hash(json.dumps(state, sort_keys=True))))[:8]
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                       shared_state=state)).checkpoint()
    token = _current_run.set(run_id)
    try:
        raw, events = await execute_builder_tool_with_events("finish_build", {}, _Server(), run_id=run_id)
    finally:
        _current_run.reset(token)
    return json.loads(raw), events


MISSING = {"plan_check": {"checks": 2, "missing_tools": ["send_mailchimp_campaign"], "missing_agents": []},
           "plan_findings": {"options": [{"provider": "Mailchimp", "how": "api",
                                          "sources": ["https://mailchimp.com/developer/"]}]},
           "plan_draft": "## Steps\n1. **Send** (tool step) — forced tool `send_mailchimp_campaign` (Mailchimp)."}


async def test_a_build_that_saved_nothing_names_the_missing_tool_and_its_provider():
    out, events = await _finish(MISSING)
    [stopped] = [e for e in events if e.get("type") == "build_stopped"]
    for said in (out["stopped"], stopped["reason"]):
        assert said.startswith("Nothing was built:") and "`send_mailchimp_campaign`" in said
        assert "Mailchimp" in said and "https://mailchimp.com/developer/" in said
        assert "build it again" in said


async def test_a_build_that_saved_its_workflow_says_nothing_more():
    built = {**MISSING, run_writes.WRITES_KEY: [{"kind": "orchestration", "action": "created", "id": "orch_ok",
                                                  "name": "Newsletter"}]}
    out, events = await _finish(built)
    assert "stopped" not in out and not [e for e in events if e.get("type") == "build_stopped"]


async def test_a_build_with_nothing_missing_is_left_as_it_was():
    out, events = await _finish({"plan_check": {"checks": 1, "missing_tools": [], "missing_agents": []}})
    assert "stopped" not in out and not [e for e in events if e.get("type") == "build_stopped"]
