"""A model its provider retired is never the one that fails a run (F1369, cap-04; eb 11-04).

CAP3's cap set 3, cap-04: the builder gave a narrow job's agent the lowest-cost model that fit from the workspace's
own OpenAI key, `gpt-4.1-nano`; OpenAI still lists it, and its first call answered "HTTP 404: The model
`gpt-4.1-nano` has been deprecated". F1299's shipped list knew only Gemini's retirement, and F1213/F1318's mark is set
by the refusal itself: the next build would not pick it, but the run that met it had already failed.

Now `gpt-4.1-nano` is on the shipped list, so nothing offers it; and a call its provider refuses because the model is
gone falls back, once, to a stand-in from the **same provider and the same pool** (the workspace's own key, or the
platform's), the cheapest that fits, never in a dearer price band than the refused one, never above the automatic
ceiling. A person who chose OpenAI never has a call sent to another provider because a model retired. The run says so
in the refused model's and the stand-in's names; only the call that answered is billed; the stored agent keeps its
model, the person's to change. With no stand-in it fails as today, the mark set. Any other failure never falls back.
"""
from __future__ import annotations

import types

import pytest

import core.llm_providers as _llm_mod
from core import model_availability as A
from core import model_choices
from core.llm_providers import LLMError

# The real dispatch, captured before the autouse `fake_llm` swaps it out (as test_a_failed_model_call_fails_the_step).
_REAL_GENERATE = _llm_mod.generate_response

NANO_GONE = ("OpenAI LLM Error: failed after 1 attempt. Last error: HTTP 404: The model `gpt-4.1-nano` has been "
             "deprecated, learn more here: https://platform.openai.com/docs/deprecations")


@pytest.fixture(autouse=True)
def fresh_cache():
    A._marked_cache.clear()
    yield
    A._marked_cache.clear()


@pytest.fixture
def listed():
    """The workspace's choices, as a host lists them; a test sets its own."""
    box: dict = {"models": {}}

    async def choices():
        return box["models"]

    model_choices.set_model_choices(choices)
    yield box
    model_choices.set_model_choices(None)


@pytest.fixture
def dispatch(monkeypatch):
    """The real call path, its provider faked: a model in `refused` answers cap-04's 404, any other answers."""
    from core import llm_providers
    from core.providers import registry

    called: list[str] = []
    refused: set[str] = {"gpt-4.1-nano"}
    logged: list[str] = []

    async def chat(call, spec):
        called.append(call.model)
        if call.model in refused:
            raise LLMError(NANO_GONE.replace("gpt-4.1-nano", call.model))
        return (f"answered by {call.model}", 12, 3, 0, 0, "stop", None, "")

    def branch(mode, model):
        return types.SimpleNamespace(id=registry.provider_for(model).id, mode="cloud", chat=chat,
                                     error="OpenAI LLM Error")

    async def log_usage(**kw):
        logged.append(kw.get("model"))

    monkeypatch.setattr(registry, "branch_for", branch)
    monkeypatch.setattr(registry, "resolve", lambda c: c)
    from core import usage_tracker
    monkeypatch.setattr(usage_tracker, "log_usage", log_usage)
    return types.SimpleNamespace(called=called, refused=refused, logged=logged, llm=llm_providers)


class TestTheShippedList:
    def test_gpt_4_1_nano_is_retired_for_openai(self):
        assert "gpt-4.1-nano" in A.retired_for("openai")
        assert "deprecated" in A.retired_for("openai")["gpt-4.1-nano"]["reason"]

    async def test_nothing_offers_it(self, listed):
        listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4o-mini"]}
        assert await model_choices.choices() == {"openai": ["gpt-4o-mini"]}


class TestTheStandIn:
    async def test_the_cheapest_fit_from_the_same_provider_and_pool_in_its_price_band(self, listed):
        from core.native_builder import models_menu as M

        listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4.1-mini", "gpt-4o-mini"],
                            "anthropic": ["claude-haiku-4-5"]}
        assert await M.stand_in("gpt-4.1-nano", has_tools=True) == "gpt-4o-mini"

    async def test_never_another_provider(self, listed):
        from core.native_builder import models_menu as M

        listed["models"] = {"openai": ["gpt-4.1-nano"], "anthropic": ["claude-haiku-4-5"]}
        assert await M.stand_in("gpt-4.1-nano", has_tools=True) is None

    async def test_never_a_dearer_band(self, listed):
        from core.native_builder import models_menu as M

        listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4.1-mini", "gpt-4.1"]}
        assert await M.stand_in("gpt-4.1-nano", has_tools=True) is None


class TestTheCall:
    async def test_a_gone_model_answers_once_from_its_stand_in_billed_as_the_stand_in(self, listed, dispatch):
        listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4o-mini"], "anthropic": ["claude-haiku-4-5"]}
        result = await dispatch.llm.generate_response_ex("hi", "sys", "cloud", "gpt-4.1-nano", {})
        assert str(getattr(result, "text", result)).startswith("answered by gpt-4o-mini"), result
        assert dispatch.called == ["gpt-4.1-nano", "gpt-4o-mini"]
        assert dispatch.logged == ["gpt-4o-mini"], "only the call that answered is billed"
        assert "gpt-4.1-nano" in await A.unavailable(), "the mark is set as before"

    async def test_it_is_said_naming_both(self, listed, dispatch):
        listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4o-mini"]}
        with A.noting_stand_ins() as said:
            await dispatch.llm.generate_response_ex("hi", "sys", "cloud", "gpt-4.1-nano", {})
        assert len(said) == 1 and "gpt-4.1-nano" in said[0] and "gpt-4o-mini" in said[0], said

    async def test_with_no_stand_in_it_fails_as_today(self, listed, dispatch):
        listed["models"] = {"openai": ["gpt-4.1-nano"], "anthropic": ["claude-haiku-4-5"]}
        with pytest.raises(LLMError, match="deprecated"):
            await dispatch.llm.generate_response_ex("hi", "sys", "cloud", "gpt-4.1-nano", {})
        assert dispatch.called == ["gpt-4.1-nano"], "never another provider"
        assert "gpt-4.1-nano" in await A.unavailable()

    async def test_a_stand_in_that_is_gone_too_fails_and_falls_back_no_further(self, listed, dispatch):
        listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4o-mini", "gpt-3.5-turbo"]}
        dispatch.refused.add("gpt-4o-mini")
        with pytest.raises(LLMError):
            await dispatch.llm.generate_response_ex("hi", "sys", "cloud", "gpt-4.1-nano", {})
        assert dispatch.called == ["gpt-4.1-nano", "gpt-4o-mini"], "once"

    @pytest.mark.parametrize("message", [
        "OpenAI LLM Error: 429 Rate limit reached for model gpt-4.1-nano",
        "OpenAI LLM Error: 400 This model does not support tools.",
        "Vault Error: 404 File not found: reports/q1.json",
    ])
    async def test_any_other_failure_never_falls_back(self, listed, dispatch, monkeypatch, message):
        from core.providers import registry

        listed["models"] = {"openai": ["gpt-4.1-mini", "gpt-4o-mini"]}

        async def failing(call, spec):
            dispatch.called.append(call.model)
            raise LLMError(message)

        monkeypatch.setattr(registry, "branch_for", lambda mode, model: types.SimpleNamespace(
            id="openai", mode="cloud", chat=failing, error="OpenAI LLM Error"))
        with pytest.raises(LLMError):
            await dispatch.llm.generate_response_ex("hi", "sys", "cloud", "gpt-4.1-mini", {})
        assert dispatch.called == ["gpt-4.1-mini"]


async def test_the_run_says_it_and_the_agent_keeps_its_model(listed, dispatch, monkeypatch):
    """An LLM step on the retired model: it answers from the stand-in, and its run says so in both names."""
    from _fakes import seed as S
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    monkeypatch.setattr(_llm_mod, "generate_response", _REAL_GENERATE)
    listed["models"] = {"openai": ["gpt-4.1-nano", "gpt-4o-mini"]}
    orch = S.make_orchestration(id="orch_f1369", entry_step_id="write", steps=[
        {"id": "write", "name": "Write", "type": "llm", "model": "gpt-4.1-nano", "prompt_template": "write",
         "output_key": "draft", "next_step_id": None}])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    events = [e async for e in engine.run("go", run_id="run_f1369")]
    said = [e.get("message", "") for e in events if e.get("type") == "step_warning"]
    assert any("gpt-4.1-nano" in m and "gpt-4o-mini" in m for m in said), said
    assert engine.orch.steps[0].model == "gpt-4.1-nano", "the step keeps its model: the person's to change"
