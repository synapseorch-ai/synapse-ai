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
- the builder runs the check between the plan and the review, and its review shows what is missing (since SAI2-P3
  in code, `check_build`, on the typed Plan; `check_plan` stays for runs paused on the old definition, F443);
- `finish_build` removes the agents a run made when it saved no workflow, with the reason, and keeps agents a
  workflow uses.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from _fakes import seed as S
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


#: How gpt-4.1-mini names a tool step's tool (F666: 4 of 4 plans on the scratch OSS, 2026-10-16): in the step's
#: prose, on the flow line without backticks, and in the Asks-first and Acts-without-asking bullets — never as
#: `**Tool:** `x``, so the check found nothing missing and the build stopped at the saver after approval.
PROSE_PLAN = """## Steps
3. **Summary Formatting** (type: llm)
   Reads `appointments` and writes `summary_text`.
4. **Post to Teams** (type: tool)
   Posts the `summary_text` to the clinic's Teams channel using the `teams_post_message` tool from the Teams MCP
   server. Writes `post_status`.
5. **Label** (type: tool/agent)
   Buys a label with the `shippo_buy_label` MCP tool.

## Asks you first
- `stripe_create_charge`: charges the deposit; a Human step before it approves it.

## Acts without asking
- `teams_post_message`: Posts the daily summary automatically every weekday at 8am UTC, as requested.

## Flow
```flow
Teams Message Post (tool: teams_post_message)
    ↓
Refund (tool: `stripe_refund`)
    ↓
End (end)
```
"""


def test_a_tool_named_in_prose_on_the_flow_line_or_in_the_approval_lists_is_read():
    assert names_in_plan(PROSE_PLAN)["tools"] == [
        "stripe_refund", "teams_post_message", "shippo_buy_label", "stripe_create_charge"]


@pytest.mark.parametrize("plan", [
    "Reads `appointments` and writes `summary_text`; the `daily_report` workflow runs it.",
    "4. **Post** (type: tool)\n5. **Fetch** (tool/agent)",
    "## Asks you first\nNone.\n\n## Acts without asking\n- None",
    "## Acts without asking\nThe `teams_post_message` step posts without asking.\n\n## Flow\n- `summary_text`: the text",
])
def test_backticked_words_that_are_not_named_as_tools_are_not_read_as_tools(plan):
    assert names_in_plan(plan)["tools"] == []


async def test_the_tools_the_check_step_lists_are_checked_whatever_the_plans_phrasing():
    """The class, not the phrasings (the session manager's point): the check step's model reads the plan and lists
    every tool it uses, so a tool named in words no pattern knows still reaches "Missing here". A listed name the
    plan never mentions is dropped: a model's guess is not the plan's."""
    plan = "## Steps\n2. **Post** (type: tool) — posts through teams_post_message, Microsoft's channel tool."
    assert names_in_plan(plan)["tools"] == []
    got = await check_plan(plan, _Server(), listed=["teams_post_message", "`invented_tool`", 7, "list_agents"])
    assert got["missing_tools"] == ["teams_post_message"] and got["redraft"] is True


#: The redraft Naveen saw in the Find who can shots (2026-10-16): the Teams step went to Open questions, and the
#: overview still names the tool.
MENTIONED_PLAN = """This orchestration posts a summary to Teams once the Teams MCP server and the `teams_post_message`
tool are connected. Until then, the posting step is noted but not implemented with a tool call.

## Steps
1. **Summary Formatting** (type: llm)

## Open questions
- When will the `teams_post_message` tool become available?
"""


async def test_a_tool_only_mentioned_is_named_without_saying_the_build_stops():
    """Naveen (2026-10-16): still flag it — the person asked for it, and Find who can is the next step — but "the build
    will stop there" only for a tool a step, an agent, the flow or the approval lists use. A mention alone asks for no
    redraft: the drafter already did what a redraft asks."""
    got = await check_plan(MENTIONED_PLAN, _Server(), listed=["teams_post_message"])
    assert got["missing_tools"] == ["teams_post_message"] and got["redraft"] is False
    assert got["section"] == "## Missing here\nThis plan mentions `teams_post_message`, which nothing here provides yet."


async def test_a_used_tool_still_stops_the_build_and_a_mention_beside_it_is_named_too():
    plan = MENTIONED_PLAN.replace("1. **Summary Formatting** (type: llm)",
                                  "1. **Post** (type: tool)\n   **Tool:** `smtp_email_tool`")
    first = await check_plan(plan, _Server(), listed=["teams_post_message"])
    assert first["redraft"] is True and "`smtp_email_tool`" in first["note"]
    assert "teams_post_message" not in first["note"], "only what a step uses goes back to the drafter"
    second = await check_plan(plan, _Server(), checks=1, listed=["teams_post_message"])
    assert second["section"] == (
        "## Missing here\nThis plan uses `smtp_email_tool`, which nothing in this workspace provides yet. Connect the "
        "app or service it needs, or tell me to leave that part out; approved as it is, the build will stop there. "
        "It also mentions `teams_post_message`, which nothing here provides yet.")


async def test_a_plan_with_no_sections_reads_every_name_as_used():
    got = await check_plan("Post with the `teams_post_message` tool.", _Server(), checks=1)
    assert "the build will stop there" in got["section"]


def test_the_check_step_hands_the_check_the_planners_own_plan_with_no_model():
    """Since SAI2-P3 nothing reads tool names out of prose: the check is code on the planner's typed finish."""
    from core.builder_guard import CHECK_PLAN_SCHEMA
    from core.native_builder import bundled_definitions

    step = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_check_plan")
    assert step["forced_tool"] == "check_build" and "model_role" not in step
    assert step["tool_arguments"] == {"draft": "{{ state.plan_draft }}"} and step["prompt_template"] == ""
    # The old check stays, unchanged, for a run paused on the old definition (F443).
    assert CHECK_PLAN_SCHEMA["function"]["parameters"]["properties"]["tools"]["type"] == "array"


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


def test_the_builder_checks_between_the_plan_and_the_review():
    from core.native_builder import bundled_definitions

    orch = bundled_definitions()["orchestration"]
    steps = {s["id"]: s for s in orch["steps"]}
    assert steps["step_plan"]["next_step_id"] == "step_check_plan"
    assert steps["step_check_plan"]["output_key"] == "plan_checked"
    assert steps["step_check_plan"]["next_step_id"] == "step_plan_route"
    assert steps["step_plan_route"]["switch_cases"]["review"] == "step_human_review"
    review = steps["step_human_review"]
    assert review["human_prompt"].endswith("{state.plan_checked.review}\n\n{state.plan_checked.say}")
    assert review["input_keys"][0] == "plan_checked"  # its typed context, the card's object (F795)
    assert orch["state_schema"]["plan_checked"]["type"] == "str"  # JSON text, read field by field (F801)
    assert "{{ state.plan_checked.say }}" in steps["step_plan"]["prompt_template"]


@pytest.mark.parametrize("outcome,goes_to", [
    ("review", "step_human_review"), ("ask", "step_human_clarify"), ("research", "step_plan_lookup"),
    ("said", "step_plan_said"), (None, "step_build_failed")])
@pytest.mark.parametrize("as_text", [False, True])
def test_the_check_routes_the_plan_as_the_engine_evaluates_it(outcome, goes_to, as_text):
    """Evaluated as the engine does, on the object as state holds it (JSON text, F801) or as a dict: an outcome it
    cannot read says the check failed and ends; it never falls to the review."""
    from core.native_builder import bundled_definitions
    from core.orchestration.steps import _eval_state_expr

    step = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_plan_route")
    checked = {"object": "plan", **({"outcome": outcome} if outcome else {"error": "boom"})}
    value, error = _eval_state_expr(step["switch_expression"],
                                    {"plan_checked": json.dumps(checked) if as_text else checked}, default=None)
    assert error is None
    assert step["switch_cases"].get(str(value), step["switch_default_step_id"]) == goes_to


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

    async def test_its_clean_up_deletes_as_the_agent_delete_does(self, monkeypatch):
        """Memory goes with the agent; one something else uses stays (F467, F469 — BF5's rule for the builder's deletes)."""
        from core import agent_memory
        from core.routes.agents import load_user_agents

        from core.store.resources import get_agent, save_agent

        lead, member, kept = [await self._agent(n) for n in ("Desk Lead", "Desk Member", "Desk Helper")]
        await save_agent({**await get_agent(lead), "delegate_agent_ids": [member]})
        await agent_memory.write_memory(member, "note", "the door code is 4471")
        await save_agent(S.make_agent(id="agent_theirs", name="Theirs", delegate_agent_ids=[kept]))

        got, events = await self._finish(monkeypatch, [
            {"kind": "agent", "action": "created", "id": a, "name": n}
            for a, n in ((member, "Desk Member"), (lead, "Desk Lead"), (kept, "Desk Helper"))])

        left = {a["id"] for a in await load_user_agents()}
        assert lead not in left and member not in left, "the lead goes first, so its member is free"
        assert kept in left, "an agent of the person's hands work to it"
        assert sorted(got["removed_names"]) == ["Desk Lead", "Desk Member"]
        assert agent_memory.read_memory(member, "note") is None
