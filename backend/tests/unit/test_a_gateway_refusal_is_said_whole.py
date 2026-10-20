"""A refusal the model gateway words is said whole, however long it is (F782).

The gateway answers a refusal it makes itself with `{"error": {"message": <a sentence>, …}}`, and
`call_model_gateway` turns that into "Model gateway refused the call: <the sentence>" so the person reads words,
not JSON (F317). It found the sentence with a regex over the error's text, and the v1 loop keeps only the first 200
characters of a body: a sentence longer than 178 lost its closing quote, matched nothing, and the person read the
gateway's JSON, cut. Cloud's context-window refusal (F483) is 179 characters; an unreadable image's carries up to
300 of the provider's reason; F689's uncovered-model sentence names models. Found while measuring F689.
"""
from __future__ import annotations

import json

import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx
from core.model_failures import GATEWAY_REFUSED

SETTINGS = {"model_gateway_base_url": "http://gateway.test", "model_gateway_key": "tok"}
CONTEXT = ("the request is longer than this model can read (its context window): fewer tools, a shorter "
           "conversation or a model with a larger context will fit; sending it again will not help.")


def _refusal(status: int, message: str, code: str) -> FakeResponse:
    body = {"error": {"message": message, "type": "invalid_request_error", "code": code}}
    return FakeResponse(body, status_code=status, text=json.dumps(body))


async def _refused(monkeypatch, response: FakeResponse) -> llm.LLMError:
    install_httpx(monkeypatch, response)
    with pytest.raises(llm.LLMError) as err:
        await llm.call_model_gateway("synapse.nova-lite", [{"role": "user", "content": "hi"}], "s", SETTINGS)
    return err.value


@pytest.mark.parametrize("message", [CONTEXT, "the image could not be read: " + "x" * 300,
                                     "Short and plain."], ids=["context-window", "image-reason", "short"])
async def test_the_gateways_own_sentence_is_said_whole(monkeypatch, message):
    err = await _refused(monkeypatch, _refusal(400, message, "context_exceeded"))
    assert str(err) == f"{GATEWAY_REFUSED}{message}"
    assert err.provider == "gateway" and err.status == 400


async def test_a_wallet_refusal_keeps_its_code_and_figures_words(monkeypatch):
    said = "The model wallet has $0.40 left and this call needs about $0.42. Add balance under Settings, Billing to continue."
    err = await _refused(monkeypatch, _refusal(402, said, "model_wallet_empty"))
    assert str(err) == f"{GATEWAY_REFUSED}{said}" and err.status == 402


async def test_a_body_that_is_not_the_gateways_still_raises_as_it_did(monkeypatch):
    err = await _refused(monkeypatch, FakeResponse(status_code=400, text="<html>bad gateway</html>"))
    assert not str(err).startswith(GATEWAY_REFUSED) and "HTTP 400" in str(err)
