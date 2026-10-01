"""The ingestion job (D157): a queued document through parse and the hand-off to indexing, every
step a compare-and-set, every failure ending somewhere with its reason — and the
deletes. Indexing is B1's; these tests fake it at the frozen hand-off, except
one that runs B1's real pipeline end to end."""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store, multi_tenant  # noqa: F401 — fixtures
from _fakes.knowledge_env import (
    SNAPSHOT, FakeIndex, FakeIndexer, enable_knowledge, install_fake_indexing, unbuilt_indexing, use_saved_settings,
    write_vault,
)
from core.knowledge import bases, dispatch, documents, events, seams
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.errors import (
    BudgetExceeded, EmbeddingError, EmbeddingInvalid, KnowledgeIndexError, WalletExhausted,
)
from core.knowledge.jobs import delete_document, drop_base, run_ingest_job
from core.knowledge.testing.fakes import RecordingDispatcher
from core.knowledge.types import IngestJob
from core.storage import get_blob_store
from core.tenancy import tenant_scope

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    yield
    dispatch.reset()


@pytest.fixture
def indexer(monkeypatch):
    fake = FakeIndexer()
    install_fake_indexing(monkeypatch, fake)
    return fake


async def _kb(folder="kb", **kw):
    return await bases.create_base(KnowledgeBaseCreate(name="KB", folder=folder, **kw))


async def _queued(kb, path, data, **kw):
    write_vault(path, data)
    row, _ = await documents.upsert_for_path(kb, path, source="vault", sha256=None, size=None, **kw)
    return row


def _job(row, op="upsert", tenant="default"):
    return IngestJob(tenant=tenant, kb_id=row.kb_id, op=op, reason="vault_write", path=row.path, doc_id=row.id)


class TestOneDocument:
    async def test_a_markdown_file_is_indexed(self, indexer):
        kb = await _kb()
        row = await _queued(kb, "kb/notes.md", "# Notes\n\nKickoff within five days.", meta={"team": "ops"})
        report = await run_ingest_job(_job(row))
        done = await documents.get(row.id)
        assert (done.status, done.version, done.snapshot_id, done.chunks, done.parser) == (
            "indexed", 1, SNAPSHOT, 2, "text/1")
        assert done.indexed_at and done.title == "Notes" and done.attempts == 1 and done.sha256
        (ctx,) = indexer.contexts
        assert (ctx.doc_version, ctx.snapshot_id, ctx.source, dict(ctx.metadata)) == (1, SNAPSHOT, "vault", {"team": "ops"})
        assert ctx.attribution.session_id == f"knowledge:{kb.id}" and ctx.attribution.source == "knowledge"
        assert ctx.parsed.text == "# Notes\n\nKickoff within five days." and ctx.settings["knowledge_enabled"]
        assert [s.stage for s in report.stages] == ["parse", "sanitize", "chunk", "index"]
        assert report.outcome == "indexed" and report.cost.est_usd == 0.001 and report.chunks.count == 2
        assert done.report["outcome"] == "indexed"

    async def test_new_content_is_a_new_version_and_a_retry_after_failure_reuses_it(self, monkeypatch):
        kb = await _kb()
        ok = FakeIndexer()
        install_fake_indexing(monkeypatch, ok)
        row = await _queued(kb, "kb/a.txt", "one")
        await run_ingest_job(_job(row))
        write_vault("kb/a.txt", "two")
        row, queued = await documents.upsert_for_path(kb, "kb/a.txt", source="vault", sha256=None, size=None)
        failing = FakeIndexer(raises=EmbeddingError("model down"))
        install_fake_indexing(monkeypatch, failing)
        await run_ingest_job(_job(row))
        assert (await documents.get(row.id)).version == 1  # a failure never moves the stored version
        again = await documents.transition(row.id, "queued")
        install_fake_indexing(monkeypatch, ok)
        await run_ingest_job(_job(again))
        assert [c.doc_version for c in ok.contexts] == [1, 2] and failing.contexts[0].doc_version == 2
        assert (await documents.get(row.id)).version == 2

    async def test_until_indexing_is_built_a_document_fails_honestly(self, monkeypatch):
        unbuilt_indexing(monkeypatch)
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        report = await run_ingest_job(_job(row))
        done = await documents.get(row.id)
        assert (done.status, done.status_reason) == ("failed", "internal") and "not built yet" in done.error
        assert report.stages[-1].error.code == "knowledge_unavailable" and report.retry.allowed

    @pytest.mark.parametrize("raised,status,reason", [
        (WalletExhausted(), "paused", "wallet_402"),
        (BudgetExceeded("kb", 1.0, 1.0), "paused", "budget_cap"),
        (EmbeddingInvalid("zero vector"), "failed", "embedding_invalid"),
        (EmbeddingError("timeout"), "failed", "embed_failed"),
        (KnowledgeIndexError("disk"), "failed", "index_failed"),
        (RuntimeError("boom"), "failed", "internal"),
    ])
    async def test_each_failure_ends_with_its_reason(self, monkeypatch, raised, status, reason):
        install_fake_indexing(monkeypatch, FakeIndexer(raises=raised))
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        done = await documents.get(row.id)
        assert (done.status, done.status_reason) == (status, reason)

    async def test_a_partial_outcome_keeps_its_reason(self, monkeypatch):
        install_fake_indexing(monkeypatch, FakeIndexer(status="partial", status_reason="truncated_size"))
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        assert (await documents.get(row.id)).status_reason == "truncated_size"


class TestSkipped:
    async def test_a_format_not_read_yet(self, indexer, monkeypatch):
        monkeypatch.setenv("SYNAPSE_SOFFICE", "/nonexistent/soffice")  # a .doc needs LibreOffice (A2)
        kb = await _kb()
        row = await _queued(kb, "kb/old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)
        report = await run_ingest_job(_job(row))
        done = await documents.get(row.id)
        assert (done.status, done.status_reason) == ("skipped", "unsupported_format") and indexer.contexts == []
        assert report.stages[0].status == "skipped" and report.retry.hint

    async def test_over_the_size_cap(self, indexer):
        await enable_knowledge(knowledge_max_file_mb=1)
        kb = await _kb()
        row = await _queued(kb, "kb/big.txt", "x" * (1024 * 1024 + 1))
        report = await run_ingest_job(_job(row))
        assert (await documents.get(row.id)).status_reason == "over_size_cap" and not report.retry.allowed

    async def test_an_empty_file(self, indexer):
        kb = await _kb()
        row = await _queued(kb, "kb/empty.md", "  \n")
        await run_ingest_job(_job(row))
        assert (await documents.get(row.id)).status_reason == "empty_document" and indexer.contexts == []


class TestRaces:
    async def test_a_delete_during_indexing_wins(self, monkeypatch):
        async def delete_meanwhile(ctx):
            await documents.tombstone(ctx.doc_id)

        install_fake_indexing(monkeypatch, FakeIndexer(during=delete_meanwhile))
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        assert (await documents.get(row.id)).status == "deleting"

    async def test_a_newer_write_during_indexing_wins(self, monkeypatch):
        async def write_meanwhile(ctx):
            write_vault(ctx.path, "newer")
            await documents.upsert_for_path(ctx.kb, ctx.path, source="vault", sha256="newer", size=5)

        install_fake_indexing(monkeypatch, FakeIndexer(during=write_meanwhile))
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "older")
        await run_ingest_job(_job(row))
        done = await documents.get(row.id)
        assert (done.status, done.version, done.sha256) == ("queued", 0, "newer")

    async def test_a_job_for_a_document_nobody_queued_does_nothing(self, indexer):
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        await run_ingest_job(_job(row))
        assert len(indexer.contexts) == 1

    async def test_nothing_runs_while_knowledge_is_off(self, indexer):
        from core.routes.settings import save_settings

        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await save_settings({"knowledge_enabled": False})
        assert await run_ingest_job(_job(row)) is None
        assert (await documents.get(row.id)).status == "queued" and indexer.contexts == []

    async def test_a_disabled_base_ingests_nothing(self, indexer):
        from core.knowledge.api_models import KnowledgeBaseUpdate

        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, status="disabled"))
        assert await run_ingest_job(_job(row)) is None
        assert (await documents.get(row.id)).status == "queued" and indexer.contexts == []

    async def test_the_tenant_travels_with_the_job(self, indexer, multi_tenant):  # noqa: F811
        with tenant_scope("acme"):
            await enable_knowledge()
            kb = await _kb()
            row = await _queued(kb, "kb/a.md", "acme's")
        await run_ingest_job(_job(row, tenant="acme"))  # run from another tenant's context
        with tenant_scope("acme"):
            assert (await documents.get(row.id)).status == "indexed"
        assert indexer.contexts[0].parsed.text == "acme's"


class TestDeletes:
    async def test_a_deleted_document_leaves_the_index_then_the_table(self, indexer):
        index = FakeIndex()
        seams.set_knowledge_index(index)
        heard: list = []
        events.knowledge_observers.append(heard.append)
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        get_blob_store().delete("vault/kb/a.md")
        await run_ingest_job(_job(await documents.tombstone(row.id), op="delete"))
        assert index.deleted == [(kb.id, row.id)] and await documents.get(row.id) is None
        assert heard[-1].type == "knowledge.document.deleted"

    async def test_an_index_that_refuses_keeps_the_tombstone(self, indexer):
        seams.set_knowledge_index(FakeIndex(fail=KnowledgeIndexError("down")))
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        assert await delete_document(kb, row) is False
        assert (await documents.get(row.id)).status == "deleting"

    async def test_a_file_that_vanished_while_queued_is_deleted(self, indexer):
        seams.set_knowledge_index(FakeIndex())
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        get_blob_store().delete("vault/kb/a.md")
        await run_ingest_job(_job(row))
        assert await documents.get(row.id) is None and indexer.contexts == []

    async def test_a_file_written_again_while_deleting_is_queued_afresh(self, indexer):
        seams.set_knowledge_index(FakeIndex())
        recorder = RecordingDispatcher()
        seams.set_ingest_dispatcher(recorder)
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await delete_document(kb, row)  # the file is still there: someone wrote it back
        fresh = await documents.get_by_path(kb.id, "kb/a.md")
        assert fresh.id != row.id and fresh.status == "queued"
        assert [(j.op, j.doc_id) for j in recorder.jobs] == [("upsert", fresh.id)]

    async def test_a_deleted_base_forgets_everything_but_its_files(self, indexer):
        index = FakeIndex()
        seams.set_knowledge_index(index)
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        await drop_base(kb.id)
        assert index.dropped == [kb.id] and await documents.get(row.id) is None
        assert await bases.get_base(kb.id) is None and get_blob_store().get("vault/kb/a.md") == "hello"


class TestOtherWork:
    async def test_resume_queues_paused_documents(self, monkeypatch):
        install_fake_indexing(monkeypatch, FakeIndexer(raises=WalletExhausted()))
        recorder = RecordingDispatcher()
        seams.set_ingest_dispatcher(recorder)
        kb = await _kb()
        row = await _queued(kb, "kb/a.md", "hello")
        await run_ingest_job(_job(row))
        await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="resume", reason="budget_resume"))
        assert (await documents.get(row.id)).status == "queued"
        assert [(j.op, j.reason) for j in recorder.jobs] == [("upsert", "budget_resume")]

    async def test_a_record_written_into_the_vault_brings_its_own_metadata(self, indexer):
        kb = await _kb()
        record = {"id": "t-1", "text": "Reset link expired.", "title": "Ticket", "metadata": {"priority": 1}}
        row = await _queued(kb, "kb/_records/t-1.json", json.dumps(record))
        await run_ingest_job(_job(row))
        done = await documents.get(row.id)
        assert (done.source, done.title, done.meta) == ("record", "Ticket", {"priority": 1})
        assert dict(indexer.contexts[0].metadata) == {"priority": 1}


async def test_end_to_end_through_b1s_pipeline():
    """No fakes at the hand-off: B1's index_document chunks, writes and indexes the document."""
    from core.knowledge.api_models import SearchRequest
    from core.knowledge.search import Origin, search

    kb = await _kb()
    row = await _queued(kb, "kb/notes.md", "# Escalation\n\nSeverity one incidents page the on-call engineer.")
    await run_ingest_job(_job(row))
    done = await documents.get(row.id)
    assert done.status == "indexed" and done.chunks >= 1 and done.snapshot_id
    found = await search(SearchRequest(query="severity incidents", kb_ids=[kb.id], mode="lexical"),
                         origin=Origin(surface="api"))
    assert [h.doc_id for h in found.hits][:1] == [row.id]
