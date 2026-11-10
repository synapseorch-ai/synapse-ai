"""F1491 and F1502 together (eb, 11-10): an unknown model id's sentence (F1491's, naming the id it most likely means)
is what `failure_sentence` says and what a failed step keeps in `model_failure.said`, so the build's line says it.

TRI2's run B: `anthropic.claude-sonnet-4-6` ran as an Ollama model ("Ollama is not reachable"). F1491 says it as
unknown before any call; F1502 keeps a sentence of ours whole on the step. Both change `failure_sentence`, so this
holds them to one answer.
"""
from __future__ import annotations

import pytest

from core import llm_providers as L
from core.model_failures import failure_sentence
from core.orchestration.run_error import model_failure_of
from core.providers.registry import UNKNOWN_LEAD, unknown_said

ID = "anthropic.claude-sonnet-4-6"
#: The real dispatch, taken before the unit conftest puts its stand-in in place.
_REAL_GENERATE = L.generate_response


async def _unknown() -> L.LLMError:
    with pytest.raises(L.LLMError) as err:
        await _REAL_GENERATE("hi", "sys", "cloud", ID, {})
    return err.value


async def test_an_unknown_id_is_said_in_f1491s_words_and_the_step_keeps_them():
    exc = await _unknown()
    expected = unknown_said(ID)
    assert expected and expected.startswith(UNKNOWN_LEAD) and exc.code == L.UNKNOWN_MODEL
    assert failure_sentence(exc) == expected
    failure = model_failure_of(exc)
    assert failure["code"] == L.UNKNOWN_MODEL and failure["said"] == expected
