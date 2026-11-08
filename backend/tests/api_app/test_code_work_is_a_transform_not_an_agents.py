"""Work that is code is a Transform step, not an agent running `execute_python` (F1291, CAP3's cap-04; 79 11-02).

Cap-04's plans put the week's filter, the totals by region and the chart's numbers inside new agents holding
`execute_python`, in a workspace that runs Transform steps: a model doing arithmetic it could get wrong, at a model's
price, where code does it once and exactly. Code can tell the shape: a new agent the plan makes whose tools include
`execute_python`, where code steps run. It is a structure finding (D340 D3): sent back once, then said on the card,
never a reason a plan cannot be built. An agent already here is the person's, and left alone; where code steps do not
run, `execute_python` is the only way, and left alone too.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, objects as O, structure
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


ANALYSE = {"id": "analyse", "type": "agent", "name": "Total by region", "does": "Totals last week's sales by region",
           "agent": "CSV Analyzer", "prompt": "Total the sales in {{ state.read }} by region.", "next": "end"}
ANALYZER = {"name": "CSV Analyzer", "purpose": "Totals sales", "tools": ["execute_python", "vault_read"]}


def _plan(**more) -> O.Plan:
    return O.Plan.from_draft({"kind": "create", "title": "Sales by region", "summary": "Totals sales by region.",
                              "steps": [ANALYSE], "agents": [ANALYZER], **more})


def _found(found: list[str]) -> list[str]:
    return [f for f in found if "`execute_python`" in f]


class TestTheShape:
    async def test_a_new_agent_computing_with_execute_python_is_said(self, client):
        said = _found(await structure.issues(_plan(), build_plan.Workspace(code_steps=True)))
        assert said and '"CSV Analyzer"' in said[0] and "`transform`" in said[0], said

    async def test_where_code_steps_do_not_run_it_is_the_only_way(self, client):
        assert _found(await structure.issues(_plan(), build_plan.Workspace(code_steps=False))) == []

    async def test_an_agent_already_here_is_the_persons(self, client):
        plan = O.Plan.from_draft({"kind": "create", "title": "Sales", "summary": "S.", "reuse_agents": ["agent_csv"],
                                  "steps": [{**ANALYSE, "agent": "agent_csv"}]})
        ws = build_plan.Workspace(code_steps=True, agents={"agent_csv": {"name": "CSV", "tools": ["execute_python"]}})
        assert _found(await structure.issues(plan, ws)) == []

    async def test_an_agent_without_it_is_left_alone(self, client):
        plan = O.Plan.from_draft({"kind": "create", "title": "Sales", "summary": "S.", "steps": [ANALYSE],
                                  "agents": [{**ANALYZER, "tools": ["vault_read"]}]})
        assert _found(await structure.issues(plan, build_plan.Workspace(code_steps=True))) == []


class TestOnceThenTheCard:
    async def test_sent_back_once_then_a_note(self, client, monkeypatch):
        async def workspace(_server):
            return build_plan.Workspace(code_steps=True)

        monkeypatch.setattr(build_plan, "workspace", workspace)
        builder_access.forget_runs()
        run = OrchestrationRun(run_id="builder_f1291", orchestration_id="orch_native_builder",
                               shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                             "user_message": "Work out last week's sales by region."})
        await SharedState(run).checkpoint()
        draft = {"kind": "create", "title": "Sales by region", "summary": "Totals sales by region.",
                 "steps": [ANALYSE], "agents": [ANALYZER]}

        async def submit():
            with live(run):
                raw, _ = await execute_builder_tool_with_events("submit_plan", draft, _Server(), run_id=run.run_id)
            return json.loads(raw) if isinstance(raw, str) else raw

        first = await submit()
        assert "`execute_python`" in str(first.get("error")), first
        again = await submit()
        assert "`execute_python`" not in str(again.get("error") or ""), "said once: the planner's to keep"
        builder_access.forget_runs()
