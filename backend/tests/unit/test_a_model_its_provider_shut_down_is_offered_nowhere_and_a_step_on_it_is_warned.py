"""A model its provider shut down is offered nowhere, and a saved step on it is warned in the provider's own words,
its call still sent (F1648).

Google's deprecations page shut gemini-3.1-flash-lite-preview down on May 25, 2026 (the live id is
gemini-3.1-flash-lite). DeepSeek's changelog of 2026-04-24 said its two legacy names, deepseek-chat and
deepseek-reasoner, would be discontinued on 2026-07-24. Each is in the product's own list of retired models
(`model_availability.RETIRED`) as shut down, with the provider's words and the date, and offered nowhere.

A saved step on one is told, as a step warning, that its model was retired by its provider on that date, and its call
still goes out: a provider can go on serving a retired id (Google answered the preview id after its date), and
refusing a model that still answers would break a workspace that works. If the provider refuses the call as a model
that is gone, the gone path takes over as for any model: it is marked gone (F1650) and a stand-in of the same provider
is sought (F1369).

Near misses: a model refused only to new callers (gemini-2.5-flash-lite, F1299) is called and not warned; a model on no
list is called as before.
"""
from __future__ import annotations

import types

import pytest

from core import model_availability as A
from core import model_choices
from core.llm_providers import LLMError

SHUT = {"gemini-3.1-flash-lite-preview": ("gemini", "2026-05-25"),
        "deepseek-chat": ("deepseek", "2026-07-24"),
        "deepseek-reasoner": ("deepseek", "2026-07-24")}


@pytest.fixture(autouse=True)
def fresh_cache():
    A._marked_cache.clear()
    yield
    A._marked_cache.clear()


@pytest.mark.parametrize("model", list(SHUT))
def test_each_is_retired_as_shut_down_with_its_providers_words_and_date(model):
    row = A.RETIRED[model]
    assert (row["provider"], row["since"]) == SHUT[model] and row.get("shut_down") is True, row
    assert row["reason"].strip()
    said = A.shut_down_said(model)
    assert said and "retired by its provider on" in said and SHUT[model][1] in said and row["reason"] in said, said


async def test_they_are_offered_nowhere():
    async def listed():
        return {"gemini": ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview"],
                "deepseek": ["deepseek-chat", "deepseek-reasoner", "deepseek-flash"]}

    model_choices.set_model_choices(listed)
    try:
        assert await model_choices.choices() == {"gemini": ["gemini-3.1-flash-lite"], "deepseek": ["deepseek-flash"]}
    finally:
        model_choices.set_model_choices(None)


@pytest.fixture
def provider(monkeypatch):
    """A provider that records each model it is called for, and answers each with the error set for it (a 503 by
    default)."""
    from core.providers import registry

    called: list[str] = []
    errors: dict[str, str] = {}

    async def chat(call, spec):
        called.append(call.model)
        raise LLMError(errors.get(call.model, "DeepSeek LLM Error: 503 UNAVAILABLE"))

    spec = types.SimpleNamespace(id="deepseek", mode="cloud", chat=chat, error="DeepSeek LLM Error")
    monkeypatch.setattr(registry, "branch_for", lambda mode, model: spec)
    monkeypatch.setattr(registry, "resolve", lambda chat: chat)
    return types.SimpleNamespace(called=called, errors=errors)


async def test_a_saved_steps_call_still_goes_out_and_the_step_is_warned(provider):
    from core import llm_providers

    with A.noting_stand_ins() as said, pytest.raises(LLMError):
        await llm_providers.generate_response_ex("hi", "sys", "cloud", "deepseek-chat", {})
    assert provider.called == ["deepseek-chat"], "the call goes out: a provider may still serve a retired id"
    warned = [s for s in said if "retired by its provider on 2026-07-24" in s]
    assert len(warned) == 1 and "discontinued" in warned[0], said


async def test_a_refusal_as_a_model_that_is_gone_still_takes_the_gone_path(provider, monkeypatch):
    from core import llm_providers

    sought: list[str] = []

    async def stand_in_for(model, failed, settings, tools):
        sought.append(model)
        return None

    monkeypatch.setattr(llm_providers, "_stand_in_for", stand_in_for)
    provider.errors["deepseek-chat"] = ("DeepSeek LLM Error: 404 The model `deepseek-chat` does not exist or you do "
                                        "not have access to it.")
    with A.noting_stand_ins(), pytest.raises(LLMError) as refused:
        await llm_providers.generate_response_ex("hi", "sys", "cloud", "deepseek-chat", {})
    assert provider.called == ["deepseek-chat"] and sought == ["deepseek-chat"], (provider.called, sought)
    assert A.model_gone(refused.value)


@pytest.mark.parametrize("model", ["gemini-2.5-flash-lite", "deepseek-flash"])
async def test_a_model_refused_only_to_new_callers_or_on_no_list_is_called_and_not_warned(provider, model):
    from core import llm_providers

    with A.noting_stand_ins() as said, pytest.raises(LLMError):
        await llm_providers.generate_response_ex("hi", "sys", "cloud", model, {})
    assert provider.called == [model] and not [s for s in said if "retired by its provider" in s], said
