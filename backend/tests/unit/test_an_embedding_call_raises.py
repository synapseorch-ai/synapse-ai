"""
An embedding call Knowledge makes raises when it fails; it never answers zeros (B2a, D163).

`embed_batch` answers all-zero vectors when OpenAI, Gemini or Ollama fail, and
`[]` when Bedrock does: code search stores them and they match nothing, or
everything. `embed_batch_ex` is what Knowledge calls instead. Each provider in
the table names its raising adapter (`embed_ex`), each tells the provider which
side of a search a text is on where the provider takes it (Cohere's
`input_type`, Gemini's `task_type`, our gateway's) and how wide to answer, and
each says what the call cost: tokens, a request id, and our gateway's charge.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import botocore.exceptions
import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx
from core.providers.registry import PROVIDERS, provider_for

KEYS = {
    "openai_key": "k-openai", "gemini_key": "k-gemini", "aws_region": "us-east-1",
    "model_gateway_base_url": "http://gateway.test", "model_gateway_key": "k-gateway",
    "openai_compatible_base_url": "http://oaic.test", "openai_compatible_key": "k-oaic",
    "ollama_base_url": "http://ollama.test:11434",
}


class _Answer(FakeResponse):
    def __init__(self, json_data=None, status_code=200, text="", headers=None):
        super().__init__(json_data, status_code, text)
        self.headers = headers or {}


def _openai_shaped(vectors, tokens=7):
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}


# ── the table ──────────────────────────────────────────────────────────────────

def test_every_provider_that_embeds_names_a_raising_adapter():
    for p in PROVIDERS:
        assert (p.embed is None) == (p.embed_ex is None), p.id


async def test_a_provider_that_does_not_embed_refuses():
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["t"], "claude-sonnet-4-6", KEYS)
    assert caught.value.code == llm.PROVIDER_REFUSED


async def test_the_kind_is_document_or_query():
    with pytest.raises(ValueError):
        await llm.embed_batch_ex(["t"], "text-embedding-3-small", KEYS, input_type="passage")
    with pytest.raises(ValueError):
        await llm.embed_batch_ex([], "text-embedding-3-small", KEYS)


# ── OpenAI ─────────────────────────────────────────────────────────────────────

async def test_openai_is_asked_for_a_width_and_says_what_it_cost(monkeypatch):
    seen = {}

    def handler(url, kwargs):
        seen.update(url=url, body=kwargs["json"], auth=kwargs["headers"]["Authorization"])
        return _Answer(_openai_shaped([[0.6, 0.8], [0.8, 0.6]], tokens=11), headers={"x-request-id": "req_1"})

    install_httpx(monkeypatch, handler)
    answer = await llm.embed_batch_ex(["a", "b"], "text-embedding-3-small", KEYS, input_type="query", dims=2)

    assert seen == {"url": "https://api.openai.com/v1/embeddings", "auth": "Bearer k-openai",
                    "body": {"model": "text-embedding-3-small", "input": ["a", "b"], "dimensions": 2}}
    assert answer.vectors == [[0.6, 0.8], [0.8, 0.6]]
    assert (answer.model, answer.provider, answer.input_tokens, answer.request_id) == (
        "text-embedding-3-small", "openai", 11, "req_1")


async def test_openai_answers_in_input_order_whatever_order_it_lists(monkeypatch):
    install_httpx(monkeypatch, _Answer({"data": [{"index": 1, "embedding": [0.0, 1.0]},
                                                 {"index": 0, "embedding": [1.0, 0.0]}]}))
    answer = await llm.embed_batch_ex(["a", "b"], "text-embedding-3-small", KEYS)
    assert answer.vectors == [[1.0, 0.0], [0.0, 1.0]]


async def test_a_one_width_openai_model_is_never_asked_for_another(monkeypatch):
    install_httpx(monkeypatch, lambda url, kwargs: pytest.fail("called"))
    with pytest.raises(llm.EmbeddingFailed):
        await llm.embed_batch_ex(["a"], "text-embedding-ada-002", KEYS, dims=256)


async def test_openai_without_a_key_names_the_setting_and_calls_nothing(monkeypatch):
    install_httpx(monkeypatch, lambda url, kwargs: pytest.fail("called"))
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "text-embedding-3-small", {})
    assert caught.value.missing == "openai_key"


@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (401, False)])
async def test_an_openai_failure_raises_where_embed_batch_answered_zeros(monkeypatch, status, retryable):
    install_httpx(monkeypatch, _Answer({"error": {"message": "nope"}}, status_code=status))
    assert await llm.embed_batch(["a"], "text-embedding-3-small", KEYS) == [[0.0] * 1536]  # unchanged
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "text-embedding-3-small", KEYS)
    assert caught.value.status == status and caught.value.retryable is retryable
    assert "nope" in str(caught.value)


@pytest.mark.parametrize("vectors,why", [
    ([[0.0, 0.0]], "zero"),
    ([[]], "empty"),
    ([[1.0, 0.0], [1.0]], "dimensions"),
    ([[1.0, 0.0]], "vectors"),  # two texts, one vector
    ([[float("nan"), 1.0]], "non-finite"),
])
async def test_an_answer_that_cannot_be_stored_is_refused(monkeypatch, vectors, why):
    install_httpx(monkeypatch, _Answer(_openai_shaped(vectors)))
    texts = ["a", "b"] if why in ("vectors", "dimensions") else ["a"]
    with pytest.raises(llm.EmbeddingFailed, match=why):
        await llm.embed_batch_ex(texts, "text-embedding-3-small", KEYS)


async def test_an_answer_of_the_wrong_width_is_refused(monkeypatch):
    install_httpx(monkeypatch, _Answer(_openai_shaped([[0.6, 0.8]])))
    with pytest.raises(llm.EmbeddingFailed, match="not 3"):
        await llm.embed_batch_ex(["a"], "text-embedding-3-small", KEYS, dims=3)


# ── our gateway and the OpenAI-compatible servers ─────────────────────────────

async def test_the_gateway_is_told_the_kind_and_the_width_and_reports_its_charge(monkeypatch):
    seen = {}

    def handler(url, kwargs):
        seen.update(url=url, body=kwargs["json"], auth=kwargs["headers"]["Authorization"])
        body = _openai_shaped([[0.6, 0.8] + [0.0] * 254], tokens=4)
        body["usage"].update(charged_microusd=12, cost_source="platform")
        return _Answer(body)

    install_httpx(monkeypatch, handler)
    answer = await llm.embed_batch_ex(["a"], "synapse.cohere-embed-v4", KEYS, input_type="query", dims=256)

    assert seen == {"url": "http://gateway.test/v1/embeddings", "auth": "Bearer k-gateway",
                    "body": {"model": "cohere-embed-v4", "input": ["a"], "dimensions": 256, "input_type": "query"}}
    assert (answer.provider, answer.input_tokens, answer.charged_microusd) == ("model_gateway", 4, 12)


async def test_a_document_batch_waits_longer_than_the_gateway_gives_up(monkeypatch):
    """C1: the gateway abandons a batch at 150 s and still charges what it embedded."""
    import httpx

    timeouts = []

    class _Client:
        def __init__(self, *a, timeout=None, **k):
            timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return _Answer(_openai_shaped([[0.6, 0.8]]))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await llm.embed_batch_ex(["a"], "synapse.titan-embed-v2", KEYS, input_type="document")
    await llm.embed_batch_ex(["a"], "synapse.titan-embed-v2", KEYS, input_type="query")
    assert timeouts[0] > 150 and timeouts[1] < timeouts[0]


async def test_the_gateway_s_402_carries_its_figures(monkeypatch):
    body = {"error": {"message": "Your model wallet is empty.", "type": "insufficient_funds",
                      "code": "model_wallet_empty", "needed_microusd": 40, "available_microusd": 3}}
    install_httpx(monkeypatch, _Answer(body, status_code=402))
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "synapse.titan-embed-v2", KEYS)
    e = caught.value
    assert (e.status, e.code, e.needed_microusd, e.available_microusd) == (402, llm.QUOTA_EXHAUSTED, 40, 3)
    assert e.provider == "model_gateway" and "wallet is empty" in str(e)


async def test_a_failed_gateway_batch_says_what_it_already_charged(monkeypatch):
    body = {"error": {"message": "Bedrock throttled the batch.", "charged_microusd": 9}}
    install_httpx(monkeypatch, _Answer(body, status_code=503))
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a", "b"], "synapse.titan-embed-v2", KEYS)
    assert caught.value.charged_microusd == 9 and caught.value.retryable


async def test_an_openai_compatible_server_is_not_told_the_kind(monkeypatch):
    seen = {}

    def handler(url, kwargs):
        seen.update(url=url, body=kwargs["json"])
        return _Answer(_openai_shaped([[0.6, 0.8]]))

    install_httpx(monkeypatch, handler)
    answer = await llm.embed_batch_ex(["a"], "oaic.nomic-embed-text", KEYS, input_type="query")
    assert seen == {"url": "http://oaic.test/v1/embeddings", "body": {"model": "nomic-embed-text", "input": ["a"]}}
    assert answer.charged_microusd is None


async def test_a_server_with_no_address_names_the_setting(monkeypatch):
    install_httpx(monkeypatch, lambda url, kwargs: pytest.fail("called"))
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "locv1.bge-m3", {})
    assert caught.value.missing == "local_compatible_base_url"


async def test_a_server_that_cannot_be_reached_is_retryable(monkeypatch):
    import httpx

    class _Down:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "ollama.nomic-embed-text", KEYS)
    assert caught.value.code == llm.PROVIDER_UNAVAILABLE and caught.value.retryable


# ── Gemini ─────────────────────────────────────────────────────────────────────

def _gemini(monkeypatch, seen, *, values=(0.6, 0.8), fail=None):
    class _Models:
        def embed_content(self, model, contents, config):
            if fail:
                raise fail
            seen.update(model=model, contents=list(contents), task_type=config.task_type,
                         dims=config.output_dimensionality)
            return SimpleNamespace(embeddings=[SimpleNamespace(values=list(values)) for _ in contents])

    def _client_for(api_key, **_k):
        seen["key"] = api_key
        return SimpleNamespace(models=_Models())

    monkeypatch.setattr(llm, "_gemini_client_for", _client_for)


@pytest.mark.parametrize("kind,task", [("query", "RETRIEVAL_QUERY"), ("document", "RETRIEVAL_DOCUMENT"),
                                       (None, None)])
async def test_gemini_is_told_the_task_and_the_width(monkeypatch, kind, task):
    seen = {}
    _gemini(monkeypatch, seen)
    answer = await llm.embed_batch_ex(["a", "b"], "gemini-embedding-001", KEYS, input_type=kind, dims=2)
    assert seen == {"key": "k-gemini", "model": "gemini-embedding-001", "contents": ["a", "b"], "task_type": task,
                    "dims": 2}
    assert answer.vectors == [[0.6, 0.8], [0.6, 0.8]] and answer.provider == "gemini"


async def test_a_gemini_failure_raises_where_embed_batch_answered_zeros(monkeypatch):
    _gemini(monkeypatch, {}, fail=RuntimeError("429 RESOURCE_EXHAUSTED. quota per minute"))
    assert await llm.embed_batch(["a"], "gemini-embedding-001", KEYS) == [[0.0] * 768]  # unchanged
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "gemini-embedding-001", KEYS)
    assert caught.value.status == 429 and caught.value.retryable


async def test_gemini_without_a_key_names_the_setting():
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "gemini-embedding-001", {})
    assert caught.value.missing == "gemini_key"


# ── Bedrock ────────────────────────────────────────────────────────────────────

class _Bedrock:
    def __init__(self, answer, *, tokens="3", error=None):
        self.answer = answer
        self.tokens = tokens
        self.error = error
        self.calls = []

    def invoke_model(self, body, modelId, accept, contentType):
        self.calls.append((modelId, json.loads(body)))
        if self.error:
            raise self.error
        payload = self.answer(json.loads(body)) if callable(self.answer) else self.answer
        return {"body": SimpleNamespace(read=lambda: json.dumps(payload).encode()),
                "ResponseMetadata": {"RequestId": "rid-1",
                                     "HTTPHeaders": {"x-amzn-bedrock-input-token-count": self.tokens}}}


async def test_titan_v2_is_asked_for_a_width_one_text_a_call(monkeypatch):
    fake = _Bedrock({"embedding": [0.6, 0.8], "inputTextTokenCount": 3})
    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: fake)
    answer = await llm.embed_batch_ex(["a", "b"], "bedrock.amazon.titan-embed-text-v2:0", KEYS,
                                      input_type="query", dims=2)
    assert sorted(c[1]["inputText"] for c in fake.calls) == ["a", "b"]
    assert all(c == ("amazon.titan-embed-text-v2:0", {"inputText": c[1]["inputText"], "normalize": True,
                                                      "dimensions": 2}) for c in fake.calls)
    assert (answer.input_tokens, answer.request_id) == (6, "rid-1")


async def test_cohere_on_bedrock_is_told_the_kind_in_batches_of_96(monkeypatch):
    fake = _Bedrock(lambda body: {"embeddings": {"float": [[0.6, 0.8] for _ in body["texts"]]}})
    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: fake)
    texts = [f"t{i}" for i in range(100)]
    answer = await llm.embed_batch_ex(texts, "bedrock.cohere.embed-v4:0", KEYS, input_type="query", dims=2)
    assert [len(c[1]["texts"]) for c in fake.calls] == [96, 4]
    first = fake.calls[0][1]
    assert (first["input_type"], first["embedding_types"], first["output_dimension"]) == (
        "search_query", ["float"], 2)
    assert len(answer.vectors) == 100


async def test_cohere_v3_reads_documents_as_documents(monkeypatch):
    fake = _Bedrock(lambda body: {"embeddings": [[0.6, 0.8] for _ in body["texts"]]})
    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: fake)
    await llm.embed_batch_ex(["x" * 3000], "bedrock.cohere.embed-english-v3", KEYS, input_type="document")
    body = fake.calls[0][1]
    assert body["input_type"] == "search_document" and len(body["texts"][0]) == 2048
    assert "output_dimension" not in body
    with pytest.raises(llm.EmbeddingFailed):
        await llm.embed_batch_ex(["a"], "bedrock.cohere.embed-english-v3", KEYS, dims=256)


async def test_a_bedrock_refusal_raises_where_embed_batch_answered_nothing(monkeypatch):
    error = botocore.exceptions.ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"},
         "ResponseMetadata": {"HTTPStatusCode": 429}}, "InvokeModel")
    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _Bedrock({}, error=error))
    assert await llm.embed_batch(["a"], "bedrock.amazon.titan-embed-text-v2:0", KEYS) == [[]]  # unchanged
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "bedrock.amazon.titan-embed-text-v2:0", KEYS)
    assert caught.value.status == 429 and caught.value.retryable and "slow down" in str(caught.value)


async def test_bedrock_with_no_credentials_names_the_setting(monkeypatch):
    error = botocore.exceptions.NoCredentialsError()
    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _Bedrock({}, error=error))
    with pytest.raises(llm.EmbeddingFailed) as caught:
        await llm.embed_batch_ex(["a"], "bedrock.amazon.titan-embed-text-v2:0", KEYS)
    assert caught.value.missing == "bedrock_api_key"


# ── Ollama ─────────────────────────────────────────────────────────────────────

async def test_ollama_embeds_a_batch_in_one_call_and_counts_its_tokens(monkeypatch):
    seen = []

    def handler(url, kwargs):
        seen.append((url, kwargs["json"]))
        return _Answer({"embeddings": [[0.6, 0.8], [0.8, 0.6]], "prompt_eval_count": 5})

    install_httpx(monkeypatch, handler)
    answer = await llm.embed_batch_ex(["a", "b"], "ollama.nomic-embed-text", KEYS, input_type="query")
    assert seen == [("http://ollama.test:11434/api/embed", {"model": "nomic-embed-text", "input": ["a", "b"]})]
    assert (answer.input_tokens, answer.provider) == (5, "ollama")


async def test_a_model_ollama_lacks_raises_with_its_words(monkeypatch):
    install_httpx(monkeypatch, _Answer({"error": 'model "nomic-embed-text" not found, try pulling it first'},
                                       status_code=404))
    with pytest.raises(llm.EmbeddingFailed, match="try pulling it first") as caught:
        await llm.embed_batch_ex(["a"], "nomic-embed-text", KEYS)
    assert caught.value.status == 404 and not caught.value.retryable


# ── code search's probe ────────────────────────────────────────────────────────

def test_code_search_s_width_probe_refuses_a_zero_vector(monkeypatch):
    """`embed_batch` answers zeros when Gemini fails, and the probe took their length as the model's width."""
    from services import code_indexer

    class _Models:
        def embed_content(self, model, contents, config):
            raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(llm, "_gemini_client_for", lambda *a, **k: SimpleNamespace(models=_Models()))
    assert provider_for("embedding-001").id == "gemini"
    with pytest.raises(ValueError, match="probe failed"):
        code_indexer.probe_embedding_dim("embedding-001", KEYS)
