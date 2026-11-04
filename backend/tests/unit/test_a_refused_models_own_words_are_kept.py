"""A model call the provider refuses keeps the provider's own words, so a gone model is marked gone (F1318, CAP3's
prod-14 on 63dfb505; 79 11-03).

prod-14's cheapest pick gave an agent gpt-4.1-nano, which OpenAI listed for the key and then refused on
/v1/chat/completions with a 404: "The model `gpt-4.1-nano` does not exist or you do not have access to it." The
adapter kept httpx's own sentence ("Client error '404 Not Found' for url …") and dropped the body, so the provider's
words never reached `model_availability.model_gone`, nothing was marked, and the next build would pick it again. The
OpenAI, Grok and DeepSeek adapters now keep the refusal's own sentence (its JSON `error.message`, else its text), kept
as a stored run error is (held secrets named, key-shaped tokens marked) and capped. A 404 that names no model (a wrong
address's page) still marks nothing.
"""
from __future__ import annotations

import json

import pytest

from _fakes.fake_http import FakeResponse, install_httpx
from core import llm_providers as P
from core import model_availability as A
from core.secret_scrub import CREDENTIAL_MARK, hold_secrets

NANO = "The model `gpt-4.1-nano` does not exist or you do not have access to it."
GONE = json.dumps({"error": {"message": NANO, "type": "invalid_request_error", "code": "model_not_found"}})
KEY = "sk-" + "proj-" + "K" * 40


@pytest.fixture(autouse=True)
def fresh():
    A._marked_cache.clear()
    yield
    A._marked_cache.clear()


async def _openai(model):
    return await P.call_openai(model, [{"role": "user", "content": "hi"}], "sk-test")


async def _grok(model):
    return await P.call_grok(model, [{"role": "user", "content": "hi"}], "sys", "xai-test")


async def _deepseek(model):
    return await P.call_deepseek(model, [{"role": "user", "content": "hi"}], "sys", "ds-test")


ADAPTERS = [pytest.param(_openai, id="openai"), pytest.param(_grok, id="grok"),
            pytest.param(_deepseek, id="deepseek")]


async def _refused(monkeypatch, call, response: FakeResponse) -> P.LLMError:
    install_httpx(monkeypatch, response)
    with pytest.raises(P.LLMError) as caught:
        await call("gpt-4.1-nano")
    return caught.value


@pytest.mark.parametrize("call", ADAPTERS)
async def test_a_gone_models_404_keeps_the_providers_sentence(monkeypatch, call):
    err = await _refused(monkeypatch, call, FakeResponse(status_code=404, text=GONE))
    assert NANO in str(err), str(err)
    assert A.model_gone(err), "the provider's own words say the model is gone"


async def test_so_the_model_is_marked_gone_for_the_workspace(monkeypatch):
    err = await _refused(monkeypatch, _openai, FakeResponse(status_code=404, text=GONE))
    await A.note_failure("gpt-4.1-nano", err, provider="openai")
    assert "gpt-4.1-nano" in await A.unavailable()


@pytest.mark.parametrize("call", ADAPTERS)
async def test_a_404_that_names_no_model_marks_nothing(monkeypatch, call):
    err = await _refused(monkeypatch, call, FakeResponse(status_code=404, text="<html><h1>404 Not Found</h1></html>"))
    assert A.model_gone(err) is None


@pytest.mark.parametrize("call", ADAPTERS)
async def test_a_key_the_refusal_quotes_is_never_kept(monkeypatch, call):
    said = json.dumps({"error": {"message": f"Incorrect API key provided: {KEY}. Find your key at the dashboard."}})
    err = await _refused(monkeypatch, call, FakeResponse(status_code=401, text=said))
    assert KEY not in str(err) and CREDENTIAL_MARK in str(err), str(err)


async def test_a_held_secret_the_refusal_quotes_is_named_not_kept(monkeypatch):
    hold_secrets({"SHOP_TOKEN": "tidewell-canary-key-2284"})
    said = json.dumps({"error": {"message": "Content refused: it quotes tidewell-canary-key-2284 in the prompt."}})
    err = await _refused(monkeypatch, _openai, FakeResponse(status_code=400, text=said))
    assert "tidewell-canary-key-2284" not in str(err) and "SHOP_TOKEN" in str(err), str(err)
