"""An evaluator's reply names its route as a bare label too (F491, BWA1, D274).

An evaluator asks the model for `{"tool": "route_<label>", ...}`. Nova Pro (Free's default model) answers with the
label alone: `{"tool": "manage", ...}`. Those replies were thrown away as unparseable and the step fell back. On
cloud dev, 49 of 80 of the AI builder's first routing decisions on Nova Pro were such fallbacks — almost all of them
right, and all sent to the builder's first route, `create`, which then built workflows nobody asked for (102 of Nova
Pro's 663 evaluator decisions in all). These pin:

- `route_<label>`, the bare label, either in any case, and a reply that is only the label, all name the route;
- anything else still falls back;
- the builder's own router falls back to `answer` — reading, never building — when a reply really does not parse.
  Since SAI2-P4 the builder's router is one call, an E2 finish on `classify_request`: a reply that is not that call
  ends the step on its fallback, a workspace answer.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import bundled_definitions, seed_native_builder
from core.orchestration.steps import route_named

from test_builder_routes import stream

LABELS = ["create", "update", "manage", "answer"]


@pytest.mark.parametrize("reply,label", [
    ("route_manage", "manage"),
    ("manage", "manage"),
    ("MANAGE", "manage"),
    ("Route_Answer", "answer"),
    ('"answer"', "answer"),
    ("  update  ", "update"),
])
def test_the_route_is_read_however_it_is_named(reply, label):
    assert route_named(reply, LABELS) == label


@pytest.mark.parametrize("reply", ["", "route_", "delete", "route_delete", "I think manage is right", "answer it"])
def test_anything_else_names_no_route(reply):
    assert route_named(reply, LABELS) is None


def test_the_builders_router_falls_back_to_reading_not_building():
    orch = bundled_definitions()["orchestration"]
    classify = next(s for s in orch["steps"] if s["id"] == "step_classify")
    assert classify["finish_tools"] == ["classify_request"]
    assert (classify["finish_fallback"]["kind"], classify["finish_fallback"]["route"]) == ("answer", "step_answer")


@pytest.fixture
async def builder():
    await seed_native_builder()


async def test_a_reply_that_is_not_the_call_is_read_and_answered_never_built(client, builder, fake_llm):
    """Nova Pro's bare label (F491), at the front door since SAI2-P4: a reply naming a route instead of calling
    `classify_request` is no build. The step ends on its fallback, and the question is answered from the workspace."""
    fake_llm.set_default("The nightly schedule last ran at 02:00.")
    fake_llm.script([json.dumps({"tool": "answer", "arguments": {"reasoning": "a question"}})])
    events = await stream(client, "/api/builder/chat", {"message": "what's the last schedule that ran?"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert "step_answer_workspace" in order and "step_plan" not in order and "step_may_change" not in order, order
    final = next(e for e in events if e["type"] == "orchestration_complete")["final_state"]
    assert final["request"]["route"] == "step_answer" and final["request"]["fallback"] is True
    assert "02:00" in final["summary"]
