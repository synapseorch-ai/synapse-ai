"""
An embedding model Settings offers is embedded by the provider that offered it
(F322).

Settings → Models offers OpenAI's `text-embedding-3-small`, `-3-large` and
`text-embedding-ada-002`, and Gemini's `text-embedding-004`, `embedding-001` and
`gemini-embedding-001`. The router knew OpenAI only as `gpt*`, `chatgpt*` and
the o-series, and Gemini only as `gemini*`, `gemma*` and `lyria*`, so all but
the last went to Ollama — which either answered with a model of its own or,
with no Ollama running, handed back zero vectors that code indexing stored.
Bedrock's list without credentials offered its Titan ids without their
`bedrock.` prefix, and they went to Ollama too.
"""
from types import SimpleNamespace

import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx

KEYS = {"openai_key": "k-openai", "gemini_key": "k-gemini", "aws_region": "us-east-1"}

OPENAI = ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"]
GEMINI = ["text-embedding-004", "embedding-001", "gemini-embedding-001"]


@pytest.fixture
def no_ollama(monkeypatch):
    """Any call that reaches Ollama fails the test."""
    async def _ollama(*a, **k):
        raise AssertionError("an offered embedding model reached Ollama")
    monkeypatch.setattr(llm, "_embed_ollama", _ollama)


@pytest.mark.parametrize("model", OPENAI)
async def test_an_openai_embedding_model_goes_to_openai(model, monkeypatch, no_ollama):
    seen = {}

    def handler(url, kwargs):
        seen.update(url=url, auth=kwargs["headers"]["Authorization"], model=kwargs["json"]["model"])
        return FakeResponse({"data": [{"embedding": [0.25, 0.75]}]})

    install_httpx(monkeypatch, handler)

    assert await llm.embed_batch(["t"], model, KEYS) == [[0.25, 0.75]]
    assert seen == {"url": "https://api.openai.com/v1/embeddings", "auth": "Bearer k-openai", "model": model}
    assert llm.detect_provider_from_model(model) == "openai"


@pytest.mark.parametrize("model", GEMINI)
async def test_a_gemini_embedding_model_goes_to_google(model, monkeypatch, no_ollama):
    seen = {}

    class _Models:
        def embed_content(self, model, contents, config):
            seen["model"] = model
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.5, 0.5]) for _ in contents])

    def _client_for(api_key, **_k):
        seen["key"] = api_key
        return SimpleNamespace(models=_Models())

    monkeypatch.setattr(llm, "_gemini_client_for", _client_for)

    assert await llm.embed_batch(["t"], model, KEYS) == [[0.5, 0.5]]
    assert seen == {"key": "k-gemini", "model": model}
    assert llm.detect_provider_from_model(model) == "gemini"


async def test_every_embedding_model_the_list_offers_without_keys_is_its_provider_s(client, monkeypatch):
    """What a fresh install's Settings offers, before any key is saved."""
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {})
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse({}, status_code=404))

    providers = (await client.get("/api/models")).json()["providers"]
    offered = {p: providers[p]["embedding_models"] for p in ("openai", "gemini", "bedrock")}
    assert all(offered.values()), offered
    for provider, models in offered.items():
        for model in models:
            assert llm.detect_provider_from_model(model) == provider, (provider, model)


@pytest.mark.parametrize("model", ["bedrock.amazon.titan-embed-text-v1", "bedrock.amazon.titan-embed-text-v2:0"])
async def test_a_titan_model_goes_to_bedrock(model, monkeypatch, no_ollama):
    seen = []

    class _Bedrock:
        def invoke_model(self, body, modelId, accept, contentType):
            seen.append(modelId)
            return {"body": SimpleNamespace(read=lambda: b'{"embedding": [0.1, 0.9]}')}

    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _Bedrock())

    assert await llm.embed_batch(["t"], model, KEYS) == [[0.1, 0.9]]
    assert seen == [model[len("bedrock."):]]


@pytest.mark.parametrize("model,provider", [
    ("oaic.text-embedding-3-small", "openai_compatible"),
    ("locv1.text-embedding-3-small", "local_compatible"),
    ("synapse.text-embedding-3-small", "model_gateway"),
    ("bedrock.cohere.embed-english-v3", "bedrock"),
    ("ollama.nomic-embed-text", "ollama"),
    ("nomic-embed-text", "ollama"),
    ("text-embedding-3", "ollama"),
    ("embedding-gecko-001", "ollama"),
])
def test_a_look_alike_keeps_its_provider(model, provider):
    assert llm.detect_provider_from_model(model) == provider


@pytest.mark.parametrize("model", OPENAI + GEMINI)
def test_no_chat_list_offers_an_embedding_model(model):
    assert not llm.is_openai_chat_model(model)
    assert not llm.is_gemini_chat_model(model)


@pytest.mark.parametrize("model", [m for m in OPENAI + GEMINI if m != "embedding-001"])
def test_code_indexing_still_knows_each_one_s_dimension_without_calling_it(model, monkeypatch):
    from services import code_indexer

    async def _no_call(*a, **k):
        raise AssertionError("probed a model whose dimension is known")
    monkeypatch.setattr(llm, "embed_batch", _no_call)

    assert code_indexer.probe_embedding_dim(model, KEYS) in (768, 1536, 3072)


def test_code_indexing_probes_an_unlisted_one_at_its_provider(monkeypatch, no_ollama):
    """`embedding-001` is not in the dimension table, so one text is embedded to learn it."""
    from services import code_indexer

    class _Models:
        def embed_content(self, model, contents, config):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 768) for _ in contents])

    monkeypatch.setattr(llm, "_gemini_client_for", lambda *a, **k: SimpleNamespace(models=_Models()))

    assert code_indexer.probe_embedding_dim("embedding-001", KEYS) == 768
