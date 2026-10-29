"""Synapse AI's planner builds a Browser step from a recording (F1193; BRW1, D328).

The planner is shown the workspace's recordings (`native_builder.browser_steps.palette`, in the run's `browser_steps`
state key beside `knowledge_steps`) and writes a Browser step as the engine's own fields in its `config`; its Browser
agent, for a page that changed, by id as `agent`. The plan is held to what every builder door holds a Browser step to
(F1189): the recording is there, its inputs are filled, its sends are its own steps, the Browser is on the plan.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.browser import recordings
from core.models_orchestration import OrchestrationRun
from core.native_builder import objects as O
from core.native_builder.browser_steps import palette
from core.native_builder.compile import compile_plan, plan_from_workflow
from core.orchestration.engine import live
from core.orchestration.state import SharedState

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order"}


def _rec_steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": "https://portal.saltmarsh.example/orders"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"}},
            {"id": "s3", "action": "click", "target": {"role": "button", "name": "Save"}}]


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def world():
    builder_access.forget_runs()
    await S.seed_agents([S.make_agent(id="agent_web", name="Web", type="browser"),
                         S.make_agent(id="agent_desk", name="Front desk")])
    yield (await recordings.create("Confirm a delivery", _rec_steps(), by="you"))["id"]
    builder_access.forget_runs()
    builder_tools.set_browser_check(None)


async def _submit(draft: dict) -> dict:
    run = OrchestrationRun(run_id="builder_brw", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("submit_plan", draft, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _plan(*steps) -> dict:
    return {"kind": "create", "title": "Confirm deliveries", "summary": "Confirms a delivery date on the portal.",
            "steps": list(steps)}


def _browser(rec: str, **config) -> dict:
    return {"id": "confirm", "type": "browser", "name": "Confirm the date", "does": "Confirms it on the portal",
            "config": {"recording_id": rec, "recording_inputs": {"order number": "{state.user_input}"}, **config}}


class TestThePalette:
    async def test_it_lists_each_recording_with_its_inputs_and_step_ids(self, world):
        text = await palette()
        assert f"`{world}` \"Confirm a delivery\"" in text and "inputs: order number" in text
        assert "[s3] Click Save" in text and "**browser**" in text

    async def test_a_workspace_without_a_recording_is_offered_no_browser_step(self, world):
        await recordings.delete(world)
        assert await palette() == ""

    def test_the_planner_s_prompt_reads_it(self):
        from core.native_builder.seed import bundled_definitions
        orch = bundled_definitions()["orchestration"]
        assert "browser_steps" in orch["state_schema"]
        plan_step = next(s for s in orch["steps"] if s["id"] == "step_plan")
        assert "{state.knowledge_steps}{state.browser_steps}" in plan_step["prompt_template"]


class TestTheCompiler:
    def test_a_browser_step_is_the_engine_s_fields_and_reads_back_the_same(self, world):
        plan = O.Plan.model_validate(_plan({**_browser(world, may_send_steps=["s3"], on_miss="agent"),
                                            "agent": "agent_web"}))
        compiled = compile_plan(plan, existing_agents={"agent_web": {"id": "agent_web", "type": "browser"}})
        step = next(s for s in compiled.orchestration["steps"] if s["type"] == "browser")
        assert step["recording_id"] == world and step["recording_inputs"] == {"order number": "{state.user_input}"}
        assert step["may_send_steps"] == ["s3"] and step["on_miss"] == "agent" and step["agent_id"] == "agent_web"
        assert step["output_key"] == step["id"], "what it reads is under its id, as every result step's"
        back = next(s for s in plan_from_workflow(compiled.orchestration).steps if s.type == "browser")
        assert back.agent == "agent_web" and back.config["recording_id"] == world
        assert back.config["may_send_steps"] == ["s3"]


class TestThePlanIsHeldToTheBuilder:
    async def test_a_whole_browser_step_is_taken(self, world):
        out = await _submit(_plan(_browser(world)))
        assert out.get("finish") == "submit_plan", out

    async def test_a_recording_that_is_not_there_is_sent_back(self, world):
        out = await _submit(_plan(_browser("rec_00000000000000000000")))
        assert "No such recording: rec_00000000000000000000" in json.dumps(out), out

    async def test_an_input_left_unfilled_is_sent_back_naming_it(self, world):
        step = _browser(world)
        step["config"]["recording_inputs"] = {}
        out = await _submit(_plan(step))
        assert "order number" in out.get("error", ""), out

    async def test_a_send_that_is_not_the_recording_s_is_sent_back(self, world):
        out = await _submit(_plan(_browser(world, may_send_steps=["s9"])))
        assert "s9" in out.get("error", ""), out

    async def test_an_agent_to_find_the_way_must_be_a_browser_agent(self, world):
        out = await _submit(_plan({**_browser(world, on_miss="agent"), "agent": "agent_desk"}))
        assert "pick a Browser" in out.get("error", ""), out

    async def test_the_browser_off_the_plan_is_said(self, world):
        async def check(kind):
            return "Browser agents are not available yet."

        builder_tools.set_browser_check(check)
        out = await _submit(_plan(_browser(world)))
        assert "Browser agents are not available yet." in json.dumps(out), out
