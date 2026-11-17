"""A model is marked gone only when the error that says a model is gone names that model (F1650).

A call its provider refuses because the model is gone marks the model unavailable for the workspace (F1213). It
marked the model that was called whenever the error said "a model is gone", whichever model the error named. An error
naming another model (a stand-in's call answered with the first model's refusal, a gateway passing on another call's
error) or naming none marked a model that may well be served. Now the model is marked only when the error names it, in
any of its provider's spellings (`models/…`, a provider's prefix, quoted in backticks).

Near misses: each provider's own sentence for a gone model still marks it (Gemini's, OpenAI's, Anthropic's, Groq's);
a failure that says nothing of a model being gone marks nothing, as before.
"""
from __future__ import annotations

import pytest

from core import model_availability as A
from core.llm_providers import LLMError


@pytest.fixture(autouse=True)
def fresh_cache():
    A._marked_cache.clear()
    yield
    A._marked_cache.clear()


GEMINI = ("Gemini LLM Error: the request was refused. 404 NOT_FOUND. {'error': {'code': 404, 'message': "
          "'This model models/gemini-2.5-flash-lite is no longer available to new users.'}}")


@pytest.mark.parametrize("called, message", [
    ("gemini-2.5-flash-lite", GEMINI),
    ("gemini.gemini-2.5-flash-lite", GEMINI),
    ("gpt-4-32k", "OpenAI LLM Error: 404 The model `gpt-4-32k` does not exist or you do not have access to it."),
    ("claude-2", "Anthropic LLM Error: 404 {'type': 'error', 'error': {'type': 'not_found_error', 'message': "
                 "'model: claude-2'}}"),
    ("groq.mixtral-8x7b-32768", "Groq LLM Error: 400 The model `mixtral-8x7b-32768` has been decommissioned and is "
                                "no longer supported."),
], ids=["gemini", "gemini-prefixed", "openai", "anthropic", "groq-prefixed"])
async def test_an_error_that_names_the_called_model_marks_it(called, message):
    await A.note_failure(called, LLMError(message))
    assert called in await A.unavailable()


async def test_an_error_naming_another_model_marks_neither():
    await A.note_failure("gemini-2.5-flash", LLMError(GEMINI))
    assert await A.unavailable() == {}


@pytest.mark.parametrize("message", [
    "Gemini LLM Error: 404 NOT_FOUND: the requested model was not found.",
    "OpenAI LLM Error: 404 The model does not exist or you do not have access to it.",
])
async def test_an_error_naming_no_model_marks_nothing(message):
    await A.note_failure("gemini-2.5-flash", LLMError(message))
    assert await A.unavailable() == {}


async def test_a_failure_that_says_nothing_of_a_gone_model_marks_nothing():
    await A.note_failure("gemini-2.5-flash", LLMError("Gemini LLM Error: 503 UNAVAILABLE for gemini-2.5-flash"))
    assert await A.unavailable() == {}
