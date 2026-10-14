"""The AI builder checks its plan before the person approves it, and leaves no agents behind a build that stopped
(F492, BWA1, D274).

gpt-oss-120b's BWA1 baseline on cloud dev: 37 of 45 saver runs stopped after the person had approved the plan —
"Tool not found" (the plan named `gmail.send_message`, `smtp_email_tool`, `http_get`…, and the saver's prompt says
to stop) or "Agents are missing" (the agent creator refused the same tools). The agents made for those builds stayed
in the workspace with nothing to run them. These pin:

- `names_in_plan` reads the names the drafter's format puts in a plan: a new agent's `tools: […]`, a tool step's
  forced tool, and only the reused agents' ids (a new agent's placeholder id is not one);
- `check_plan` checks each with the run's own lookup: the first time something is missing the plan is redrafted,
  after that the person reads what is missing in a "Missing here" section on the plan card;
- the builder runs the check between the draft and the review, and its review shows the section;
- `finish_build` removes the agents a run made when it saved no workflow, with the reason, and keeps agents a
  workflow uses.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.builder_guard import check_plan, names_in_plan

PLAN = """## Overview
A weekly report.

## Steps
1. **Fetch recent runs** *(tool)*
   - **Tool:** `list_recent_runs` (`limit: 200`)
2. **Email it** (step type: tool) — forced tool `smtp_email_tool`
3. **Summarise** *(agent)* — **new** `agent_weekly_failed_summary`

## Agents summary
### Existing agents reused
- `reader → agent_01a136be55157353 (Front desk)`
### New agents to create
```
role: weekly
name: Weekly Writer
tools: ["list_agents", get_agent]   # x
```
"""


class _Server:
    agent_sessions: dict = {}


def test_the_plans_tools_and_reused_agents_are_read_from_the_drafters_format():
    assert names_in_plan(PLAN) == {
        "tools": ["list_agents", "get_agent", "list_recent_runs", "smtp_email_tool"],
        "reused_agents": ["agent_01a136be55157353"],
    }


@pytest.mark.parametrize("plan", ["", "## Steps\n1. **Ask** (step type: human)", "**Tool:** `:`"])
def test_a_plan_naming_nothing_has_nothing_to_check(plan):
    assert names_in_plan(plan) == {"tools": [], "reused_agents": []}


async def test_missing_tools_send_the_plan_back_once_then_go_to_the_person():
    first = await check_plan(PLAN, _Server())
    assert first["redraft"] is True and first["checks"] == 1
    # The run's own lookup decides: here a workflow can call the builder's `list_recent_runs`; a host whose runs
    # cannot (cloud narrows its tools) reads it as missing, which is the point of asking that lookup.
    assert first["missing_tools"] == ["smtp_email_tool"]
    assert first["missing_agents"] == ["agent_01a136be55157353"]
    assert "`smtp_email_tool`" in first["note"] and first["section"] == ""

    second = await check_plan(PLAN, _Server(), checks=first["checks"])
    assert second["redraft"] is False
    assert second["section"].startswith("## Missing here\n") and "`smtp_email_tool`" in second["section"]
    assert "the build will stop there" in second["section"]


async def test_a_plan_using_only_what_exists_passes_quietly():
    got = await check_plan("tools: [list_agents]\n**Tool:** `get_agent`", _Server())
    assert (got["redraft"], got["missing_tools"], got["note"], got["section"]) == (False, [], "", "")


def test_the_builder_checks_between_the_draft_and_the_review():
    from core.native_builder import bundled_definitions

    orch = bundled_definitions()["orchestration"]
    steps = {s["id"]: s for s in orch["steps"]}
    assert steps["step_draft_plan"]["next_step_id"] == "step_check_plan"
    assert steps["step_check_plan"]["forced_tool"] == "check_plan"
    assert steps["step_check_plan"]["output_key"] == "plan_check"
    assert steps["step_plan_checked"]["if_true_step_id"] == "step_draft_plan"
    assert steps["step_plan_checked"]["if_false_step_id"] == "step_human_review"
    assert steps["step_human_review"]["human_prompt"].endswith("{state.plan_check.section}")
    assert orch["state_schema"]["plan_check"]["type"] == "dict"
    assert "plan_check" in steps["step_draft_plan"]["input_keys"]


@pytest.mark.parametrize("redraft,goes_to", [(True, "step_draft_plan"), (False, "step_human_review")])
def test_the_check_routes_the_plan_as_the_engine_evaluates_it(redraft, goes_to):
    """Evaluated as the engine does: an unknown name (`true`) is an error, which counts as false — the browser pass
    caught exactly that, the plan going to review with a redraft asked for."""
    from core.native_builder import bundled_definitions
    from core.orchestration.steps import _eval_state_expr

    step = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_plan_checked")
    value, error = _eval_state_expr(step["if_condition"], {"plan_check": {"redraft": redraft}}, default=False)
    assert error is None
    assert (step["if_true_step_id"] if value else step["if_false_step_id"]) == goes_to


async def test_a_far_name_is_not_offered_as_the_nearest():
    got = await check_plan("**Tool:** `smtp_send_mail`", _Server())
    assert got["missing_tools"] == ["smtp_send_mail"] and "nearest" not in got.get("note", "")


def test_the_plan_card_reads_the_needs_you_section():
    from core.orchestration.steps import render_state_refs

    review = render_state_refs("x\n\n---\n\n{state.plan_draft}\n\n{state.plan_check.section}",
                               {"plan_draft": PLAN, "plan_check": {"section": ""}})
    assert review.rstrip().endswith("```"), "no section, nothing added"


class TestFinishBuildLeavesNoOrphans:
    async def _finish(self, monkeypatch, writes: list[dict]) -> tuple[dict, list[dict]]:
        from core import builder_tools
        from core.orchestration import state as state_mod

        async def restore(run_id):
            return SimpleNamespace(run=SimpleNamespace(shared_state={"_run_writes": writes}))

        monkeypatch.setattr(state_mod.SharedState, "restore", staticmethod(restore))
        token = builder_tools._current_run.set("run_f492")
        try:
            raw, events = await builder_tools.execute_builder_tool_with_events(
                "finish_build", {}, _Server(), run_id="run_f492")
        finally:
            builder_tools._current_run.reset(token)
        return json.loads(raw), events

    async def _agent(self, name: str) -> str:
        from core.builder_tools import execute_builder_tool

        made = json.loads(await execute_builder_tool(
            "create_agent", {"name": name, "description": "d", "type": "conversational", "tools": [],
                             "system_prompt": "s"}, _Server()))
        return made.get("id") or made["agent"]["id"]

    async def test_a_build_that_saved_no_workflow_removes_its_agents_and_says_why(self, monkeypatch):
        from core.routes.agents import load_user_agents

        agent_id = await self._agent("Form Fetcher")
        got, events = await self._finish(monkeypatch, [
            {"kind": "agent", "action": "created", "id": agent_id, "name": "Form Fetcher"}])
        assert "Form Fetcher" in got["removed_names"]
        assert agent_id not in {a["id"] for a in await load_user_agents()}
        (gone,) = [e for e in events if e["kind"] == "agent"]
        assert gone["action"] == "deleted" and gone["reason"] == "the build stopped before its workflow was saved"

    async def test_a_build_that_kept_its_workflow_keeps_its_agents(self, monkeypatch):
        from core.routes.agents import load_user_agents

        agent_id = await self._agent("Digest Writer")
        await self._finish(monkeypatch, [
            {"kind": "agent", "action": "created", "id": agent_id, "name": "Digest Writer"},
            {"kind": "orchestration", "action": "created", "id": "orch_kept", "name": "Digest"}])
        assert agent_id in {a["id"] for a in await load_user_agents()}
