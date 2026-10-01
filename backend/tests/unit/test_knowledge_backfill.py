"""
What was indexed keyword-only is embedded later (B2a, D163 #2).

Every document indexed before B2a, or while a model's key was missing, has
chunk rows with no embedding. Each reconcile pass embeds up to `PASS_LIMIT` of
them into the base's active snapshot, with the model that snapshot recorded
(or the configured one, which it then records), and never raises into
reconcile.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from core.knowledge import backfill, seams
from core.knowledge.kb_settings import KnowledgeBase
from core.knowledge.parsed import ParsedDocument
from core.knowledge.pipeline import index_document
from core.knowledge.pipeline_contract import IndexContext
from core.knowledge.snapshots import record_embedding, serving_snapshot, snapshot_info
from core.knowledge.testing.fakes import HashingEmbedder
from core.knowledge.testing.reference_index import InMemoryKnowledgeIndex
from core.knowledge.types import Attribution, VectorQuery, Scope
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeCostDB, KnowledgeDocumentDB

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"
KB = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts")


@pytest.fixture(autouse=True)
def _seams():
    seams.reset_all()
    yield
    seams.reset_all()


async def _keyword_only(n: int = 1, status: str = "indexed", fixture: str = "pdf_contract") -> str:
    """A document indexed with no embedding model configured (the default embedder, no model)."""
    doc_id = "kdoc_" + f"{n:032x}"
    async with session() as s:
        s.add(KnowledgeDocumentDB(id=doc_id, tenant_id="default", kb_id=KB.id, path=f"contracts/{n}.pdf",
                                  source="upload", status=status, version=1))
    parsed = ParsedDocument.model_validate_json((FIXTURES / f"{fixture}.json").read_text("utf-8"))
    outcome = await index_document(IndexContext(
        kb=KB, doc_id=doc_id, doc_version=1, sha256=parsed.source.sha256, path=f"contracts/{n}.pdf",
        source="upload", parsed=parsed, snapshot_id=await serving_snapshot(KB),
        attribution=Attribution(kb_id=KB.id, doc_id=doc_id)))
    assert outcome.stages[2].status == "skipped"
    return doc_id


async def _embedded() -> list[bool]:
    async with session() as s:
        rows = (await s.execute(select(KnowledgeChunkDB).order_by(KnowledgeChunkDB.id))).scalars().all()
    return [r.embedding is not None for r in rows]


async def test_keyword_only_chunks_are_embedded_and_found_by_meaning():
    await _keyword_only()
    assert await _embedded() == [False, False, False]
    embedder = HashingEmbedder(dims=16, cost_source="byok", usd_per_mtok=20.0)
    seams.set_knowledge_embedder(embedder)

    assert await backfill.embed_missing(KB, settings={}) == 3
    assert await _embedded() == [True, True, True]
    assert {c["kind"] for c in embedder.calls} == {"document"}
    snap = await serving_snapshot(KB)
    assert (await snapshot_info([snap]))[snap]["embed_model"] == "hash-64"
    found = await seams.get_knowledge_index().search_vector(
        VectorQuery((Scope(KB.id, snap),), embedder.vector("hosting monthly fee"), None, 3))
    assert len(found.candidates) == 3
    async with session() as s:
        costs = (await s.execute(select(KnowledgeCostDB))).scalars().all()
    assert costs and {(c.stage, c.session_id, c.cost_source) for c in costs} == {
        ("embed", f"knowledge:{KB.id}", "byok")}

    assert await backfill.embed_missing(KB, settings={}) == 0, "nothing left: idempotent"


async def test_a_pass_is_bounded_and_the_next_one_finishes():
    await _keyword_only()
    seams.set_knowledge_embedder(HashingEmbedder(dims=16))
    assert await backfill.embed_missing(KB, settings={}, limit=2) == 2
    assert await _embedded() == [True, True, False]
    assert await backfill.embed_missing(KB, settings={}, limit=2) == 1
    assert all(await _embedded())


async def test_the_snapshot_s_recorded_model_is_the_one_used():
    await _keyword_only()
    await record_embedding(await serving_snapshot(KB), "model-a", 16)
    embedder = HashingEmbedder(dims=16)
    seams.set_knowledge_embedder(embedder)
    await backfill.embed_missing(KB, settings={"knowledge_embedding_model": "model-b"})
    assert {c["spec"].model for c in embedder.calls} == {"model-a"}


async def test_with_no_model_configured_nothing_is_asked(monkeypatch):
    import core.llm_providers as llm

    async def never(*a, **k):
        raise AssertionError("an embedding call was made with no model configured")

    monkeypatch.setattr(llm, "embed_batch_ex", never)
    await _keyword_only()
    assert await backfill.embed_missing(KB, settings={}) == 0


async def test_a_missing_key_leaves_them_for_a_later_pass(monkeypatch):
    import core.llm_providers as llm

    async def no_key(*a, **k):
        raise llm.EmbeddingFailed("needs a key", provider="openai", missing="openai_key")

    monkeypatch.setattr(llm, "embed_batch_ex", no_key)
    await _keyword_only()
    assert await backfill.embed_missing(KB, settings={"knowledge_embedding_model": "text-embedding-3-small"}) == 0
    assert not any(await _embedded())


async def test_a_document_still_being_indexed_is_the_pipeline_s():
    await _keyword_only(status="indexing")
    seams.set_knowledge_embedder(HashingEmbedder(dims=16))
    assert await backfill.embed_missing(KB, settings={}) == 0


async def test_a_failure_stops_the_pass_and_never_raises():
    await _keyword_only()
    embedder = HashingEmbedder(dims=16, max_batch=1)
    seams.set_knowledge_embedder(embedder)
    async with session() as s:
        second = (await s.execute(select(KnowledgeChunkDB).order_by(KnowledgeChunkDB.id))).scalars().all()[1]
    embedder.fail_on = {f"{second.context} {second.content}".strip()}
    assert await backfill.embed_missing(KB, settings={}) == 1, "the first batch stands; the pass stops at the failure"
    assert await _embedded() == [True, False, False]


async def test_a_spent_budget_pauses_the_backfill():
    await _keyword_only()
    seams.set_knowledge_embedder(HashingEmbedder(dims=16))
    capped = KB.model_copy(update={"budget_monthly_usd": 0.0})
    from core.knowledge.ledger import CostRow, record_costs
    from core.knowledge.types import Usage
    await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p", est_usd=0.01),
                                Attribution(kb_id=KB.id))])
    assert await backfill.embed_missing(capped, settings={}) == 0


async def test_an_index_that_keeps_vectors_elsewhere_is_left_alone():
    await _keyword_only()
    seams.set_knowledge_embedder(HashingEmbedder(dims=16))
    seams.set_knowledge_index(InMemoryKnowledgeIndex())
    assert await backfill.embed_missing(KB, settings={}) == 0


async def test_reconcile_reports_what_the_backfill_embedded(monkeypatch):
    from core.knowledge import sync

    async def three(kb, **_k):
        return 3

    monkeypatch.setattr(backfill, "embed_missing", three)
    drift = await sync.reconcile(KB)  # an empty folder: nothing else out of step
    assert drift.embedded == 3 and drift.any()
