"""The AI builder's quick steps on their own model (BWA1, D274, decision 7a).

People bring their own key and set the builder model, so the builder has to work on small models; and a small model
can do some of its steps as well as a large one. BWA1's step replay measured the routing step: managed gpt-oss-120b
picked the right route for 36 of 41 recorded requests where Nova Pro managed 26. So a step may name a `model_role`,
and a run's `_model_roles` gives every step with that role its own model:

- `apply_model_override` maps a role to its model and leaves every other step to the run's model, as before; a run
  with no roles, or a role it does not map, changes nothing;
- the builder's routing evaluators are its "quick" steps;
- "Builder model for quick steps" (`builder_quick_model`, empty by default) puts them on that model, and the rest of
  the build stays on the builder model; empty, every step is on the builder model as before.
"""
from __future__ import annotations

import pytest

from core.models_orchestration import Orchestration
from core.native_builder import bundled_definitions, seed_native_builder
from core.orchestration.engine import OrchestrationEngine, apply_model_override

from test_builder_routes import route, stream


def _orch() -> Orchestration:
    return Orchestration.model_validate({
        "id": "o1", "name": "o1", "entry_step_id": "a",
        "steps": [
            {"id": "a", "name": "route it", "type": "evaluator", "model_role": "quick",
             "route_map": {"x": "b"}, "next_step_id": "b"},
            {"id": "b", "name": "answer", "type": "llm", "prompt_template": "hi", "next_step_id": "c"},
            {"id": "c", "name": "end", "type": "end"},
        ],
    })


def test_a_role_gets_its_model_and_the_rest_the_runs():
    orch = _orch()
    apply_model_override(orch, "big", {"quick": "small"})
    assert [s.model for s in orch.steps] == ["small", "big", None]


def test_roles_alone_change_only_the_steps_that_name_one():
    orch = _orch()
    apply_model_override(orch, None, {"quick": "small"})
    assert [s.model for s in orch.steps] == ["small", None, None]


@pytest.mark.parametrize("roles", [None, {}, {"other": "small"}, {"quick": ""}, {"quick": 3}, "quick"])
def test_no_role_or_a_role_not_mapped_leaves_the_runs_model(roles):
    orch = _orch()
    apply_model_override(orch, "big", roles)
    assert [s.model for s in orch.steps] == ["big", "big", None]


def test_the_builders_quick_steps_are_its_routing_and_its_one_call_look_ups():
    """Every routing evaluator, and the steps that only write one call's arguments (the findings card; the front
    door's classify, SAI2-P4): never a step that plans, writes or answers. The build's plan check is code since
    SAI2-P3, and the how-to, connect and can't-do look-ups are code since SAI2-P4, with no model at all."""
    bundle = bundled_definitions()
    steps = bundle["orchestration"]["steps"]
    agents = {a["id"]: a for a in bundle["agents"]}
    quick = {s["id"] for s in steps if s.get("model_role") == "quick"}
    evaluators = {s["id"] for s in steps if s["type"] == "evaluator"}
    assert evaluators <= quick and "step_classify" in quick
    # P3b: the build's researcher notes its own findings (E2), so step_plan_findings and its quick call are gone.
    # SAI2-P5: the research route's card is code's (finish_findings), so step_findings asks no model either.
    # F1241's follow-up: the classify at a waiting plan is the same quick call, with `continues` required.
    assert quick - evaluators == {"step_classify", "step_classify_at_plan"}
    assert not [s["id"] for s in steps if s["type"] == "tool" and s.get("tool_arguments") is not None
                and s.get("model_role")], "a code step calls no model, quick or not"
    for s in steps:
        if s["id"] not in quick or s["type"] in ("evaluator", "tool"):
            continue
        # An agent step is quick only as one call: it finishes on a tool, and its agent holds nothing else (E2).
        assert s["type"] == "agent" and s.get("finish_tools"), s["id"]
        assert set(agents[s["agent_id"]]["tools"]) == set(s["finish_tools"]), s["id"]
        assert s["max_turns"] <= 2, s["id"]


@pytest.fixture
async def builder():
    await seed_native_builder()


async def test_the_quick_model_takes_the_routing_and_the_builder_model_the_rest(client, builder, fake_llm):
    saved = await client.post("/api/settings", json={
        "agent_name": "Synapse", "builder_model": "builder-model", "builder_quick_model": "quick-model"})
    assert saved.status_code == 200
    fake_llm.script([route("answer")])
    fake_llm.set_default("The nightly schedule ran at 02:00.")
    await stream(client, "/api/builder/chat", {"message": "what ran last night?"})
    by_prompt = [(c.get("current_model"), str(c.get("prompt_msg", ""))[:40]) for c in fake_llm.calls]
    assert by_prompt[0][0] == "quick-model", by_prompt
    assert {m for m, _ in by_prompt[1:]} == {"builder-model"}, by_prompt


async def test_empty_leaves_every_step_on_the_builder_model(client, builder, fake_llm):
    await client.post("/api/settings", json={"agent_name": "Synapse", "builder_model": "builder-model"})
    fake_llm.script([route("answer")])
    fake_llm.set_default("Nothing ran.")
    await stream(client, "/api/builder/chat", {"message": "what ran last night?"})
    assert {c.get("current_model") for c in fake_llm.calls} == {"builder-model"}


def test_the_role_map_is_a_run_key_the_engine_reads():
    assert OrchestrationEngine.MODEL_ROLES_KEY == "_model_roles"
