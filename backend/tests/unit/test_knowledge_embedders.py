"""
Knowledge's default embedder and the profiles it reads (B2a, D163).

`ProviderEmbedder` embeds with any model the provider table can call. Each model
is given its own convention for telling a question from a passage — a prefix,
or the provider's own parameter — at a width it can answer, in batches it
accepts, and every call says what it cost and who pays: our gateway's wallet
(platform), the customer's own key (byok), or their own machine (local).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import core.llm_providers as llm
from core.knowledge import embedders, profiles, seams
from core.knowledge.embedders import ProviderEmbedder
from core.knowledge.errors import EmbedderUnavailable, EmbeddingError, WalletExhausted
from core.knowledge.types import Attribution, EmbeddingSpec

BACKEND = Path(__file__).resolve().parents[2]
ATTRIBUTION = Attribution(kb_id="kb_1", session_id="knowledge:kb_1")
SETTINGS = {"openai_key": "k", "model_gateway_base_url": "http://gw", "openai_compatible_base_url": "http://o"}


@pytest.fixture(autouse=True)
def _clean():
    profiles.forget_probes()
    seams.reset_all()
    yield
    profiles.forget_probes()
    seams.reset_all()


@pytest.fixture
def calls(monkeypatch):
    """Every `embed_batch_ex` call, answered with unit vectors of the width asked for (or 4)."""
    seen: list[dict] = []

    async def fake(texts, model, settings, *, input_type=None, dims=None):
        seen.append({"texts": list(texts), "model": model, "input_type": input_type, "dims": dims})
        width = dims or 4
        return llm.EmbeddingResponse([[1.0] + [0.0] * (width - 1) for _ in texts], model,
                                     llm._providers.provider_for(model).id, input_tokens=3 * len(texts),
                                     request_id="r1", charged_microusd=5 if model.startswith("synapse.") else None)

    monkeypatch.setattr(llm, "embed_batch_ex", fake)

    async def no_rates():
        return {}

    from core import usage_tracker
    monkeypatch.setattr(usage_tracker, "_ensure_pricing", no_rates)
    return seen


async def _embed(texts, model, *, kind="document", dims=None, settings=SETTINGS):
    return await ProviderEmbedder().embed(texts, kind=kind, spec=EmbeddingSpec(model=model, dims=dims),
                                          settings=settings, attribution=ATTRIBUTION)


# ── profiles ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,family", [
    ("ollama.nomic-embed-text", "nomic-embed-text"),
    ("nomic-embed-text:latest", "nomic-embed-text"),
    ("oaic.nomic-ai/nomic-embed-text-v1.5", "nomic-embed-text"),
    ("locv1.text-embedding-nomic-embed-text-v1.5", "nomic-embed-text"),
    ("bedrock.amazon.titan-embed-text-v2:0", "titan-embed-text-v2"),
    ("synapse.titan-embed-v2", "titan-embed-text-v2"),
    ("bedrock.amazon.titan-embed-text-v1", "titan-embed-text-v1"),
    ("synapse.cohere-embed-v4", "cohere-embed-v4"),
    ("bedrock.us.cohere.embed-v4:0", "cohere-embed-v4"),
    ("bedrock.cohere.embed-multilingual-v3", "cohere-embed-v3"),
    ("text-embedding-3-small", "text-embedding-3-small"),
    ("oaic.text-embedding-3-large", "text-embedding-3-large"),
    ("gemini-embedding-001", "gemini-embedding-001"),
    ("text-embedding-004", "gemini-text-embedding-004"),
    ("bge-large", "bge-en-v1.5"),
    ("oaic.BAAI/bge-small-en-v1.5", "bge-en-v1.5"),
    ("bge-m3", "bge-m3"),
    ("ollama.mxbai-embed-large", "mxbai-embed-large"),
    ("oaic.intfloat/multilingual-e5-large", "e5"),
    ("locv1.e5-base-v2", "e5"),
    ("snowflake-arctic-embed2", "snowflake-arctic-embed-v2"),
    ("snowflake-arctic-embed:335m", "snowflake-arctic-embed"),
    ("ollama.qwen3-embedding:0.6b", "qwen3-embedding"),
    ("embeddinggemma", "embeddinggemma"),
])
def test_a_model_finds_its_family_whatever_its_routing_prefix(model, family):
    assert profiles.profile_for(model).name == family


@pytest.mark.parametrize("model", ["my-embedder", "oaic.bge-large-zh-v1.5", "e5-mistral-7b-instruct",
                                   "ollama.mistral"])
def test_a_model_no_family_describes_has_none(model):
    assert profiles.profile_for(model) is None


@pytest.mark.parametrize("model,kind,expected", [
    ("nomic-embed-text", "query", "search_query: q"),
    ("nomic-embed-text", "document", "search_document: q"),
    ("oaic.intfloat/e5-large-v2", "query", "query: q"),
    ("oaic.intfloat/e5-large-v2", "document", "passage: q"),
    ("bge-large", "query", "Represent this sentence for searching relevant passages: q"),
    ("bge-large", "document", "q"),
    ("qwen3-embedding", "query",
     "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:q"),
    ("text-embedding-3-small", "query", "q"),
    ("synapse.cohere-embed-v4", "query", "q"),
])
def test_each_model_is_told_the_kind_its_own_way(model, kind, expected):
    profile = profiles.profile_for(model)
    assert profile.prepare("q", kind) == expected
    assert profile.input_type(kind) == (kind if profile.kind == "native" else None)


def test_a_text_is_cut_to_the_model_s_input_cap():
    profile = profiles.profile_for("bedrock.cohere.embed-english-v3")
    assert len(profile.prepare("x" * 10_000, "document")) == 512 * profiles.CHARS_PER_TOKEN


@pytest.mark.parametrize("model,wanted,sent", [
    ("text-embedding-ada-002", None, None),
    ("text-embedding-ada-002", 1536, None),
    ("text-embedding-3-large", None, None),
    ("text-embedding-3-large", 256, 256),
    ("gemini-embedding-001", None, 768),  # 3,072 wide: Synapse asks for 768, as code search does
    ("gemini-embedding-001", 3072, None),
    ("synapse.titan-embed-v2", 512, 512),
    ("synapse.titan-embed-v2", 1024, None),
])
def test_the_width_sent_is_the_one_asked_for_or_none_for_the_native_one(model, wanted, sent):
    assert profiles.profile_for(model).request_dims(wanted) == sent


@pytest.mark.parametrize("model,wanted", [("text-embedding-ada-002", 256), ("text-embedding-3-large", 4000),
                                          ("synapse.titan-embed-v2", 300), ("bedrock.cohere.embed-v4:0", 2048)])
def test_a_width_the_model_cannot_answer_is_refused_before_any_call(model, wanted):
    with pytest.raises(EmbeddingError) as caught:
        profiles.profile_for(model).request_dims(wanted)
    assert not caught.value.retryable


async def test_an_unknown_model_is_probed_once(monkeypatch):
    seen = []

    async def probe(texts, model, settings, *, input_type=None, dims=None):
        seen.append(model)
        return llm.EmbeddingResponse([[1.0] + [0.0] * 5 for _ in texts], model, "openai_compatible")

    monkeypatch.setattr(llm, "embed_batch_ex", probe)
    first = await profiles.resolve("oaic.my-embedder", SETTINGS)
    second = await profiles.resolve("oaic.my-embedder", SETTINGS)
    assert (first.dims, second.dims, seen) == (6, 6, ["oaic.my-embedder"])
    assert first.kind == "none" and first.usd_per_mtok is None


# ── the embedder ───────────────────────────────────────────────────────────────

async def test_a_prefix_model_gets_its_prefix_and_no_input_type(calls):
    result = await _embed(["what is due?"], "ollama.nomic-embed-text", kind="query", dims=4)
    assert calls[-1]["texts"] == ["search_query: what is due?"] and calls[-1]["input_type"] is None
    assert result.dims == 4 and result.usage.cost_source == "local" and result.usage.est_usd == 0.0
    assert result.usage.priced


async def test_a_native_model_is_told_the_kind_and_our_gateway_s_charge_comes_back(calls):
    result = await _embed(["what is due?"], "synapse.cohere-embed-v4", kind="query", dims=256)
    assert calls[-1] == {"texts": ["what is due?"], "model": "synapse.cohere-embed-v4", "input_type": "query",
                         "dims": 256}
    usage = result.usage
    assert (usage.cost_source, usage.provider, usage.charged_microusd, usage.stage) == (
        "platform", "model_gateway", 5, "embed")


async def test_texts_go_in_the_model_s_own_batch_size(calls):
    result = await _embed([f"t{i}" for i in range(300)], "text-embedding-3-small")
    assert [len(c["texts"]) for c in calls] == [256, 44]
    assert len(result.vectors) == 300 and result.usage.input_tokens == 900


async def test_a_byok_call_is_estimated_from_the_profile_s_price(calls):
    result = await _embed(["a"] * 10, "text-embedding-3-small")
    usage = result.usage
    assert (usage.cost_source, usage.input_tokens, usage.charged_microusd, usage.priced) == ("byok", 30, None, True)
    assert usage.est_usd == pytest.approx(30 * 0.02 / 1_000_000)


async def test_an_operator_s_rate_wins_over_the_profile_s(calls, monkeypatch):
    from core import usage_tracker

    async def rates():
        return {"text-embedding-3-small": {"input_per_1m": 1.0, "output_per_1m": 0}}

    monkeypatch.setattr(usage_tracker, "_ensure_pricing", rates)
    result = await _embed(["a"], "text-embedding-3-small")
    assert result.usage.est_usd == pytest.approx(3 / 1_000_000)


async def test_a_remote_model_with_no_price_is_marked_unpriced(calls):
    result = await _embed(["a"], "oaic.text-embedding-nobody-prices")
    assert result.usage.cost_source == "byok" and not result.usage.priced and result.usage.est_usd == 0.0


async def test_no_model_configured_is_unavailable_not_a_failure():
    with pytest.raises(EmbedderUnavailable):
        await _embed(["a"], "")


@pytest.mark.parametrize("failure,expected", [
    (llm.EmbeddingFailed("no key", missing="openai_key"), EmbedderUnavailable),
    (llm.EmbeddingFailed("HTTP 429: slow", provider="openai", status=429), EmbeddingError),
    (llm.EmbeddingFailed("HTTP 402: insufficient_quota", provider="openai", status=402), EmbeddingError),
])
async def test_a_failure_becomes_the_error_ingestion_maps(monkeypatch, failure, expected):
    async def fail(*a, **k):
        raise failure

    monkeypatch.setattr(llm, "embed_batch_ex", fail)
    with pytest.raises(expected) as caught:
        await _embed(["a"], "text-embedding-3-small")
    if expected is EmbeddingError:
        assert caught.value.retryable is failure.retryable


async def test_our_gateway_s_402_pauses_and_a_charged_failure_is_recorded(monkeypatch):
    recorded = []

    async def fail(*a, **k):
        raise llm.EmbeddingFailed("HTTP 402: wallet empty", provider="model_gateway", status=402,
                                  needed_microusd=40, available_microusd=3, charged_microusd=7)

    async def record(rows):
        recorded.extend(rows)

    monkeypatch.setattr(llm, "embed_batch_ex", fail)
    from core.knowledge import ledger
    monkeypatch.setattr(ledger, "record_costs", record)
    with pytest.raises(WalletExhausted) as caught:
        await _embed(["a"], "synapse.titan-embed-v2")
    assert (caught.value.needed_microusd, caught.value.available_microusd) == (40, 3)
    assert [(r.usage.charged_microusd, r.usage.cost_source, r.attribution.kb_id) for r in recorded] == [
        (7, "platform", "kb_1")]


async def test_through_the_seam_a_zero_vector_still_never_passes(monkeypatch):
    async def zeros(texts, model, settings, *, input_type=None, dims=None):
        return llm.EmbeddingResponse([[0.0, 0.0] for _ in texts], model, "openai")

    monkeypatch.setattr(llm, "embed_batch_ex", zeros)
    from core.knowledge.errors import EmbeddingInvalid
    with pytest.raises(EmbeddingInvalid):
        await seams.get_knowledge_embedder().embed(["a"], kind="document", spec=EmbeddingSpec("text-embedding-3-small"),
                                                   settings=SETTINGS, attribution=ATTRIBUTION)


# ── the snapshot's model wins (D163 #3) ────────────────────────────────────────

async def test_a_snapshot_embeds_with_the_model_it_recorded(monkeypatch):
    from core.knowledge import snapshots
    from core.knowledge.kb_settings import KnowledgeBase

    async def info(ids):
        return {"ksnap_1": {"embed_model": "text-embedding-3-small", "embed_dims": 1536}}

    monkeypatch.setattr(snapshots, "snapshot_info", info)
    kb = KnowledgeBase(id="kb_1", name="K", folder="k", embedding={"model": "bge-m3"})
    spec = await embedders.snapshot_spec(kb, "ksnap_1", {"knowledge_embedding_model": "gemini-embedding-001"})
    assert (spec.model, spec.dims) == ("text-embedding-3-small", 1536)

    async def empty(ids):
        return {"ksnap_1": {"embed_model": None, "embed_dims": None}}

    monkeypatch.setattr(snapshots, "snapshot_info", empty)
    spec = await embedders.snapshot_spec(kb, "ksnap_1", {"knowledge_embedding_model": "gemini-embedding-001"})
    assert spec.model == "bge-m3", "no model recorded yet: the knowledge base's own"
    kb = KnowledgeBase(id="kb_1", name="K", folder="k")
    spec = await embedders.snapshot_spec(kb, "ksnap_1", {"knowledge_embedding_model": "gemini-embedding-001"})
    assert spec.model == "gemini-embedding-001", "then the deployment's"
    spec = await embedders.snapshot_spec(kb, "ksnap_1", {"embedding_model": "nomic-embed-text"})
    assert spec.model == "nomic-embed-text", "then the workspace's code-search model"


# ── what cloud imports ─────────────────────────────────────────────────────────

def test_the_new_modules_load_no_provider_sdk_and_no_numpy():
    names = ["core.knowledge.profiles", "core.knowledge.embedders"]
    script = textwrap.dedent(f"""
        import sys, importlib
        sys.path.insert(0, {str(BACKEND)!r})
        for name in {names!r}:
            importlib.import_module(name)
        bad = ("numpy", "httpx", "core.llm_providers", "boto3", "google")
        print(";".join(sorted(m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in bad))))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == ""
