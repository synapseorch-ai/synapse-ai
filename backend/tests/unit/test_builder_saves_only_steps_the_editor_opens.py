"""The builder's step tools save only a step the engine's own model reads (F1047; HOT2, D323).

On prod, rc4.3's Create Saver wrote a human step's form fields as `{"fields": [...]}`, an object where the step holds a
list. `add_single_step` saved it as given, and the workflow then broke the editor's page on open (F1054). Each flat tool
that writes steps now refuses a step `StepConfig` would not load: the refusal names the step, the field and the shape
it needs, so the model writes it again, and nothing is saved. Over MCP and the CLI the same words come back as a
refusal, never a server error.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool

# The step as prod saved it (HOT2's read, 10-27), its values cut to their shape.
PROD_FIELDS = {"fields": [{"name": "api_key", "type": "str"}, {"name": "api_secret_key", "type": "str"}]}
HUMAN_ARGS = {
    "orch_id": "orch_h", "step_id": "step_generate_api_keys", "name": "Generate API Keys", "type": "human",
    "human_prompt": "Please generate and input your Twitter API keys.", "output_key": "keys",
    "human_fields_json": json.dumps(PROD_FIELDS), "next_step_id": None,
}


async def call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, types.SimpleNamespace(agent_sessions={})))


async def _steps(orch_id: str) -> dict:
    from core.routes.orchestrations import load_orchestrations
    orch = next(o for o in await load_orchestrations() if o["id"] == orch_id)
    return {s["id"]: s for s in orch["steps"]}


@pytest.fixture
async def orch():
    await S.seed_orchestrations([S.make_orchestration(id="orch_h")])
    return "orch_h"


def _says_the_field_and_its_shape(error: str, step_id: str, field: str, shape: str) -> None:
    assert error.startswith("Refusing to save"), error
    assert step_id in error and f"`{field}`" in error and shape in error, error


class TestAMisshapenStepIsRefused:
    async def test_prod_s_human_fields_object_is_refused_and_nothing_is_saved(self, orch):
        result = await call("add_single_step", HUMAN_ARGS)
        _says_the_field_and_its_shape(result["error"], "step_generate_api_keys", "human_fields", "a list")
        assert "an object" in result["error"]
        assert list(await _steps(orch)) == ["step_1"]

    async def test_add_multiple_steps_refuses_it_too(self, orch):
        step = {k: v for k, v in HUMAN_ARGS.items() if k not in ("orch_id", "human_fields_json")}
        result = await call("add_multiple_steps", {"orch_id": orch, "steps": [step | {"human_fields": PROD_FIELDS}]})
        _says_the_field_and_its_shape(result["error"], "step_generate_api_keys", "human_fields", "a list")
        assert list(await _steps(orch)) == ["step_1"]

    async def test_update_step_refuses_a_patch_that_misshapes_it(self, orch):
        before = (await _steps(orch))["step_1"]
        result = await call("update_step", {"orch_id": orch, "step_id": "step_1",
                                            "patch_json": json.dumps({"input_keys": "topic"})})
        _says_the_field_and_its_shape(result["error"], "step_1", "input_keys", "a list")
        assert (await _steps(orch))["step_1"] == before

    async def test_create_orchestration_refuses_and_makes_nothing(self):
        from core.routes.orchestrations import load_orchestrations

        step = {"id": "ask", "name": "Ask", "type": "human", "human_fields": PROD_FIELDS, "next_step_id": None}
        result = await call("create_orchestration", {"name": "Twitter", "entry_step_id": "ask",
                                                     "steps_json": json.dumps([step])})
        _says_the_field_and_its_shape(result["error"], "ask", "human_fields", "a list")
        assert [o for o in await load_orchestrations() if o.get("name") == "Twitter"] == []

    async def test_a_step_type_the_engine_has_not_got_is_refused_with_the_types(self, orch):
        result = await call("add_single_step", {"orch_id": orch, "step_id": "step_post", "name": "Post",
                                                "type": "tweet"})
        _says_the_field_and_its_shape(result["error"], "step_post", "type", "one of")
        assert "human" in result["error"] and "agent" in result["error"]
        assert list(await _steps(orch)) == ["step_1"]

    async def test_every_misshapen_field_is_named_at_once(self, orch):
        result = await call("add_single_step", HUMAN_ARGS | {"route_map_json": '["a", "b"]'})
        assert "`human_fields`" in result["error"] and "`route_map`" in result["error"]


class TestAWellShapedStepStillSaves:
    async def test_a_human_step_with_a_list_of_fields_is_added(self, orch):
        result = await call("add_single_step", HUMAN_ARGS | {"human_fields_json": json.dumps(PROD_FIELDS["fields"])})
        assert result["status"] == "step_added"
        assert (await _steps(orch))["step_generate_api_keys"]["human_fields"] == PROD_FIELDS["fields"]


class TestTheProductToolsDoor:
    async def test_over_mcp_it_is_a_refusal_in_the_same_words(self, orch):
        from core.product_tools.build import _handler
        from core.product_tools.registry import ToolRefusal

        with pytest.raises(ToolRefusal) as refused:
            await _handler("add_single_step")(None, dict(HUMAN_ARGS))
        _says_the_field_and_its_shape(str(refused.value), "step_generate_api_keys", "human_fields", "a list")
        assert list(await _steps(orch)) == ["step_1"]
