"""A model its provider shut down is offered nowhere, and a saved step on it is refused before anything is called,
in the provider's own words (F1648).

Google shut gemini-3.1-flash-lite-preview down on May 25, 2026 (its deprecations page; the live id is
gemini-3.1-flash-lite). DeepSeek's changelog of 2026-04-24 said its two legacy names, deepseek-chat and
deepseek-reasoner, would be discontinued on 2026-07-24. Each is now in the product's own list of retired models
(`model_availability.RETIRED`) as shut down, with the provider's words and the date. It is offered nowhere, and a call
on it, a saved step's say, is never sent: it is refused with that reason. Where the same provider has a model that
fits within its price band (F1369), the step answers from it and says so.

Near misses: a model refused only to new callers (gemini-2.5-flash-lite, F1299) is still called for a workspace that
has it; a model on no list is called as before.
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
    assert said and "has been retired" in said and SHUT[model][1] in said and row["reason"] in said, said


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
    """A provider that records each model it is called for, and answers none of them."""
    from core.providers import registry

    called: list[str] = []

    async def chat(call, spec):
        called.append(call.model)
        raise LLMError("DeepSeek LLM Error: 503 UNAVAILABLE")

    spec = types.SimpleNamespace(id="deepseek", mode="cloud", chat=chat, error="DeepSeek LLM Error")
    monkeypatch.setattr(registry, "branch_for", lambda mode, model: spec)
    monkeypatch.setattr(registry, "resolve", lambda chat: chat)
    return called


async def test_a_saved_steps_call_is_refused_before_it_is_sent_in_the_providers_words(provider):
    from core import llm_providers

    with pytest.raises(LLMError) as refused:
        await llm_providers.generate_response_ex("hi", "sys", "cloud", "deepseek-chat", {})
    assert provider == [], "nothing is sent for a model its provider shut down"
    said = str(refused.value)
    assert "has been retired" in said and "2026-07-24" in said and "discontinued" in said, said
    assert A.model_gone(refused.value), "read as a model that is gone, so a same-provider stand-in may answer"


@pytest.mark.parametrize("model", ["gemini-2.5-flash-lite", "deepseek-flash"])
async def test_a_model_refused_only_to_new_callers_or_on_no_list_is_still_called(provider, model):
    from core import llm_providers

    with pytest.raises(LLMError):
        await llm_providers.generate_response_ex("hi", "sys", "cloud", model, {})
    assert provider == [model], provider
