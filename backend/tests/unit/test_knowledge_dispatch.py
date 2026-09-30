"""Handing ingestion work over (D157): the tenant travels, a burst of saves is one job, at most
`knowledge_ingest_concurrency` jobs run at once in process, and nothing ever
fails the caller."""
from __future__ import annotations

import asyncio

import pytest

from _fakes.knowledge_db import multi_tenant  # noqa: F401 — fixture
from _fakes.knowledge_env import (
    FakeIndexer, enable_knowledge, install_fake_indexing, use_saved_settings, write_vault,
)
from core.knowledge import dispatch, seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.knowledge.types import DispatchReceipt
from core.tenancy import tenant_scope


@pytest.fixture(autouse=True)
def _fresh():
    dispatch.reset()
    yield
    dispatch.reset()


class _Held:
    """A dispatcher that accepts jobs in the background and never starts them."""

    def __init__(self):
        self.jobs = []

    async def dispatch(self, job):
        self.jobs.append(job)
        return DispatchReceipt(mode="background")


class TestEnqueue:
    async def test_a_job_carries_the_tenant_and_when_it_was_asked_for(self, multi_tenant):  # noqa: F811
        recorder = RecordingDispatcher()
        seams.set_ingest_dispatcher(recorder)
        with tenant_scope("acme"):
            receipt = await dispatch.enqueue("kb_000000000001", "upsert", "upload", path="kb/a.md", doc_id="kdoc_1")
        (job,) = recorder.jobs
        assert (job.tenant, job.op, job.reason, job.path, job.doc_id) == ("acme", "upsert", "upload", "kb/a.md",
                                                                          "kdoc_1")
        assert job.requested_at and receipt.mode == "queued"

    async def test_a_burst_of_saves_is_one_job_until_it_starts(self):
        held = _Held()
        seams.set_ingest_dispatcher(held)
        for _ in range(5):
            await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1")
        assert len(held.jobs) == 1
        dispatch.started(held.jobs[0])
        await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1")
        await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_2")
        assert [j.doc_id for j in held.jobs] == ["kdoc_1", "kdoc_1", "kdoc_2"]

    async def test_a_queue_is_left_to_deduplicate_itself(self):
        recorder = RecordingDispatcher()
        seams.set_ingest_dispatcher(recorder)
        await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1")
        assert not dispatch.is_pending(recorder.jobs[0])

    async def test_a_job_never_started_stops_holding_its_key(self, monkeypatch):
        held = _Held()
        seams.set_ingest_dispatcher(held)
        clock = [1000.0]
        monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock[0])
        await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1")
        clock[0] += dispatch.STALE_AFTER_S + 1
        await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1")
        assert len(held.jobs) == 2

    async def test_a_dispatcher_that_raises_never_reaches_the_caller(self):
        class Broken:
            async def dispatch(self, job):
                raise RuntimeError("redis down")

        seams.set_ingest_dispatcher(Broken())
        assert await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1") is None
        seams.set_ingest_dispatcher(RecordingDispatcher())
        assert await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1") is not None


class TestInProcess:
    """KB0's default dispatcher, driving A1's job."""

    async def test_at_most_the_configured_number_run_at_once(self, monkeypatch):
        use_saved_settings(monkeypatch)
        await enable_knowledge(knowledge_ingest_concurrency=2)
        running, peak, gate = [0], [0], asyncio.Event()

        async def runner(job):
            dispatch.started(job)
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await gate.wait()
            running[0] -= 1

        real = seams._load
        monkeypatch.setattr(seams, "_load", lambda dotted: runner if dotted == seams.INGEST_ENTRYPOINT else real(dotted))
        for n in range(5):
            await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id=f"kdoc_{n}")
        for _ in range(20):
            await asyncio.sleep(0)
        assert running[0] == 2 and peak[0] == 2
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert running[0] == 0 and peak[0] == 2

    async def test_a_failing_job_never_reaches_the_caller(self, monkeypatch):
        async def runner(job):
            raise RuntimeError("the job broke")

        real = seams._load
        monkeypatch.setattr(seams, "_load", lambda dotted: runner if dotted == seams.INGEST_ENTRYPOINT else real(dotted))
        receipt = await dispatch.enqueue("kb_1", "upsert", "vault_write", doc_id="kdoc_1")
        assert receipt.mode == "background"
        await asyncio.sleep(0.01)

    async def test_an_enqueued_document_is_indexed_in_the_callers_tenant(self, monkeypatch, multi_tenant):  # noqa: F811
        from core.knowledge import bases, documents
        from core.knowledge.api_models import KnowledgeBaseCreate

        indexer = FakeIndexer()
        install_fake_indexing(monkeypatch, indexer)
        use_saved_settings(monkeypatch)
        with tenant_scope("acme"):
            await enable_knowledge()
            kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
            write_vault("kb/a.md", "acme notes")
            row, _ = await documents.upsert_for_path(kb, "kb/a.md", source="vault", sha256=None, size=None)
            await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
            for _ in range(200):
                if (await documents.get(row.id)).status == "indexed":
                    break
                await asyncio.sleep(0.01)
            assert (await documents.get(row.id)).status == "indexed"
        assert indexer.contexts[0].parsed.text == "acme notes"
