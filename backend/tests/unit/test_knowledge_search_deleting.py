"""F335: a document being deleted is never a search's answer — SQLite always; Postgres with
`SYNAPSE_TEST_POSTGRES_URL` (a throwaway `pgvector/pgvector:pg18`, never the shared :55432).

A deleted file's document is `deleting` until its delete job runs, and its chunks stay in the table until then: in
OSS for every Vault delete, in cloud for a document the knowledge worker was indexing when its file was deleted
(its chunks are written after the tombstone). Search must not find them in that window.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import false, func, select, update

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge import seams, sync
from core.knowledge.api_models import SearchRequest
from core.knowledge.index_sql import SqlKnowledgeIndex
from core.knowledge.kb_settings import COLLECTION, KnowledgeBase
from core.knowledge.parsed import ParsedDocument
from core.knowledge.pipeline import index_document
from core.knowledge.pipeline_contract import IndexContext
from core.knowledge.search import search
from core.knowledge.snapshots import serving_snapshot
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.knowledge.trace import Origin
from core.knowledge.types import Attribution
from core.store import collections, session
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB
from core.vault_events import VaultChange

pytestmark = pytest.mark.usefixtures("knowledge_store")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"
ON = {"knowledge_enabled": True}
ORIGIN = Origin(surface="tool", run_id="run_1", session_id="sess_1", agent_id="agent_1", turn=1)
CONTRACTS = KnowledgeBase(id="kb_0000000000f5", name="Contracts", folder="contracts")
#: Words in both fixtures, so each document answers it.
QUERY = "agreement renewal fees invoice"


def _parsed(name: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))


async def _row(n: int, path: str) -> str:
    doc_id = "kdoc_" + f"{n:032x}"
    async with session() as s:
        s.add(KnowledgeDocumentDB(id=doc_id, tenant_id="default", kb_id=CONTRACTS.id, path=path, source="upload",
                                  status="indexing", version=1, source_url=None))
    return doc_id


async def _index(fixture: str, n: int, path: str) -> str:
    doc_id = await _row(n, path)
    parsed = _parsed(fixture)
    await index_document(IndexContext(
        kb=CONTRACTS, doc_id=doc_id, doc_version=1, sha256=parsed.source.sha256, path=path, source="upload",
        parsed=parsed, snapshot_id=await serving_snapshot(CONTRACTS),
        attribution=Attribution(kb_id=CONTRACTS.id, doc_id=doc_id), metadata={}))
    return doc_id


async def _corpus() -> tuple[str, str]:
    await collections.save(COLLECTION, [CONTRACTS.model_dump()])
    acme = await _index("pdf_contract", 1, "contracts/acme.pdf")
    mail = await _index("eml_with_attachment", 2, "contracts/mail/renewal.eml")
    return acme, mail


async def _search(**kw):
    return await search(SearchRequest(kb_ids=[CONTRACTS.id], **kw), origin=ORIGIN, settings=ON)


async def _delete_file(path: str) -> list[str]:
    """The Vault hook's delete, with its delete jobs held: the window before the job runs."""
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return [row.id for row in await sync.on_vault_change(VaultChange(path=path, op="delete"))]


async def _chunks(doc_id: str) -> int:
    async with session() as s:
        return (await s.execute(select(func.count()).where(KnowledgeChunkDB.doc_id == doc_id))).scalar_one()


async def _status(doc_id: str) -> str:
    async with session() as s:
        return (await s.execute(select(KnowledgeDocumentDB.status).where(KnowledgeDocumentDB.id == doc_id))
                ).scalar_one()


async def test_a_deleted_file_is_not_found_before_its_delete_job_runs():
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    acme, mail = await _corpus()
    for mode in ("lexical", "vector", "hybrid"):
        assert acme in {h.doc_id for h in (await _search(query=QUERY, mode=mode)).hits}, mode
    assert await _delete_file("contracts/acme.pdf") == [acme]
    assert await _status(acme) == "deleting" and await _chunks(acme) > 0, "the job has not run"

    for mode in ("lexical", "vector", "hybrid"):
        hits = (await _search(query=QUERY, mode=mode)).hits
        assert hits and acme not in {h.doc_id for h in hits}, mode
        assert {h.doc_id for h in hits} == {mail}, mode


class _HeldEmbedder(HashingEmbedder):
    """Holds its first document batch until `go` is set: the file is deleted while the document is mid-index."""

    def __init__(self) -> None:
        super().__init__(dims=32)
        self.started = asyncio.Event()
        self.go = asyncio.Event()

    async def embed(self, texts: Sequence[str], *, kind, spec, settings, attribution):
        if kind == "document" and not self.go.is_set():
            self.started.set()
            await self.go.wait()
        return await super().embed(texts, kind=kind, spec=spec, settings=settings, attribution=attribution)


async def test_a_document_mid_index_when_its_file_is_deleted_is_never_found():
    held = _HeldEmbedder()
    held.go.set()
    seams.set_knowledge_embedder(held)
    await collections.save(COLLECTION, [CONTRACTS.model_dump()])
    mail = await _index("eml_with_attachment", 2, "contracts/mail/renewal.eml")
    held.go.clear()

    indexing = asyncio.create_task(_index("pdf_contract", 1, "contracts/acme.pdf"))
    await asyncio.wait_for(held.started.wait(), 10)
    acme = "kdoc_" + f"{1:032x}"
    assert await _delete_file("contracts/acme.pdf") == [acme]
    held.go.set()
    await asyncio.wait_for(indexing, 30)
    assert await _status(acme) == "deleting" and await _chunks(acme) > 0, "its chunks came after the tombstone"

    for mode in ("lexical", "vector", "hybrid"):
        hits = (await _search(query=QUERY, mode=mode)).hits
        assert {h.doc_id for h in hits} == {mail}, mode


async def test_search_skips_a_document_being_deleted_whatever_the_index_returns(monkeypatch):
    """The guarantee for an index that cannot see a document's status (an external store, later): search drops
    the chunk when it reads it, even when the index offered it."""
    documents = KnowledgeDocumentDB.__table__
    monkeypatch.setattr(SqlKnowledgeIndex, "_deleting", raising=False,
                        value=staticmethod(lambda scopes: select(documents.c.id).where(false())))
    acme, mail = await _corpus()
    async with session() as s:
        await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.id == acme).values(status="deleting"))
    response = await _search(query=QUERY, mode="lexical", top_k=20, include_trace=True)
    offered = {c.chunk_id for leg in response.trace.legs for c in leg.candidates}
    async with session() as s:
        theirs = set((await s.execute(select(KnowledgeChunkDB.chunk_id).where(
            KnowledgeChunkDB.doc_id == acme))).scalars())
    assert offered & theirs, "the index offered the deleting document's chunks"
    assert response.hits and {h.doc_id for h in response.hits} == {mail}


async def test_a_document_being_indexed_is_still_found():
    """Only `deleting` is skipped: a document mid-index (or the old version during a re-index) stays an answer."""
    acme, _mail = await _corpus()
    assert await _status(acme) == "indexing"
    assert acme in {h.doc_id for h in (await _search(query=QUERY, mode="lexical")).hits}
