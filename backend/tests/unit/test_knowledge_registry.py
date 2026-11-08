"""Knowledge's registry (D157): knowledge bases with a revision compare-and-set, documents with a
status compare-and-set, and the ingestion report. SQLite always; Postgres with
`SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from _fakes.knowledge_db import knowledge_store, multi_tenant  # noqa: F401 — fixtures
from core.knowledge import bases, documents, events
from core.knowledge.api_models import KnowledgeBaseCreate, KnowledgeBaseUpdate
from core.knowledge.errors import EmbeddingError, KnowledgePathError, StaleRevision
from core.knowledge.report import ReportBuilder, error_info, retry_hint
from core.tenancy import tenant_scope

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture
def heard():
    seen: list = []
    events.knowledge_observers.append(seen.append)
    return seen


async def _kb(name="Contracts", folder="contracts", **kw):
    return await bases.create_base(KnowledgeBaseCreate(name=name, folder=folder, **kw))


class TestBases:
    async def test_created_listed_and_read(self):
        kb = await _kb(folder="/Contracts/2026/")
        assert kb.id.startswith("kb_") and kb.folder == "Contracts/2026" and kb.revision == 1
        assert [b.id for b in await bases.list_bases()] == [kb.id]
        assert await bases.get_base(kb.id) == kb
        assert await bases.get_base("kb_000000000000") is None

    async def test_folders_never_overlap_and_the_engines_are_refused(self):
        await _kb(folder="contracts")
        for folder in ("contracts/2026", "contracts", "tool_outputs/x", ""):
            with pytest.raises((KnowledgePathError, ValueError)):
                await _kb(name="Other", folder=folder)
        await _kb(name="Sibling", folder="contracts2")  # component-wise: not inside "contracts"

    async def test_an_update_from_the_stored_revision_applies(self):
        kb = await _kb()
        before, after = await bases.update_base(kb.id, KnowledgeBaseUpdate(
            revision=1, name="Legal", description=None, budget_monthly_usd=5.0))
        assert before == kb
        assert (after.name, after.description, after.budget_monthly_usd, after.revision) == ("Legal", "", 5.0, 2)
        assert await bases.get_base(kb.id) == after
        _, cleared = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=2, clear_budget=True))
        assert cleared.budget_monthly_usd is None and cleared.revision == 3

    async def test_a_stale_revision_is_refused_and_writes_nothing(self):
        kb = await _kb()
        await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, name="First"))
        with pytest.raises(StaleRevision):
            await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, name="Second"))
        assert (await bases.get_base(kb.id)).name == "First"

    async def test_two_writers_from_one_revision_cannot_both_win(self, monkeypatch):
        """The SQL compare-and-set holds even without this process's lock (another process)."""
        kb = await _kb()
        real_row = bases._row
        both_read = asyncio.Barrier(2)

        async def row_then_wait(kb_id):
            found = await real_row(kb_id)
            await both_read.wait()
            return found

        monkeypatch.setattr(bases, "_lock", lambda: asyncio.Lock())
        monkeypatch.setattr(bases, "_row", row_then_wait)
        results = await asyncio.gather(
            bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, name="A")),
            bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, name="B")), return_exceptions=True)
        assert sorted(type(r).__name__ for r in results) == ["StaleRevision", "tuple"]
        stored, _ = await real_row(kb.id)
        assert stored.revision == 2 and stored.name in ("A", "B")

    async def test_a_new_folder_must_not_overlap_either(self):
        kb = await _kb()
        await _kb(name="Reports", folder="reports")
        with pytest.raises(KnowledgePathError):
            await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, folder="reports/q3"))
        _, moved = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, folder="contracts/legal"))
        assert moved.folder == "contracts/legal"  # inside its own old folder is fine

    async def test_a_broken_record_hides_nothing_else(self):
        from core.store import session
        from core.store.models import CollectionDB
        from core.tenancy import get_tenant

        kb = await _kb()
        async with session() as s:
            s.add(CollectionDB(tenant_id=get_tenant(), collection="knowledge_bases", key="kb_broken",
                               value={"id": "kb_broken", "folder": ""}))
        assert [b.id for b in await bases.list_bases()] == [kb.id]

    async def test_delete_forgets_the_record(self):
        kb = await _kb()
        assert await bases.delete_base_record(kb.id) is True
        assert await bases.get_base(kb.id) is None and await bases.delete_base_record(kb.id) is False

    async def test_state_is_read_live(self):
        from core.knowledge.ledger import CostRow, record_costs
        from core.knowledge.types import Attribution, Usage
        from core.store import session
        from core.store.models import KnowledgeSnapshotDB
        from core.tenancy import get_tenant

        kb = await _kb()
        other = await _kb(name="Empty", folder="empty")
        a, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="a", size=1)
        b, _ = await documents.upsert_for_path(kb, "contracts/b.md", source="vault", sha256="b", size=1)
        await documents.transition(b.id, "paused", reason="budget_cap")
        async with session() as s:
            s.add(KnowledgeSnapshotDB(id="ksnap_" + "1" * 32, tenant_id=get_tenant(), kb_id=kb.id, status="active"))
        await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p",
                                          est_usd=0.25), Attribution(kb_id=kb.id, doc_id=a.id))])
        state = await bases.states([kb, other])
        assert state[kb.id].counts_by_status == {"queued": 1, "paused": 1}
        assert state[kb.id].active_snapshot == "ksnap_" + "1" * 32
        assert state[kb.id].paused_reason == "budget_cap" and state[kb.id].month_spend_usd == 0.25
        assert state[other.id].counts_by_status == {} and state[other.id].active_snapshot is None


class TestDocuments:
    async def test_a_new_file_is_queued_and_announced(self, heard):
        kb = await _kb()
        row, queued = await documents.upsert_for_path(kb, "contracts/_records/t-1.json", source="vault",
                                                      sha256="s1", size=10, meta={"team": "ops", "title": "T1"})
        assert queued and row.status == "queued" and row.id.startswith("kdoc_")
        assert (row.source, row.mime, row.meta, row.title) == ("record", "application/json", {"team": "ops"}, "T1")
        assert [(e.type, e.doc_id) for e in heard] == [("knowledge.document.queued", row.id)]

    async def test_the_same_content_queues_nothing(self, heard):
        kb = await _kb()
        row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="upload", sha256="s1", size=1)
        await documents.transition(row.id, "parsing")
        await documents.transition(row.id, "indexing")
        await documents.transition(row.id, "indexed")
        again, queued = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s1", size=1)
        assert not queued and again.status == "indexed" and again.source == "upload"
        new, queued = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s2", size=2)
        assert queued and new.status == "queued" and new.sha256 == "s2"
        assert heard[-1].type == "knowledge.document.queued" and heard[-1].previous_status == "indexed"

    async def test_changed_metadata_queues_the_same_content(self):
        kb = await _kb()
        row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="upload", sha256="s1", size=1)
        await documents.transition(row.id, "failed", reason="internal")
        _, queued = await documents.upsert_for_path(kb, "contracts/a.md", source="upload", sha256="s1", size=1,
                                                    meta={"year": 2026})
        assert queued

    async def test_a_document_being_deleted_is_not_queued(self):
        kb = await _kb()
        row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s1", size=1)
        await documents.tombstone(row.id)
        again, queued = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s2", size=1)
        assert not queued and again.status == "deleting"

    async def test_every_status_change_is_a_compare_and_set(self):
        kb = await _kb()
        row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s", size=1)
        assert (await documents.transition(row.id, "parsing", attempts=1)).attempts == 1
        assert await documents.transition(row.id, "parsing") is None  # already parsing
        assert await documents.transition(row.id, "indexed") is None  # parsing cannot jump to indexed
        await documents.tombstone(row.id)
        assert await documents.transition(row.id, "indexing") is None  # lost to a delete
        with pytest.raises(ValueError):
            await documents.transition(row.id, "failed", reason="budget_cap")

    async def test_a_prefix_is_matched_by_component(self):
        kb = await _kb(folder="docs")
        for path in ("docs/contracts/a.md", "docs/contracts/b/c.md", "docs/contracts2/d.md", "docs/contracts"):
            await documents.upsert_for_path(kb, path, source="vault", sha256=path, size=1)
        gone = await documents.tombstone_under(kb.id, "docs/contracts")
        assert sorted(r.path for r in gone) == ["docs/contracts", "docs/contracts/a.md", "docs/contracts/b/c.md"]
        assert [r.path for r in await documents.list_for(kb.id, status="queued")] == ["docs/contracts2/d.md"]

    async def test_a_deleted_document_takes_its_chunks(self, heard):
        from sqlalchemy import func, select

        from core.store import session
        from core.store.models import KnowledgeChunkDB
        from core.tenancy import get_tenant

        kb = await _kb()
        row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s", size=1)
        async with session() as s:
            s.add(KnowledgeChunkDB(chunk_id="kch_" + "1" * 32, tenant_id=get_tenant(), kb_id=kb.id, doc_id=row.id,
                                   snapshot_id="ksnap_1", doc_version=1, ordinal=0, content="x", char_start=0,
                                   char_end=1))
        assert await documents.delete_row(row) is False  # only a tombstone is deleted
        await documents.tombstone(row.id)
        assert await documents.delete_row(row) is True and heard[-1].type == "knowledge.document.deleted"
        async with session() as s:
            assert (await s.execute(select(func.count()).select_from(KnowledgeChunkDB))).scalar_one() == 0

    async def test_paging_and_filters(self):
        kb = await _kb()
        rows = [(await documents.upsert_for_path(kb, f"contracts/{n}.md", source="vault", sha256=str(n), size=1))[0]
                for n in range(5)]
        ids = sorted(r.id for r in rows)
        page = await documents.list_for(kb.id, limit=2)
        assert [r.id for r in page] == ids[:2]
        assert [r.id for r in await documents.list_for(kb.id, after=ids[1], limit=10)] == ids[2:]

    async def test_tenants_never_see_each_others_documents(self, multi_tenant):  # noqa: F811
        with tenant_scope("acme"):
            kb = await _kb()
            row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="vault", sha256="s", size=1)
        with tenant_scope("globex"):
            assert await bases.list_bases() == []
            assert await documents.get(row.id) is None
            assert await documents.transition(row.id, "parsing") is None
            assert await documents.tombstone_under(kb.id) == []
        with tenant_scope("acme"):
            assert (await documents.get(row.id)).status == "queued"

    async def test_the_api_shapes(self):
        kb = await _kb()
        row, _ = await documents.upsert_for_path(kb, "contracts/a.md", source="upload", sha256="s", size=3,
                                                 modified_at=datetime(2026, 9, 30, tzinfo=timezone.utc))
        builder = ReportBuilder(doc_id=row.id, path=row.path, attempt=1, sha256="s")
        report = builder.finish("skipped", "unsupported_format")
        row = await documents.transition(row.id, "skipped", reason="unsupported_format",
                                         report=report.model_dump(mode="json"))
        out = documents.to_detail(row)
        assert out.status == "skipped" and out.report.retry.hint and out.metadata == {}
        assert out.created_at and documents.to_out(row).size_bytes == 3


class TestReport:
    def test_stages_are_timed_and_a_failure_keeps_its_code(self):
        builder = ReportBuilder(doc_id="kdoc_1", path="a.md", attempt=2)
        with builder.stage("parse") as st:
            st.partial(pages=3)
        with pytest.raises(EmbeddingError):
            with builder.stage("index"):
                raise EmbeddingError("model down")
        report = builder.finish("failed", "embed_failed")
        assert [(s.stage, s.status) for s in report.stages] == [("parse", "partial"), ("index", "failed")]
        assert report.stages[1].error.code == "embed_failed" and report.stages[1].error.retryable
        assert report.outcome == "failed" and report.retry.allowed and report.finished_at

    def test_what_a_retry_cannot_fix_says_so(self):
        assert retry_hint("over_size_cap").allowed is False
        assert retry_hint("unsupported_format").allowed is True
        assert error_info(RuntimeError("boom")).code == "internal"
