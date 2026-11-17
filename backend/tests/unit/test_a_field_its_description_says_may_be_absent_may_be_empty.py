"""An answer field whose own description says it may be absent is marked "may be empty" by the plan check (F1667).

Since 2.1 a model step's answer fields reach the model as a schema it must answer in (F1618): OpenAI, Gemini, Claude's
structured outputs and the model CLIs. A plan declared `order_number` as a required number while describing it "The
order number in the message, or null if none"; for a message with no order number, a schema-enforcing provider forces
the model to answer one anyway, so a made-up value (0) reached the run, or the CLI refused the call (F1665). Before
2.1 the shape was asked for in words and the model could say null.

Now the plan check repairs such a declaration rather than refusing it: a field whose description says it may be absent
("or null", "if none", "if any", "if absent", "if not present", "may be empty", "optional") is marked may be empty, at
any depth and in an inner workflow too, and the saved step keeps it, so its schema takes null.

Near misses: "the null hypothesis" and "non-null" do not trip it; a description with none of those words is unchanged;
a field already marked is unchanged; words inside a longer word ("if anything", "optionally") do not trip it.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan
from core.native_builder import objects as O
from core.native_builder.compile import compile_plan
from core.orchestration import output_fields as answer


def _classify(*fields: dict) -> dict:
    return {"id": "classify", "type": "llm", "name": "Classify", "does": "Classifies the message",
            "prompt": "Classify this message: {{ state.user_input }}", "config": {"output_fields": list(fields)},
            "next": "end"}


def _plan(*fields: dict) -> dict:
    return {"kind": "create", "title": "Message triage", "summary": "Classifies each message.",
            "steps": [_classify(*fields)]}


def _number(description: str | None, **over) -> dict:
    field = {"name": "order_number", "type": "number", **({"description": description} if description else {})}
    field.update(over)
    return field


async def _examined(plan: dict):
    return await build_plan.examine(plan, None, ws=build_plan.Workspace(), state={"user_message": "x"})


def _fields(found, step_id: str = "classify", workflow: int | None = None) -> list[dict]:
    steps = found.plan.steps if workflow is None else found.plan.inner_workflows[workflow].steps
    return answer.normalised((next(s for s in steps if s.id == step_id).config or {}).get("output_fields"))


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


class TestItIsMarked:
    @pytest.mark.parametrize("description", [
        "The order number in the message, or null if none",
        "The order number, if any",
        "The order number, if absent leave it out",
        "The order number if not present in the message",
        "The order number; may be empty",
        "Optional: the order number",
    ])
    async def test_a_field_described_as_possibly_absent(self, description):
        found = await _examined(_plan({"name": "is_complaint", "type": "boolean"}, _number(description)))
        assert found.plan is not None, found.errors
        assert _fields(found)[1].get("may_be_empty") is True, _fields(found)

    async def test_its_schema_then_takes_null(self):
        found = await _examined(_plan(_number("The order number, or null if none")))
        schema = answer.json_schema(_fields(found))
        assert {"type": "null"} in schema["properties"]["order_number"].get("anyOf", []), schema

    async def test_at_any_depth(self):
        people = {"name": "people", "type": "list", "of": {"fields": [
            {"name": "name", "type": "text"}, {"name": "phone", "type": "text", "description": "Their phone, if any"}]}}
        found = await _examined(_plan(people))
        phone = _fields(found)[0]["of"]["fields"][1]
        assert phone.get("may_be_empty") is True, phone

    async def test_in_an_inner_workflow_too(self):
        plan = _plan({"name": "kind", "type": "text"})
        plan["inner_workflows"] = [{"id": "w_inner", "title": "Inner triage", "does": "Classifies one message", "steps": [
            _classify({"name": "is_complaint", "type": "boolean"}, _number("The order number, or null if none"))]}]
        found = await _examined(plan)
        assert found.plan is not None and found.plan.inner_workflows, found.errors
        assert _fields(found, workflow=0)[1].get("may_be_empty") is True

    async def test_the_saved_step_keeps_it(self):
        found = await _examined(_plan(_number("The order number, or null if none")))
        saved = compile_plan(found.plan, mint=_mint()).orchestration
        step = next(s for s in saved["steps"] if s.get("type") == "llm")
        assert answer.normalised(step["output_fields"])[0].get("may_be_empty") is True, step["output_fields"]


class TestNearMisses:
    @pytest.mark.parametrize("description", [
        "The p-value under the null hypothesis",
        "A non-null id of the order",
        "The order number in the message",
        "If anything, the order number",
        "The order number, optionally with its prefix",
    ])
    async def test_other_words_leave_it_required(self, description):
        found = await _examined(_plan(_number(description)))
        assert found.plan is not None, found.errors
        assert not _fields(found)[0].get("may_be_empty"), _fields(found)

    async def test_no_description_leaves_it_as_it_was(self):
        found = await _examined(_plan(_number(None)))
        assert not _fields(found)[0].get("may_be_empty")

    async def test_a_field_already_marked_is_unchanged(self):
        found = await _examined(_plan(_number("The order number, or null if none", may_be_empty=True)))
        assert _fields(found)[0] == _number("The order number, or null if none", may_be_empty=True)
