"""A chart the person asks for has its route in the plan (F1288, CAP3's pass 2, brief 4; 79 11-02).

Brief 4's plan worked last week's sales by region out right, but nothing in it could save a file, and its agent said
it could not save the chart. A chart here is a CSV in the Vault, which the Vault draws (F1204); a board note links it
as `@[path]`. Now the planner's pack says so when the person asks for a chart, and a plan for one with nothing that
writes the Vault (`vault_write`: a Tool step on it, or an agent given it) is sent back; with no `vault_write` in the
workspace at all, the planner is told to say it cannot.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, charts
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


ASK = "Every Monday, show last week's sales by region as a bar chart."
TOTALS = {"id": "totals", "type": "llm", "name": "Total it", "does": "Totals the sales by region",
          "prompt": "Total last week's sales by region as CSV.", "next": "end"}
SAVE = {"id": "save", "type": "tool", "name": "Save the chart", "does": "Saves the CSV the Vault draws",
        "tool": "vault_write", "config": {"tool_arguments": {"path": "reports/sales-by-region.csv",
                                                             "content": "{{ state.totals }}"}}, "next": "end"}


def _plan(steps, agents=()) -> dict:
    return {"kind": "create", "title": "Sales chart", "summary": "Charts last week's sales by region.",
            "steps": list(steps), "agents": list(agents)}


def _ws(*tools: str) -> build_plan.Workspace:
    return build_plan.Workspace(tools=[{"name": t, "does": f"{t} here"} for t in tools])


async def _chart_errors(draft: dict, ws: build_plan.Workspace, message: str = ASK) -> list[str]:
    found = await build_plan.examine(draft, _Server(), ws=ws, state={"user_message": message})
    return [e for e in found.errors if e.startswith("chart:")]


class TestAsked:
    @pytest.mark.parametrize("message", [ASK, "Plot signups a day", "Graphs of our ticket volume, please",
                                         "Visualise churn by month"])
    def test_a_chart_asked_for_in_their_words(self, message):
        assert charts.asked(message)

    @pytest.mark.parametrize("message", ["Summarise last week's sales", "Build a knowledge graph of our docs", ""])
    def test_no_chart_is_none(self, message):
        assert not charts.asked(message)


class TestThePlanHasItsRoute:
    async def test_a_chart_with_nothing_that_writes_the_vault_is_sent_back_with_how(self, client):
        said = await _chart_errors(_plan([TOTALS]), _ws("vault_write"))
        assert said and "nothing in this plan writes the Vault" in said[0] and "`vault_write`" in said[0], said

    async def test_a_tool_step_on_the_vaults_write_is_its_route(self, client):
        assert await _chart_errors(_plan([{**TOTALS, "next": "save"}, SAVE]), _ws("vault_write")) == []

    async def test_an_agent_given_the_vaults_write_is_its_route(self, client):
        step = {"id": "chart", "type": "agent", "name": "Chart it", "does": "Totals and saves the chart",
                "agent": "Sales clerk", "prompt": "Total and save.", "next": "end"}
        clerk = {"name": "Sales clerk", "purpose": "Totals sales", "tools": ["vault_write"]}
        assert await _chart_errors(_plan([step], [clerk]), _ws("vault_write")) == []

    async def test_with_no_vault_write_here_the_planner_is_told_to_say_it_cannot(self, client):
        said = await _chart_errors(_plan([TOTALS]), _ws())
        assert said and "finish with `cannot`" in said[0], said

    async def test_no_chart_asked_for_is_no_rule(self, client):
        assert await _chart_errors(_plan([TOTALS]), _ws("vault_write"), "Total last week's sales by region") == []


class TestThePackSaysHow:
    async def _pack(self, message: str) -> dict:
        builder_access.forget_runs()
        run = OrchestrationRun(run_id=f"builder_chart_{abs(hash(message)) % 10_000}",
                               orchestration_id="orch_native_builder",
                               shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                             "user_message": message})
        await SharedState(run).checkpoint()
        with live(run):
            raw, _ = await execute_builder_tool_with_events("read_plan_context", {}, _Server(), run_id=run.run_id)
        return json.loads(raw) if isinstance(raw, str) else raw

    async def test_asked_for_it_carries_how_a_chart_lands(self, client):
        assert (await self._pack(ASK)).get("chart") == charts.HOW

    async def test_not_asked_for_no_key(self, client):
        assert "chart" not in await self._pack("Total last week's sales by region")


def test_the_planner_follows_it():
    from core.native_builder.seed import bundled_definitions

    planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
    building = planner["system_prompt"].split("# Building", 1)[1].split("# Steps", 1)[0]
    assert "9. A chart the person asks for" in building and "`chart`" in building and "`vault_write`" in building
