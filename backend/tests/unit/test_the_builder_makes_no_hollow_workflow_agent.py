"""The AI builder never makes a hollow workflow agent (F264).

Asked for a three-level workflow, the builder created two orchestrator agents
with `orchestration_id: null` — no child orchestration existed — wired the
parent's steps to them, said "fully materialized", and the first run failed:
"Orchestrator agent … has no orchestration_id". Its agent tools accepted the
type and hard-coded the id to None; nothing could deploy an orchestration as
an agent; the validator never looked at the agent behind a step; and
`POST /api/agents` accepted the same hollow shape.

Now the builder builds one orchestration per build: an orchestrator agent is
made only by deploying an orchestration that exists (`deploy_orchestration`,
the Deploy button's logic), `create_agent(s)` refuse the type with the way to
do it, the validator refuses a step whose agent is missing or runs nothing,
and so does the agents route.
"""
import json
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def call(tool, args):
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, _server()))


def _agent_spec(**kw):
    return {"name": "Fact Checker Orchestrator", "description": "d", "type": "orchestrator",
            "tools": [], "system_prompt": "runs the fact checker", **kw}


class TestTheAgentToolsRefuseAHollowWorkflowAgent:
    async def test_create_agent(self):
        from core.routes.agents import load_user_agents
        result = await call("create_agent", _agent_spec())

        assert "error" in result and "deploy_orchestration" in result["error"]
        assert not [a for a in await load_user_agents() if a.get("type") == "orchestrator"]

    async def test_create_agents_refuses_that_spec_and_makes_the_rest(self):
        from core.routes.agents import load_user_agents
        result = await call("create_agents", {"agents": [
            _agent_spec(),
            {"name": "Writer", "description": "d", "type": "conversational", "tools": [], "system_prompt": "writes"},
        ]})

        assert any("deploy_orchestration" in e["error"] for e in result.get("errors", []))
        made = await load_user_agents()
        assert [a["name"] for a in made] == ["Writer"]


class TestDeployingAnOrchestration:
    async def test_it_makes_the_workflow_agent_once(self):
        orch = S.make_orchestration(id="orch_f264", name="Fact Checker")
        await S.seed_orchestrations([orch])

        first = await call("deploy_orchestration", {"orch_id": "orch_f264"})
        again = await call("deploy_orchestration", {"orch_id": "orch_f264"})

        assert first["status"] == "deployed"
        assert first["agent"]["type"] == "orchestrator" and first["agent"]["orchestration_id"] == "orch_f264"
        assert again == {"status": "already_deployed", "agent_id": first["agent_id"]}

    async def test_an_orchestration_that_does_not_exist(self):
        result = await call("deploy_orchestration", {"orch_id": "orch_nowhere"})
        assert "error" in result


class TestTheValidatorLooksAtTheAgent:
    async def _validate(self, agent_id, *others):
        orch = S.make_orchestration(id="orch_f264v", entry_step_id="call", steps=[
            {"id": "call", "name": "Check facts", "type": "agent", "agent_id": agent_id,
             "prompt_template": "check", "output_key": "checked", "next_step_id": "end"},
            {"id": "end", "name": "End", "type": "end"}])
        # One seeding: it replaces the list, so a child seeded before it was gone,
        # which the validator now notices (F429).
        await S.seed_orchestrations([orch, *others])
        return await call("validate_orchestration", {"orch_id": "orch_f264v"})

    async def test_a_hollow_workflow_agent(self):
        await S.seed_agents([S.make_agent(id="agent_hollow", type="orchestrator", orchestration_id=None)])
        report = await self._validate("agent_hollow")
        assert not report["ok"] and any("agent_hollow" in i and "deploy_orchestration" in i for i in report["issues"])

    async def test_a_missing_agent(self):
        report = await self._validate("agent_nowhere")
        assert not report["ok"] and any("agent_nowhere" in i for i in report["issues"])

    async def test_a_deployed_workflow_agent_passes(self):
        await S.seed_agents([S.make_orchestrator_agent("orch_child", id="agent_deployed")])
        report = await self._validate("agent_deployed", S.make_orchestration(id="orch_child"))
        assert report["ok"], report


class TestTheAgentsRoute:
    async def test_a_hollow_workflow_agent_is_refused(self, client):
        resp = await client.post("/api/agents", json={"id": "agent_route_hollow", "name": "Hollow",
                                                      "description": "d", "type": "orchestrator", "tools": [], "system_prompt": "x"})
        assert resp.status_code == 422
        assert "orchestration" in resp.text.lower()

    async def test_a_deployed_one_is_saved(self, client):
        await S.seed_orchestrations([S.make_orchestration(id="orch_route_ok")])
        resp = await client.post("/api/agents", json={"id": "agent_route_ok", "name": "Ok", "description": "d",
                                                      "type": "orchestrator", "orchestration_id": "orch_route_ok",
                                                      "tools": [], "system_prompt": "x"})
        assert resp.status_code == 200


class TestTheBuilderIsToldHowToNest:
    """Since E3 (F770) a workflow runs another through a Workflow step, by its id; since SAI2-P3 the planner is told so,
    and the save makes the inner workflow first and never a workflow agent for it."""

    def test_the_planner_is_told_and_the_cheatsheet_still_says_extract_json(self):
        from core.native_builder import STEP_TYPE_CHEATSHEET, bundled_definitions
        planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
        assert "`workflow`: runs another workflow" in planner["system_prompt"]
        assert "`inner_workflows`, which are saved first" in planner["system_prompt"]
        assert "**extract_json**" in STEP_TYPE_CHEATSHEET

    def test_a_nested_plan_saves_its_inner_workflow_first_and_runs_it_by_its_id(self):
        import json

        from core.native_builder import objects as O
        from core.native_builder.build_save import save_calls
        plan = O.Plan.from_draft({"kind": "create", "title": "Outer", "summary": "s", "inner_workflows": [
            {"title": "Inner", "does": "x", "steps": [{"id": "a", "type": "llm", "name": "A", "does": "x",
                                                      "prompt": "p"}]}], "steps": [
            {"id": "run_inner", "type": "workflow", "name": "Run it", "does": "x", "workflow": "Inner"}]})
        inner, outer = save_calls(plan, {}, {})
        assert (inner["tool"], inner["name"]) == ("create_orchestration", "Inner")
        assert outer["tool"] == "create_orchestration"
        [step] = [s for s in json.loads(outer["args"]["steps_json"]) if s["id"] == "run_inner"]
        assert step["type"] == "workflow" and step["workflow_id"] == inner["makes"]

    def test_no_builder_agent_makes_a_workflow_agent(self):
        from core.native_builder import bundled_definitions
        assert not [a["id"] for a in bundled_definitions()["agents"] if "deploy_orchestration" in a.get("tools", [])]
