"""
OpenRouter is told which app is calling (F1545, DIR1).

OpenRouter gives an app a page and a place in its rankings when its requests
carry `HTTP-Referer` and a title (`X-OpenRouter-Title`, and `X-Title`, the
older spelling it still reads): https://openrouter.ai/docs/app-attribution.
Synapse sent neither, so every call made through OpenRouter was nobody's.

They go to OpenRouter and nowhere else, decided by the address a request goes
to: an `openrouter.` model, and an OpenAI-compatible server set to OpenRouter's
address, alike; a chat, streamed or not, and an embedding. Every other provider,
and an address that only looks like OpenRouter's, is sent the request as before.
The headers carry the product's name and site, never anything of the person's.

Driven through a real httpx client over a mock transport, so what is checked is
what would be sent.
"""
import json

import httpx
import pytest

import core.llm_providers as llm
import core.model_wait as mw
from core.providers.registry import HOSTED

_REAL_CLIENT = httpx.AsyncClient

ATTRIBUTION = {"http-referer": "https://synapseorch.com", "x-openrouter-title": "SynapseOrch",
               "x-title": "SynapseOrch"}

_ANSWER = {"usage": {"prompt_tokens": 3, "completion_tokens": 2},
           "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
_VECTORS = {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}], "usage": {"prompt_tokens": 1}}


def _sse() -> bytes:
    chunks = [{"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}]
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


@pytest.fixture
def no_usage(monkeypatch):
    from core import usage_tracker

    async def nothing(**_kwargs):
        return None

    monkeypatch.setattr(usage_tracker, "log_usage", nothing)


@pytest.fixture
def sent(monkeypatch, no_usage):
    """Every request as it would leave: a chat answered plain or as a stream, as it asked; an embedding."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json=_VECTORS)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=_sse(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_ANSWER)

    monkeypatch.setattr(llm.httpx, "AsyncClient",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return requests


def _settings(**extra) -> dict:
    return {**{f"{spec.id}_key": f"k-{spec.id}" for spec in HOSTED}, "openai_compatible_key": "k-oaic",
            "local_compatible_key": "", **extra}


async def _ask(model: str, settings: dict | None = None):
    return await llm.generate_response_ex("hi", "sys", llm.detect_mode_from_model(model), model,
                                          settings or _settings())


def _attribution(request: httpx.Request) -> dict:
    return {k: request.headers[k] for k in ATTRIBUTION if k in request.headers}


# ── to OpenRouter ────────────────────────────────────────────────────────────

async def test_an_openrouter_model_says_which_app_calls(sent):
    result = await _ask("openrouter.moonshotai/kimi-k3")

    assert result.text == "ok"
    (request,) = sent
    assert request.url.host == "openrouter.ai"
    assert _attribution(request) == ATTRIBUTION
    assert request.headers["authorization"] == "Bearer k-openrouter"  # the person's key, as before


async def test_a_streamed_answer_says_it_too(sent):
    wait = mw.ModelWait(_ask("openrouter.z-ai/glm-5.3"), deltas=True)
    deltas = [e["text"] async for e in wait.events() if e["type"] == "_model_delta"]

    assert "".join(deltas) == "ok"
    (request,) = sent
    assert json.loads(request.content)["stream"] is True
    assert _attribution(request) == ATTRIBUTION


@pytest.mark.parametrize("base_url", ["https://openrouter.ai/api", "https://openrouter.ai/api/v1/",
                                      "https://OpenRouter.ai/api"])
async def test_an_openai_compatible_server_at_openrouters_address_says_it(sent, base_url):
    await _ask("oaic.openai/gpt-oss-120b", _settings(openai_compatible_base_url=base_url))

    (request,) = sent
    assert request.url.host == "openrouter.ai"
    assert _attribution(request) == ATTRIBUTION


async def test_an_embedding_through_openrouter_says_it(sent):
    settings = _settings(openai_compatible_base_url="https://openrouter.ai/api")
    await llm.embed_batch(["a text"], "oaic.openai/text-embedding-3-small", settings)
    await llm.embed_batch_ex(["a text"], "oaic.openai/text-embedding-3-small", settings)

    assert len(sent) == 2
    for request in sent:
        assert request.url.path.endswith("/embeddings")
        assert _attribution(request) == ATTRIBUTION


# ── never anywhere else ──────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", [s for s in HOSTED if s.id != "openrouter"], ids=lambda s: s.id)
async def test_no_other_hosted_provider_is_told(sent, spec):
    await _ask(f"{spec.id}.{spec.fallback[0]}")

    (request,) = sent
    assert _attribution(request) == {}


@pytest.mark.parametrize("model, setting, base_url", [
    ("oaic.meta-llama/llama-4", "openai_compatible_base_url", "https://api.together.xyz"),
    # Only OpenRouter's own host: one that merely starts or ends like it is someone else's.
    ("oaic.some-model", "openai_compatible_base_url", "https://openrouter.ai.example.com/api"),
    ("oaic.some-model", "openai_compatible_base_url", "https://notopenrouter.ai/api"),
    ("locv1.qwen3", "local_compatible_base_url", "http://127.0.0.1:1234"),
])
async def test_no_other_server_is_told(sent, model, setting, base_url):
    await _ask(model, _settings(**{setting: base_url}))

    (request,) = sent
    assert _attribution(request) == {}


async def test_an_embedding_elsewhere_is_not_told(sent):
    settings = _settings(openai_compatible_base_url="https://api.together.xyz")
    await llm.embed_batch(["a text"], "oaic.some-embedder", settings)
    await llm.embed_batch_ex(["a text"], "oaic.some-embedder", settings)

    assert len(sent) == 2
    assert all(_attribution(request) == {} for request in sent)
