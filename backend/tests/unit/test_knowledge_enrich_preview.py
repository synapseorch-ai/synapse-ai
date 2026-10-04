"""Contextual Retrieval's price before it is paid, and the settings check (CR1, D180): `enrich.estimate_usd`, the
cost preview's `enrich` line and its embedding tokens, and the 422 for a base that cannot write contexts."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge import costs, enrich
from core.knowledge.kb_settings import Enrichment
from core.store import session
from core.store.models import KnowledgeChunkDB
from test_knowledge_enrich import MODEL, TEXT, Writer, _base, _chunks, _ingest, _on, jobs  # noqa: F401 — fixtures

pytestmark = pytest.mark.usefixtures("knowledge_store")


# ── the estimate and the preview ───────────────────────────────────────────────

async def test_an_estimate_reads_the_cache_only_where_a_cache_point_holds():
    big, small = enrich.EXPLICIT_CACHE_MIN_TOKENS * 2, 1_000
    cached = await enrich.estimate_usd("claude-haiku-4-5", [(10, big)])
    full_i, full_o, _, _ = (10 * (big + enrich.DOCUMENT_WRAPPER_TOKENS) + 10 * (big // 10 + enrich.INSTRUCTION_TOKENS),
                            10 * enrich.CONTEXT_OUTPUT_TOKENS, 0, 0)
    from core import usage_tracker
    await usage_tracker._ensure_pricing()
    uncached = usage_tracker.calculate_cost("claude-haiku-4-5", input_tokens=full_i, output_tokens=full_o)
    assert cached < uncached / 3, "a document over the cache minimum is read from the cache after the first call"
    i, o, r, w = enrich.window_tokens("claude-haiku-4-5", small, [100] * 10)
    assert (r, w) == (0, 0) and i == 10 * (small + enrich.DOCUMENT_WRAPPER_TOKENS) + 10 * 180, \
        "under the cache minimum every call reads the document in full"
    i, o, r, w = enrich.window_tokens("synapse.nova-lite", big, [100] * 10)
    assert (r, w) == (0, 0), "a gateway model caches only as its rate says (none here)"
    _, _, r, w = enrich.window_tokens(MODEL, big, [100] * 10)
    assert w == 0 and r == 9 * (big + enrich.DOCUMENT_WRAPPER_TOKENS), "OpenAI caches a repeated prefix itself"
    assert await enrich.estimate_usd("some-unpriced-model", [(3, 500)]) is None


async def test_a_long_document_is_estimated_in_windows():
    whole = await enrich.estimate_usd("gpt-4.1-nano", [(100, 15_000)])
    long = await enrich.estimate_usd("gpt-4.1-nano", [(1000, 150_000)])
    assert long < whole * 10 * 1.6, "per passage a windowed document costs about what a 13k-token one does"


async def test_the_preview_prices_the_contexts_as_enrichment_will_ask_for_them(jobs, monkeypatch):
    monkeypatch.setattr(enrich, "_ask", Writer())
    kb = await _base(enrichment=Enrichment(mode="prefix"))
    for i in range(2):
        await _ingest(jobs, kb, f"doc{i}.md", TEXT.replace("Refunds", f"Refunds {i}"))
    preview = await costs.preview(kb, {"enrichment": {"mode": "contextual", "contextual_model": MODEL}})
    stage = next(s for s in preview.stages if s.stage == "enrich")
    async with session() as s:
        per_doc = (await s.execute(select(KnowledgeChunkDB.doc_id, KnowledgeChunkDB.tokens).where(
            KnowledgeChunkDB.kb_id == kb.id))).all()
    counts: dict[str, list[int]] = {}
    for doc_id, tokens in per_doc:
        counts.setdefault(doc_id, []).append(tokens)
    expected = await enrich.estimate_usd(MODEL, [(len(t), sum(t)) for t in counts.values()])
    assert stage.units == len(per_doc) and stage.est_usd == pytest.approx(expected, rel=1e-6)
    assert stage.model == MODEL and stage.cost_source == "byok"


async def test_adding_contexts_adds_their_tokens_to_the_embedding_estimate(jobs, monkeypatch):
    monkeypatch.setattr(enrich, "_ask", Writer())
    kb = await _base(enrichment=Enrichment(mode="prefix"))
    await _ingest(jobs, kb)
    embedding = {"model": "text-embedding-3-small"}  # priced in the preview only: nothing is embedded here
    plain = next(s for s in (await costs.preview(kb, {"embedding": embedding})).stages if s.stage == "embed")
    more = next(s for s in (await costs.preview(
        kb, {"embedding": embedding, "enrichment": {"mode": "contextual", "contextual_model": MODEL}})).stages
        if s.stage == "embed")
    n = len(await _chunks(kb.id))
    assert more.units == plain.units + n * enrich.CONTEXT_OUTPUT_TOKENS


# ── the settings check ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("model, words", [
    ("", "needs a chat model"),
    ("cli.claude", "runs a session per call"),
    ("text-embedding-3-small", "is an embedding model"),
])
def test_contextual_retrieval_without_a_model_that_can_write_is_refused(model, words):
    with pytest.raises(ValueError, match=words):
        costs.check_settings(enrichment=Enrichment(mode="contextual", contextual_model=model or None),
                             settings={"model": ""})


def test_a_prefix_base_and_a_chat_model_pass_the_check():
    costs.check_settings(enrichment=Enrichment(mode="prefix"), settings={})
    costs.check_settings(enrichment=Enrichment(mode="contextual", contextual_model=MODEL), settings={})
    costs.check_settings(enrichment=Enrichment(mode="contextual"), settings={"model": "claude-haiku-4-5"})


async def test_a_platform_model_is_quoted_at_what_the_deployment_charges():
    """F334's seam (W1): a `synapse.*` context model is priced at the deployment's rate (cloud: its catalogue × the
    plan's markup), never the $0 of a model the rate card does not know."""
    assert await enrich.estimate_usd("synapse.nova-lite", [(10, 2_000)]) is None, "no pricer: unpriced, as in OSS"

    async def pricer(model):
        return costs.Rate(input_per_mtok=0.075, output_per_mtok=0.30) if model == "synapse.nova-lite" else None

    costs.set_platform_pricer(pricer)
    try:
        usd = await enrich.estimate_usd("synapse.nova-lite", [(10, 2_000)])
        i, o, r, w = enrich.window_tokens("synapse.nova-lite", 2_000, [200] * 10)
        assert (r, w) == (0, 0), "a rate with no cache prices: every call pays the document"
        assert usd == pytest.approx((i * 0.075 + o * 0.30) / 1_000_000)
    finally:
        costs.set_platform_pricer(None)


async def test_a_platform_model_whose_rate_prices_its_cache_is_quoted_with_the_cache():
    """F349: our gateway puts a cache point after the document (the system prompt) for a model its catalogue prices
    a cache for, so a window's first call writes it and the rest read it — and the estimate says so."""
    async def pricer(model):
        if model == "synapse.nova-lite":
            return costs.Rate(input_per_mtok=0.075, output_per_mtok=0.30, cache_read_per_mtok=0.01875,
                              cache_write_per_mtok=0.0, cache_min_tokens=0)
        return None

    costs.set_platform_pricer(pricer)
    try:
        assert await enrich.caching_for("synapse.nova-lite") == ("explicit", 0)
        assert await enrich.caching_for("synapse.nova-micro") == (None, 0), "unpriced: no cache assumed"
        usd = await enrich.estimate_usd("synapse.nova-lite", [(10, 2_000)])
        i, o, r, w = enrich.window_tokens("synapse.nova-lite", 2_000, [200] * 10, caching=("explicit", 0))
        shown = 2_000 + enrich.DOCUMENT_WRAPPER_TOKENS
        assert (r, w) == (9 * shown, shown)
        assert usd == pytest.approx((i * 0.075 + r * 0.01875 + o * 0.30) / 1_000_000)
        uncached = await costs.chat_usd("synapse.nova-lite", i + r + w, o)
        assert usd < uncached / 2, "nine of ten calls read the document at a quarter of the price"
    finally:
        costs.set_platform_pricer(None)
