"""Every setting the agent editor has is in Synapse AI's reach, or says why not (F1259; the class guard).

`agent_census.SETTINGS` holds one row per setting: the plan field that sets it on a new agent and the change op that
sets it on one that exists, or why it stays the editor's (said to the person), or that code sets it. The walk goes
both ways: every field of `core.models.Agent` (and its team, browser and boards parts) has a row, and every field of
`objects.NewAgent` is a row's plan field or a plan's own reasoning. A setting added to either without a row fails here.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules, as at boot
from core.models import Agent, AgentBoards, AgentBrowser, AgentTeam
from core.native_builder import agent_census as census, change_ops as C
from core.native_builder.objects import NewAgent, Plan

PARTS = {"team": AgentTeam, "browser": AgentBrowser, "boards": AgentBoards}


def _agent_fields() -> list[str]:
    out = list(Agent.model_fields)
    for part, model in PARTS.items():
        out += [f"{part}.{f}" for f in model.model_fields]
    return out


@pytest.mark.parametrize("field", _agent_fields())
def test_each_agent_setting_has_a_row(field):
    assert field in census.by_field(), f"{field}: no row in agent_census.SETTINGS"


@pytest.mark.parametrize("row", census.SETTINGS, ids=lambda r: r.field)
def test_each_row_is_reached_or_says_why_not(row):
    if row.code:
        return
    assert row.section, row
    assert (row.plan and row.op) or row.cant, f"{row.field}: a plan field and an op, or why it stays the editor's"
    if row.plan:
        assert row.plan in NewAgent.model_fields, (row.field, row.plan)
    if row.op:
        assert row.op in C.OPS and C.OPS[row.op].target == "agent", (row.field, row.op)


@pytest.mark.parametrize("field", list(NewAgent.model_fields))
def test_each_plan_field_is_a_setting_or_the_plans_own(field):
    plans = {row.plan for row in census.SETTINGS if row.plan}
    assert field in plans or field in census.PLAN_ONLY or census.PLAN_PARTS.get(field) in census.by_field(), \
        f"NewAgent.{field}: no row says which setting it is"


def test_a_cant_is_said_in_the_persons_words():
    for row in census.SETTINGS:
        if row.cant:
            assert row.cant.endswith(".") and row.section.lower() in row.cant.lower() or "made" in row.cant, row


def test_the_editors_own_model_decides_a_whole_agent():
    good = {"id": "a1", "name": "Desk", "description": "d", "tools": [], "system_prompt": "s"}
    assert census.editor_problem(good) is None
    assert "turn_limit_mode" in census.editor_problem({**good, "turn_limit_mode": "sometimes"})
    assert "team" in census.editor_problem({**good, "team": {"mode": "team", "at_once": 0}})


# ── a plan sets them on a new agent ──────────────────────────────────────────────────────────────────────────────

def _plan(agent: dict, **over):
    raw = {"kind": "create", "title": "Desk", "summary": "Answers the desk.",
           "steps": [{"id": "answer", "type": "agent", "name": "Answer", "does": "Answers it", "agent": agent["name"],
                      "prompt": "Answer the question."}], "agents": [agent], **over}
    return Plan.from_draft(raw)


def _compiled(plan) -> dict:
    from core.native_builder.build_plan import placeholder_mint
    from core.native_builder.compile import compile_plan

    [agent] = compile_plan(plan, mint=placeholder_mint()).agents
    return agent


def test_a_team_with_its_members_way_of_working_and_helpers():
    from core.native_builder.render import setting_lines

    plan = _plan({"name": "Crew", "purpose": "Runs the desk", "type": "team", "members": ["Billing"],
                  "team": {"mode": "team", "at_once": 4, "spend_limit_usd": 2}, "helpers": True, "max_turns": 40,
                  "turn_limit_mode": "stop", "reasoning_effort": "high", "max_output_tokens": 4096})
    plan.refs["agent:Billing"] = "agent_billing"
    crew = _compiled(plan)
    assert crew["type"] == "delegate" and crew["delegate_agent_ids"] == ["agent_billing"], crew
    assert (crew["team"]["mode"], crew["team"]["at_once"], crew["team"]["spend_limit_usd"]) == ("team", 4, 2)
    assert "spawn_subtask" in crew["tools"]
    assert (crew["max_turns"], crew["turn_limit_mode"], crew["reasoning_effort"], crew["max_output_tokens"]) == (
        40, "stop", "high", 4096)
    assert census.editor_problem(crew) is None, census.editor_problem(crew)
    lines = setting_lines(plan.agents[0])
    assert "Team: “Billing”, up to 4 at once, within $2.00" in lines, lines


def test_a_code_agent_with_its_repositories_and_a_browser_agents_recordings():
    plan = _plan({"name": "Coder", "purpose": "Fixes the shop", "type": "code", "repos": ["Shop site"]})
    plan.refs["repo:Shop site"] = "repo_shop"
    assert _compiled(plan)["repos"] == ["repo_shop"]
    plan = _plan({"name": "Runner", "purpose": "Reads the portal", "type": "browser",
                  "browser": {"sites": ["portal.example.com"], "recordings": ["Log in"], "action_budget": 60}})
    plan.refs["recording:Log in"] = "rec_login"
    runner = _compiled(plan)
    assert runner["browser"]["recordings"] == ["rec_login"] and runner["browser"]["action_budget"] == 60, runner


async def test_a_setting_on_the_wrong_kind_of_agent_is_an_issue(client):
    from core.native_builder.build_plan import _settings_issues

    plan = _plan({"name": "Desk", "purpose": "Answers", "repos": ["Shop site"]})
    issues = await _settings_issues(plan, plan.agents[0])
    assert any("repos: only a code agent has it" in i for i in issues), issues
