"""Indexing (B1a, D158): `index_document` and the snapshot it indexes into."""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import func, select

from core.knowledge import seams
from core.knowledge.errors import BudgetExceeded, EmbeddingError, KnowledgeIndexError
from core.knowledge.index_sql import SqlKnowledgeIndex
from core.knowledge.kb_settings import Chunking, Enrichment, KnowledgeBase
from core.knowledge.parsed import ParsedDocument
from core.knowledge.pipeline import enrich_prefix, index_document, partial_reason
from core.knowledge.pipeline_contract import IndexContext
from core.knowledge.snapshots import active_snapshots, record_embedding, serving_snapshot, snapshot_info
from core.knowledge.testing.fakes import HashingEmbedder
from core.knowledge.types import Attribution, LexicalQuery, Scope
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeCostDB, KnowledgeDocumentDB, KnowledgeSnapshotDB, UsageLogDB

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"
KB = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts")


def _parsed(name: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))


async def _document(doc_id: str = "kdoc_" + "1" * 32, path: str = "contracts/acme.pdf") -> str:
    async with session() as s:
        s.add(KnowledgeDocumentDB(id=doc_id, tenant_id="default", kb_id=KB.id, path=path, source="upload",
                                  status="indexing", version=1))
    return doc_id


async def _ctx(parsed: ParsedDocument, *, kb: KnowledgeBase = KB, version: int = 1, doc_id: str | None = None,
               metadata: dict | None = None, path: str = "contracts/acme.pdf") -> IndexContext:
    doc_id = doc_id or "kdoc_" + "1" * 32
    return IndexContext(kb=kb, doc_id=doc_id, doc_version=version, sha256=parsed.source.sha256, path=path,
                        source="upload", parsed=parsed, snapshot_id=await serving_snapshot(kb),
                        attribution=Attribution(kb_id=kb.id, doc_id=doc_id,
                                                session_id=Attribution.ingestion_session(kb.id)),
                        metadata=metadata or {})


async def _rows():
    async with session() as s:
        return (await s.execute(select(KnowledgeChunkDB).order_by(KnowledgeChunkDB.ordinal))).scalars().all()


# ── the hand-off ───────────────────────────────────────────────────────────────

async def test_a_pdf_is_chunked_enriched_and_searchable_by_keyword():
    await _document()
    ctx = await _ctx(_parsed("pdf_contract"), metadata={"doc_type": "contract", "title": "Acme MSA"})
    outcome = await index_document(ctx)

    assert outcome.chunks == 3 and outcome.tokens > 0
    assert outcome.status == "partial" and outcome.status_reason == "no_text_layer", "page 3 had no text layer"
    assert [(s.stage, s.status) for s in outcome.stages] == [
        ("chunk", "ok"), ("enrich", "ok"), ("embed", "skipped"), ("index", "ok")]
    assert outcome.stages[2].detail == {"reason": "embedder_unavailable"}
    assert outcome.usage == []

    rows = await _rows()
    assert [r.kind for r in rows] == ["text", "table", "text"]
    table = rows[1]
    assert table.context == "Document: Acme MSA · 2024-03-01 · Section: Master Services Agreement › Fees"
    assert table.title == "Acme MSA" and table.meta == {"doc_type": "contract"}, "title is a column, not metadata"
    assert table.path == "contracts/acme.pdf" and table.source == "upload" and table.mime == "application/pdf"
    assert table.embedding is None and table.indexed_at is not None
    assert all(r.content == ctx.parsed.text[r.char_start:r.char_end] for r in rows)

    hits = await SqlKnowledgeIndex().search_lexical(
        LexicalQuery((Scope(KB.id, ctx.snapshot_id),), "hosting fee", "simple", None, 5))
    assert [c.chunk_id for c in hits.candidates][:1] == [table.chunk_id]


async def test_the_section_prefix_is_searchable_too():
    await _document()
    ctx = await _ctx(_parsed("pdf_contract"))
    await index_document(ctx)
    hits = await SqlKnowledgeIndex().search_lexical(
        LexicalQuery((Scope(KB.id, ctx.snapshot_id),), "fees", "simple", None, 5))
    assert len(hits.candidates) == 2, "the table under 'Fees' and the footnote, by their section"


async def test_with_an_embedder_the_vectors_and_their_cost_are_recorded():
    embedder = HashingEmbedder(dims=16, max_batch=2, cost_source="byok", usd_per_mtok=20.0)
    seams.set_knowledge_embedder(embedder)
    await _document()
    ctx = await _ctx(_parsed("pdf_contract"))
    outcome = await index_document(ctx)

    assert outcome.stages[2].status == "ok"
    assert outcome.stages[2].detail["vectors"] == 3 and outcome.stages[2].detail["batches"] == 2
    assert [call["kind"] for call in embedder.calls] == ["document", "document"]
    assert embedder.calls[0]["texts"][0].startswith("Document: ")
    stats = await SqlKnowledgeIndex().stats(Scope(KB.id, ctx.snapshot_id))
    assert stats.chunks == 3 and stats.vectors == 3 and stats.dims == 16
    assert (await snapshot_info([ctx.snapshot_id]))[ctx.snapshot_id]["embed_model"] == "hash-64"

    async with session() as s:
        costs = (await s.execute(select(KnowledgeCostDB))).scalars().all()
        usage = (await s.execute(select(UsageLogDB))).scalars().all()
    assert len(costs) == 2 and {c.stage for c in costs} == {"embed"} and {c.cost_source for c in costs} == {"byok"}
    assert all(c.doc_id == ctx.doc_id and c.snapshot_id == ctx.snapshot_id for c in costs)
    assert len(usage) == 2 and {u.session_id for u in usage} == {f"knowledge:{KB.id}"}
    assert outcome.usage and sum(u.input_tokens for u in outcome.usage) == sum(c.input_tokens for c in costs)


async def test_a_failed_embedding_writes_nothing():
    seams.set_knowledge_embedder(HashingEmbedder(fail_on=[]))

    class Broken(HashingEmbedder):
        async def embed(self, texts, **kw):
            raise EmbeddingError("provider down")

    seams.set_knowledge_embedder(Broken())
    await _document()
    with pytest.raises(EmbeddingError):
        await index_document(await _ctx(_parsed("pdf_contract")))
    assert await _rows() == []


async def test_a_spent_budget_pauses_before_any_work():
    seams.set_knowledge_embedder(HashingEmbedder())
    kb = KB.model_copy(update={"budget_monthly_usd": 0.001})
    async with session() as s:
        s.add(KnowledgeCostDB(tenant_id="default", kb_id=kb.id, stage="embed", cost_source="platform",
                              model="m", est_usd=0.01))
    await _document()
    with pytest.raises(BudgetExceeded):
        await index_document(await _ctx(_parsed("pdf_contract"), kb=kb))
    assert await _rows() == []


# ── versions and retries ───────────────────────────────────────────────────────

async def test_a_retry_of_the_same_version_overwrites_itself():
    await _document()
    ctx = await _ctx(_parsed("pdf_contract"))
    await index_document(ctx)
    first = [r.chunk_id for r in await _rows()]
    await index_document(ctx)
    assert [r.chunk_id for r in await _rows()] == first


async def test_a_new_version_replaces_the_old_only_after_it_is_indexed():
    calls: list[str] = []

    class Watching(SqlKnowledgeIndex):
        async def upsert(self, entries):
            calls.append(f"upsert {len(entries)}")
            return await super().upsert(entries)

        async def delete_chunks(self, scope, chunk_ids):
            calls.append(f"delete {len(chunk_ids)}")
            return await super().delete_chunks(scope, chunk_ids)

    seams.set_knowledge_index(Watching())
    await _document()
    await index_document(await _ctx(_parsed("pdf_contract"), version=1))
    v1 = {r.chunk_id for r in await _rows()}
    await index_document(await _ctx(_parsed("docx_contract"), version=2))
    rows = await _rows()
    assert {r.doc_version for r in rows} == {2} and not v1 & {r.chunk_id for r in rows}
    assert calls == ["upsert 3", "upsert 2", "delete 3"], "the new version is in before the old goes"


async def test_a_document_deleted_mid_index_is_an_index_error():
    ctx = await _ctx(_parsed("record"), doc_id="kdoc_" + "9" * 32)  # no document row: it was deleted
    with pytest.raises(KnowledgeIndexError):
        await index_document(ctx)


# ── what the outcome says ──────────────────────────────────────────────────────

def test_partial_reasons_come_from_the_parse():
    doc = _parsed("record")
    assert partial_reason(doc) is None
    cut = doc.model_copy(update={"stats": doc.stats.model_copy(update={"truncated": True})})
    assert partial_reason(cut) == "truncated_size"
    eml = _parsed("eml_with_attachment")
    skipped = eml.model_copy(update={"attachments": [eml.attachments[0].model_copy(
        update={"parsed": None, "skipped_reason": "encrypted"})]})
    assert partial_reason(skipped) == "attachment_skipped"


async def test_metadata_reaches_every_chunk_and_attachments_are_marked():
    await _document(path="mail/renewal.eml")
    ctx = await _ctx(_parsed("eml_with_attachment"), metadata={"region": "EU", "doc_date": "2025-10-01"},
                     path="mail/renewal.eml")
    outcome = await index_document(ctx)
    assert outcome.status == "indexed" and outcome.chunks == 2
    body, attachment = await _rows()
    assert body.meta == {"region": "EU"} and body.doc_date == "2025-10-01"
    assert attachment.meta["region"] == "EU" and attachment.meta["_attachment"] == "invoice.txt"
    assert "Section: invoice.txt" in attachment.context


async def test_contextual_enrichment_is_reported_as_not_built():
    kb = KB.model_copy(update={"enrichment": Enrichment(mode="contextual")})
    await _document()
    outcome = await index_document(await _ctx(_parsed("record"), kb=kb))
    enrich = next(s for s in outcome.stages if s.stage == "enrich")
    assert enrich.detail["requested"] == "contextual" and enrich.detail["mode"] == "prefix"


async def test_the_kb_chunking_settings_apply():
    kb = KB.model_copy(update={"chunking": Chunking(mode="none")})
    await _document()
    outcome = await index_document(await _ctx(_parsed("docx_contract"), kb=kb))
    assert outcome.chunks == 1


def test_the_prefix():
    assert enrich_prefix("Acme MSA", "2024-03-01", ["A", "B"]) == "Document: Acme MSA · 2024-03-01 · Section: A › B"
    assert enrich_prefix(None, None, []) == ""


# ── snapshots ──────────────────────────────────────────────────────────────────

async def test_the_first_snapshot_is_made_once_even_under_a_race():
    await active_snapshots([KB.id])  # the store's own first-use setup is not what this races
    ids = await asyncio.gather(*[serving_snapshot(KB) for _ in range(5)])
    assert len(set(ids)) == 1
    async with session() as s:
        assert (await s.execute(select(func.count()).select_from(KnowledgeSnapshotDB))).scalar_one() == 1
    info = (await snapshot_info(ids[:1]))[ids[0]]
    assert info["chunker_version"] == "structure-1" and info["language"] == "simple" and info["status"] == "active"
    assert await active_snapshots([KB.id, "kb_000000000000"]) == {KB.id: ids[0]}


async def test_the_loser_of_the_race_reads_the_winners_snapshot(monkeypatch):
    from core.knowledge import snapshots

    winner = await serving_snapshot(KB)
    real = snapshots._active
    calls = {"n": 0}

    async def missed_it(s, kb_id):  # the first look happens before the winner's commit
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(s, kb_id)

    monkeypatch.setattr(snapshots, "_active", missed_it)
    assert await serving_snapshot(KB) == winner and calls["n"] == 2


async def test_the_embedding_model_is_recorded_once():
    snap = await serving_snapshot(KB)
    await record_embedding(snap, "model-a", 8)
    await record_embedding(snap, "model-b", 16)
    info = (await snapshot_info([snap]))[snap]
    assert (info["embed_model"], info["embed_dims"]) == ("model-a", 8)


# ── B2a (D163): the default embedder, and the snapshot's model wins ────────────

async def test_a_missing_key_skips_the_embed_stage_rather_than_failing_the_document(monkeypatch):
    """The default embedder with a model configured but no key: keyword-only now, the backfill later."""
    import core.llm_providers as llm

    async def no_key(*a, **k):
        raise llm.EmbeddingFailed("OpenAI embeddings need an OpenAI key.", provider="openai", missing="openai_key")

    monkeypatch.setattr(llm, "embed_batch_ex", no_key)
    await _document()
    ctx = await _ctx(_parsed("pdf_contract"))
    ctx = dataclasses.replace(ctx, settings={"knowledge_embedding_model": "text-embedding-3-small"})
    outcome = await index_document(ctx)

    assert outcome.status == "partial" and [(s.stage, s.status) for s in outcome.stages][2] == ("embed", "skipped")
    assert outcome.stages[2].detail == {"reason": "embedder_unavailable"}
    assert all(r.embedding is None for r in await _rows()) and len(await _rows()) == 3


async def test_a_snapshot_keeps_embedding_with_the_model_it_recorded():
    embedder = HashingEmbedder(dims=16)
    seams.set_knowledge_embedder(embedder)
    snap = await serving_snapshot(KB)
    await record_embedding(snap, "model-a", 16, profile_version="model-a@1")
    await _document()
    ctx = await _ctx(_parsed("pdf_contract"))
    ctx = dataclasses.replace(ctx, settings={"knowledge_embedding_model": "model-b"})
    await index_document(ctx)

    assert {c["spec"].model for c in embedder.calls} == {"model-a"}, "the deployment's new default waits for B2b"
    async with session() as s:
        row = (await s.execute(select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.id == snap))).scalar_one()
    assert (row.embed_model, row.embed_profile_version) == ("model-a", "model-a@1")


async def test_the_profile_version_is_recorded_with_the_model():
    seams.set_knowledge_embedder(HashingEmbedder(dims=16, model="text-embedding-3-small"))
    await _document()
    await index_document(await _ctx(_parsed("pdf_contract")))
    snap = (await active_snapshots([KB.id]))[KB.id]
    async with session() as s:
        row = (await s.execute(select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.id == snap))).scalar_one()
    assert (row.embed_model, row.embed_profile_version) == ("text-embedding-3-small", "text-embedding-3-small@1")
