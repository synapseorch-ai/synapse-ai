"""An evaluator's reply names its route as a bare label too (F491, BWA1, D274).

An evaluator asks the model for `{"tool": "route_<label>", ...}`. Nova Pro (Free's default model) answers with the
label alone: `{"tool": "manage", ...}`. Those replies were thrown away as unparseable and the step fell back. On
cloud dev, 49 of 80 of the AI builder's first routing decisions on Nova Pro were such fallbacks — almost all of them
right, and all sent to the builder's first route, `create`, which then built workflows nobody asked for (102 of Nova
Pro's 663 evaluator decisions in all). These pin:

- `route_<label>`, the bare label, either in any case, and a reply that is only the label, all name the route;
- anything else still falls back;
- the builder's own router falls back to `answer` — reading, never building — when a reply really does not parse.
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
    intent = next(s for s in orch["steps"] if s["id"] == "step_intent")
    assert intent["fallback_route"] == "answer"


@pytest.fixture
async def builder():
    await seed_native_builder()


async def test_a_bare_label_routes_the_builder_where_the_model_said(client, builder, fake_llm):
    fake_llm.set_default("The nightly schedule last ran at 02:00.")
    fake_llm.script([json.dumps({"tool": "answer", "arguments": {"reasoning": "a question"}})])
    events = await stream(client, "/api/builder/chat", {"message": "what's the last schedule that ran?"})
    decision = next(e for e in events if e["type"] == "routing_decision" and e.get("orch_step_id") == "step_intent")
    assert decision["decision"] == "answer"
    assert "Fallback" not in decision.get("reasoning", "")
    assert decision["reasoning"] == "a question"
