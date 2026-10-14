"""A quick step whose model fails runs once on the builder model (BWA1, D274, decision 7a).

The AI builder's quick steps (its routing and one-call look-ups) run on their own model: the person's "Builder model
for quick steps", or a host's pick. A wrong model id, a key that lapsed or a wallet that ran dry made the first step
fail and the whole build with it, although the builder model could have done the step. Naveen's call (the plan,
7a): the step goes back to the person's model — once, said in the step's log — and the build never stops for it.
A step not on a role keeps failing as before: its model is the one the person chose for it.
"""
from __future__ import annotations

import pytest

from core.llm_providers import LLMError
from core.models_orchestration import Orchestration, OrchestrationRun
from core.native_builder import seed_native_builder
from core.orchestration.steps import on_model_or_fallback, role_fallback

from test_builder_routes import route, stream


@pytest.fixture
def quick_model_fails(monkeypatch, fake_llm):
    """`quick-model` answers with a provider's refusal; every other model is the fake."""
    import core.llm_providers as _llm

    async def gated(*args, **kwargs):
        if kwargs.get("current_model") == "quick-model":
            fake_llm.calls.append(dict(kwargs))
            raise LLMError("401 Unauthorized: the key for quick-model is not valid", status=401)
        return await fake_llm(*args, **kwargs)

    monkeypatch.setattr(_llm, "generate_response", gated, raising=False)
    return fake_llm


async def test_the_builders_routing_goes_back_to_the_builder_model_and_the_build_goes_on(client, quick_model_fails):
    await seed_native_builder()
    await client.post("/api/settings", json={
        "agent_name": "Synapse", "builder_model": "builder-model", "builder_quick_model": "quick-model"})
    quick_model_fails.script([route("answer")])
    quick_model_fails.set_default("The nightly schedule ran at 02:00.")

    events = await stream(client, "/api/builder/chat", {"message": "what ran last night?"})

    models = [c.get("current_model") for c in quick_model_fails.calls]
    assert models[:2] == ["quick-model", "builder-model"], models
    assert not [e for e in events if e.get("type") in ("error", "orchestration_error")], events
    assert any("The nightly schedule ran at 02:00." in str(e) for e in events)


def _run(roles=None, override=None) -> OrchestrationRun:
    state = {}
    if roles is not None:
        state["_model_roles"] = roles
    if override is not None:
        state["_model_override"] = override
    return OrchestrationRun(run_id="r1", orchestration_id="o1", shared_state=state)


def _step(role: str | None = "quick", model: str | None = "quick-model"):
    orch = Orchestration.model_validate({
        "id": "o1", "name": "o1", "entry_step_id": "a",
        "steps": [{"id": "a", "name": "route", "type": "evaluator", "model_role": role, "model": model,
                   "route_map": {"x": "b"}}, {"id": "b", "name": "end", "type": "end"}],
    })
    return orch.steps[0]


def test_a_step_on_its_roles_model_falls_back_to_the_runs():
    assert role_fallback(_step(), _run({"quick": "quick-model"}, "builder-model"), "quick-model") == "builder-model"


def test_with_no_run_model_it_falls_back_to_the_workspace_default(monkeypatch):
    import core.config as _config
    monkeypatch.setattr(_config, "load_settings", lambda: {"model": "workspace-model"})
    assert role_fallback(_step(), _run({"quick": "quick-model"}), "quick-model") == "workspace-model"


@pytest.mark.parametrize("step, run", [
    (_step(role=None), _run({"quick": "quick-model"}, "builder-model")),       # not on a role
    (_step(), _run(None, "builder-model")),                                   # the run maps no roles
    (_step(), _run({"quick": "other-model"}, "builder-model")),               # its model is not its role's
    (_step(model="builder-model"), _run({"quick": "builder-model"}, "builder-model")),  # nothing else to try
])
def test_any_other_step_has_none(step, run):
    assert role_fallback(step, run, step.model) is None


async def test_only_a_models_failure_falls_back_and_only_once():
    calls: list[str] = []

    async def call(model):
        calls.append(model)
        raise LLMError("503 unavailable", status=503)

    used: list = []
    with pytest.raises(LLMError):
        await on_model_or_fallback(call, "quick-model", "builder-model", used)
    assert calls == ["quick-model", "builder-model"], "the fallback is tried once, and its failure is the step's"

    async def cancelled(model):
        calls.append(model)
        raise TimeoutError

    calls.clear()
    with pytest.raises(TimeoutError):
        await on_model_or_fallback(cancelled, "quick-model", "builder-model", used)
    assert calls == ["quick-model"], "a timeout is not the model's failure"

    calls.clear()
    with pytest.raises(LLMError):
        await on_model_or_fallback(call, "quick-model", None, used)
    assert calls == ["quick-model"], "no fallback: it fails as before"


async def test_the_step_says_which_model_answered():
    async def call(model):
        if model == "quick-model":
            raise LLMError("401 bad key", status=401)
        return "ok"

    used: list = []
    assert await on_model_or_fallback(call, "quick-model", "builder-model", used) == "ok"
    assert used[0] == "builder-model" and "quick-model failed" in used[1] and "ran on builder-model" in used[1]
