"""A Browser step's inputs read what the run has (F1232; BRW1, D328; CAP3's stage 1, brw-02 and brw-04).

"When I give you an order number, confirm its delivery with my recording": what the person gives when the run starts
is the run's own input, `{state.user_input}`. The planner was never told so: it added a Human step to ask again for
what the run is given, and a `{state.order_number}` that no step writes would read empty and stop the run at the
Browser step ("needs its inputs") every time.

The palette names what a Browser step's inputs may read. Every builder door holds a `{state.…}` in
`recording_inputs` to what the run has (`save_checks.input_problems`, beside `recording_problems`, F1189's one place):
the run's input, the workflow's declared state, a step's output and a Loop's item. Where the door has the whole
workflow (a plan, a whole save) a step's output is one written before the Browser step, and a Loop's item is the
item of a Loop around it; a door that adds or patches a step mid-build checks against the whole workflow. The app's
editor is unchanged (Naveen, 10-29).
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools
from core.browser import recordings
from core.builder_tools import execute_builder_tool, execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder.browser_steps import palette
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
    await S.seed_agents([S.make_agent(id="agent_web", name="Web", type="browser")])
    await S.seed_orchestrations([S.make_orchestration(id="orch_b")])
    yield (await recordings.create("Confirm a delivery", _rec_steps(), by="you"))["id"]
    builder_access.forget_runs()
    builder_tools.set_browser_check(None)


async def _submit(draft: dict) -> dict:
    run = OrchestrationRun(run_id="builder_brw_in", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("submit_plan", draft, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _plan(*steps, settings: dict | None = None) -> dict:
    return {"kind": "create", "title": "Confirm deliveries", "summary": "Confirms a delivery on the portal.",
            "steps": list(steps), **({"settings": settings} if settings else {})}


def _browser(rec: str, value: str, sid: str = "confirm") -> dict:
    return {"id": sid, "type": "browser", "name": "Confirm the delivery", "does": "Confirms it on the portal",
            "config": {"recording_id": rec, "recording_inputs": {"order number": value}}}


def _say(sid: str) -> dict:
    return {"id": sid, "type": "llm", "name": "Say it", "does": "Says what happened", "prompt": "Say what happened."}


class TestThePaletteSaysWhatAnInputReads:
    async def test_the_run_s_own_input_is_named_and_never_asked_again(self, world):
        text = await palette()
        assert "{{state.user_input}}" in text
        assert "never a human step" in text.lower()

    async def test_a_step_s_output_and_a_loop_s_item_are_named(self, world):
        text = await palette()
        assert "{{state.<step id>}}" in text and "{{state.loop_item}}" in text


class TestThePlanIsHeldToIt:
    async def test_the_run_s_own_input_is_taken(self, world):
        out = await _submit(_plan(_browser(world, "{{state.user_input}}")))
        assert out.get("finish") == "submit_plan", out

    async def test_a_key_nothing_writes_is_sent_back_naming_it_and_what_the_run_has(self, world):
        out = await _submit(_plan(_browser(world, "{state.order_number}")))
        error = out.get("error", "")
        assert "order_number" in error and "user_input" in error, out

    async def test_an_earlier_step_s_output_is_taken_by_a_path_too(self, world):
        # A field of a model's answer is one it declares (F1553), which the run holds it to.
        ask = {"id": "pick", "type": "llm", "name": "Pick the order", "does": "Picks the order number",
               "prompt": "The order number in the message.",
               "config": {"output_fields": [{"name": "order", "type": "text"}]}}
        out = await _submit(_plan(ask, _browser(world, "{{ state.pick.order }}")))
        assert out.get("finish") == "submit_plan", out

    async def test_a_later_step_s_output_is_sent_back(self, world):
        out = await _submit(_plan(_browser(world, "{state.say}"), _say("say")))
        assert "say" in out.get("error", "") and "after" in out.get("error", ""), out

    async def test_a_declared_state_key_is_taken(self, world):
        out = await _submit(_plan(_browser(world, "{state.order}"),
                                  settings={"state_schema": {"order": {"type": "str", "default": ""}}}))
        assert out.get("finish") == "submit_plan", out

    async def test_a_loop_s_item_is_taken_inside_its_body_only(self, world):
        listed = {"id": "orders", "type": "llm", "name": "List the orders", "does": "Lists the order numbers",
                  "prompt": "The order numbers in the message, as a JSON list."}
        loop = {"id": "each", "type": "loop", "name": "Each order", "does": "Each order in turn",
                "loop_over": "orders", "body": ["confirm"]}
        inside = await _submit(_plan(listed, loop, _browser(world, "{{state.loop_item}}"),
                                     settings={"state_schema": {"orders": {"type": "list", "default": []}}}))
        assert inside.get("finish") == "submit_plan", inside
        outside = await _submit(_plan(_browser(world, "{{state.loop_item}}")))
        assert "loop_item" in outside.get("error", ""), outside


async def call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, types.SimpleNamespace(agent_sessions={})))


class TestTheBuilderToolsHoldItToo:
    def _step(self, rec: str, value: str) -> dict:
        return {"step_id": "confirm", "name": "Confirm", "type": "browser", "recording_id": rec,
                "recording_inputs_json": json.dumps({"order number": value})}

    async def test_a_key_nothing_in_the_workflow_writes_is_refused(self, world):
        out = await call("add_single_step", {"orch_id": "orch_b", **self._step(world, "{state.order_number}")})
        assert "order_number" in out.get("error", ""), out

    async def test_a_step_of_the_workflow_is_taken_mid_build(self, world):
        # The seeded workflow's one step writes `greeting`.
        out = await call("add_single_step", {"orch_id": "orch_b", **self._step(world, "{state.greeting}")})
        assert "error" not in out, out
        out = await call("add_single_step", {"orch_id": "orch_b", **{**self._step(world, "{state.user_input}"),
                                                                    "step_id": "again"}})
        assert "error" not in out, out
