"""Following the Vault (D157): the hook queues and tombstones documents as files change, and
reconcile brings a knowledge base in step with its folder for every write the
hook never saw. SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from _fakes.blob_stores import MemoryBlobStore
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from core.knowledge import bases, dispatch, documents, seams, sync
from core.knowledge.api_models import KnowledgeBaseCreate, KnowledgeBaseUpdate
from core.knowledge.testing.fakes import RecordingDispatcher
from core.knowledge.types import IngestJob
from core.storage import get_blob_store, set_blob_store
from core.vault_events import VaultChange, get_vault_observer, notify_vault_change

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    yield
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


@pytest.fixture(params=["local", "object"])
def store_kind(request):
    """The Vault in a directory, and in a store with no filesystem."""
    if request.param == "object":
        set_blob_store(MemoryBlobStore())
    return request.param


async def _kb(folder="kb", **kw):
    return await bases.create_base(KnowledgeBaseCreate(name="KB", folder=folder, **kw))


def _ops(recorder):
    return [(j.op, j.path) for j in recorder.jobs]


class TestTheHook:
    async def test_a_write_under_a_base_queues_its_document(self, jobs, store_kind):
        kb = await _kb()
        write_vault("kb/a.md", "hello")
        sync.install()
        await notify_vault_change("kb/a.md", "write", source="route")
        row = await documents.get_by_path(kb.id, "kb/a.md")
        assert row.status == "queued" and row.size_bytes == 5 and row.sha256 and row.source_modified_at
        assert _ops(jobs) == [("upsert", "kb/a.md")] and jobs.jobs[0].reason == "vault_write"

    async def test_a_write_outside_every_base_is_ignored(self, jobs):
        await _kb()
        write_vault("elsewhere/a.md", "x")
        write_vault("kb2/b.md", "x")
        sync.install()
        await notify_vault_change("elsewhere/a.md", "write")
        await notify_vault_change("kb2/b.md", "write")
        assert jobs.jobs == []

    async def test_dotfiles_and_temp_files_are_not_documents(self, jobs):
        await _kb()
        write_vault("kb/.keep", "\n")
        await sync.on_vault_change(VaultChange(path="kb/.keep", op="write"))
        await sync.on_vault_change(VaultChange(path="kb/tmpab12.tmp", op="write"))
        assert jobs.jobs == []

    async def test_the_same_content_twice_is_one_job(self, jobs):
        await _kb()
        write_vault("kb/a.md", "hello")
        for _ in range(3):
            await sync.on_vault_change(VaultChange(path="kb/a.md", op="write"))
        assert _ops(jobs) == [("upsert", "kb/a.md")]

    async def test_deleting_a_folder_tombstones_what_is_under_it(self, jobs):
        kb = await _kb()
        for path in ("kb/sub/a.md", "kb/sub/b/c.md", "kb/subway.md"):
            write_vault(path, path)
            await sync.on_vault_change(VaultChange(path=path, op="write"))
        jobs.jobs.clear()
        await sync.on_vault_change(VaultChange(path="kb/sub", op="delete", is_dir=True))
        assert sorted(_ops(jobs)) == [("delete", "kb/sub/a.md"), ("delete", "kb/sub/b/c.md")]
        assert (await documents.get_by_path(kb.id, "kb/subway.md")).status == "queued"

    async def test_deleting_a_folder_above_a_base_takes_all_of_it(self, jobs):
        kb = await _kb(folder="team/kb")
        write_vault("team/kb/a.md", "x")
        await sync.on_vault_change(VaultChange(path="team/kb/a.md", op="write"))
        await sync.on_vault_change(VaultChange(path="team", op="delete", is_dir=True))
        assert (await documents.get_by_path(kb.id, "team/kb/a.md")).status == "deleting"

    async def test_a_move_is_a_delete_and_a_write(self, jobs):
        kb = await _kb()
        write_vault("kb/a.md", "x")
        await sync.on_vault_change(VaultChange(path="kb/a.md", op="write"))
        write_vault("kb/b.md", "x")
        await sync.on_vault_change(VaultChange(path="kb/b.md", op="move", old_path="kb/a.md"))
        assert _ops(jobs)[1:] == [("delete", "kb/a.md"), ("upsert", "kb/b.md")]
        assert (await documents.get_by_path(kb.id, "kb/b.md")).status == "queued"

    async def test_a_file_gone_before_the_hook_looked_is_a_delete(self, jobs):
        kb = await _kb()
        write_vault("kb/a.md", "x")
        await sync.on_vault_change(VaultChange(path="kb/a.md", op="write"))
        get_blob_store().delete("vault/kb/a.md")
        await sync.on_vault_change(VaultChange(path="kb/a.md", op="write"))
        assert (await documents.get_by_path(kb.id, "kb/a.md")).status == "deleting"

    async def test_off_means_the_hook_does_nothing(self, jobs):
        from core.routes.settings import save_settings

        await _kb()
        write_vault("kb/a.md", "x")
        await save_settings({"knowledge_enabled": False})
        sync.install()
        await notify_vault_change("kb/a.md", "write")
        assert jobs.jobs == [] and get_vault_observer() is sync.observe

    async def test_the_engines_folders_cost_no_database_read(self, monkeypatch):
        async def boom():
            raise AssertionError("read the knowledge bases for a tool output")

        monkeypatch.setattr(bases, "list_bases", boom)
        await sync.observe(VaultChange(path="tool_outputs/run/x.json", op="write"))

    async def test_a_disabled_base_is_not_followed(self, jobs):
        kb = await _kb()
        await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, status="disabled"))
        write_vault("kb/a.md", "x")
        await sync.on_vault_change(VaultChange(path="kb/a.md", op="write"))
        assert jobs.jobs == []


class TestReconcile:
    async def test_new_changed_and_missing_files(self, jobs, store_kind):
        kb = await _kb()
        write_vault("kb/keep.md", "same")
        write_vault("kb/edit.md", "before")
        write_vault("kb/gone.md", "bye")
        first = await sync.reconcile(kb)
        assert (first.added, first.changed, first.removed) == (3, 0, 0)
        jobs.jobs.clear()
        assert not (await sync.reconcile(kb)).any()  # in step: nothing hashed, nothing queued
        write_vault("kb/edit.md", "after, and longer")
        write_vault("kb/new.md", "hi")
        get_blob_store().delete("vault/kb/gone.md")
        drift = await sync.reconcile(kb)
        assert (drift.added, drift.changed, drift.removed) == (1, 1, 1)
        assert sorted(_ops(jobs)) == [("delete", "kb/gone.md"), ("upsert", "kb/edit.md"), ("upsert", "kb/new.md")]

    async def test_a_touched_file_is_not_queued_again(self, jobs):
        kb = await _kb()
        write_vault("kb/a.md", "same")
        await sync.reconcile(kb)
        jobs.jobs.clear()
        path = get_blob_store().path_for("vault/kb/a.md")
        later = datetime.now().timestamp() + 60
        os.utime(path, (later, later))
        drift = await sync.reconcile(kb)
        assert (drift.touched, drift.changed) == (1, 0) and jobs.jobs == []
        assert not (await sync.reconcile(kb)).any()

    async def test_a_failed_document_is_not_retried_every_minute(self, jobs):
        kb = await _kb()
        write_vault("kb/a.md", "content")
        await sync.reconcile(kb)
        row = await documents.get_by_path(kb.id, "kb/a.md")
        await documents.transition(row.id, "parsing")
        await documents.transition(row.id, "failed", reason="internal")
        jobs.jobs.clear()
        assert not (await sync.reconcile(kb)).any() and jobs.jobs == []

    async def test_work_left_behind_is_queued_again(self, jobs):
        kb = await _kb()
        for name in ("a", "b", "c", "d"):
            write_vault(f"kb/{name}.md", name)
        await sync.reconcile(kb)
        rows = {r.path: r for r in await documents.list_for(kb.id)}
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        await documents.transition(rows["kb/a.md"].id, "parsing", started_at=old)
        await documents.transition(rows["kb/b.md"].id, "parsing", started_at=datetime.now(timezone.utc))
        await documents.set_columns(rows["kb/c.md"].id, queued_at=old)
        dispatch.reset()  # c's job was lost with the process that queued it
        jobs.jobs.clear()
        drift = await sync.reconcile(kb)
        assert drift.stuck == 2 and sorted(_ops(jobs)) == [("upsert", "kb/a.md"), ("upsert", "kb/c.md")]
        assert (await documents.get(rows["kb/b.md"].id)).status == "parsing"  # recent: still someone's
        jobs.jobs.clear()
        assert (await sync.reconcile(kb, startup=True)).stuck >= 1  # at startup nothing is still running
        assert ("upsert", "kb/b.md") in _ops(jobs)

    async def test_a_failed_index_delete_is_tried_again(self, jobs):
        kb = await _kb()
        write_vault("kb/a.md", "x")
        await sync.reconcile(kb)
        row = await documents.get_by_path(kb.id, "kb/a.md")
        await documents.tombstone(row.id)
        await documents.set_columns(row.id, updated_at=datetime.now(timezone.utc) - timedelta(hours=1))
        jobs.jobs.clear()
        await sync.reconcile(kb)
        assert _ops(jobs) == [("delete", "kb/a.md")]

    async def test_a_format_that_becomes_readable_is_queued_again(self, jobs, monkeypatch):
        import core.knowledge.parse as parse

        monkeypatch.setenv("SYNAPSE_SOFFICE", "/nonexistent/soffice")  # a .doc needs LibreOffice (A2)
        kb = await _kb()
        write_vault("kb/a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)
        await sync.reconcile(kb)
        row = await documents.get_by_path(kb.id, "kb/a.doc")
        await documents.transition(row.id, "parsing")
        await documents.transition(row.id, "skipped", reason="unsupported_format")
        jobs.jobs.clear()
        assert not (await sync.reconcile(kb)).any()
        monkeypatch.setattr(parse, "PARSED_EXTENSIONS", parse.PARSED_EXTENSIONS | {".doc"})
        assert (await sync.reconcile(kb)).supported == 1 and _ops(jobs) == [("upsert", "kb/a.doc")]

    async def test_has_drift_asks_what_reconcile_would_look_at_without_hashing_or_writing(
            self, jobs, store_kind, monkeypatch):
        """Cloud's fleet cannot parse, so it asks before waking the process that can (T1a, D169): a file added,
        resized or re-dated since its document was recorded, or a document whose file is gone. Read-only."""
        kb = await _kb()
        assert not await sync.has_drift(kb)
        write_vault("kb/a.md", "same")
        write_vault("kb/b.md", "other")
        assert await sync.has_drift(kb), "a new file"
        await sync.reconcile(kb)
        jobs.jobs.clear()
        hashed: list[str] = []
        real = sync._fingerprint

        async def spy(path):
            hashed.append(path)
            return await real(path)

        monkeypatch.setattr(sync, "_fingerprint", spy)
        before = [(r.path, r.status, r.updated_at) for r in await documents.list_for(kb.id)]
        assert not await sync.has_drift(kb), "in step"
        write_vault("kb/a.md", "changed, and longer")
        assert await sync.has_drift(kb), "a resized file"
        assert hashed == [] and jobs.jobs == []
        assert [(r.path, r.status, r.updated_at) for r in await documents.list_for(kb.id)] == before
        await sync.reconcile(kb)
        get_blob_store().delete("vault/kb/b.md")
        assert await sync.has_drift(kb), "a file gone"
        await sync.reconcile(kb)
        assert not await sync.has_drift(kb), "its tombstone is not drift"

    async def test_a_store_that_dates_a_file_two_ways_settles_after_one_pass(self, jobs):
        """S3 dates an object twice: a HEAD (`stat`) to the whole second, a LIST (`list_stats`)
        to the millisecond — measured on MinIO, 33.000 against 33.456 (T1a, D169). Reconcile
        compares the listing, so it records the listing's time; recording the HEAD's made every
        pass on S3 read and hash every file in the base again, and `has_drift` never settle."""
        from core.storage.base import BlobStat

        class TwoClocks(MemoryBlobStore):
            def stat(self, key):
                found = super().stat(key)
                if found is None:
                    return None
                return BlobStat(key=found.key, size=found.size,
                                modified_at=found.modified_at.replace(microsecond=0))

        store = TwoClocks()
        set_blob_store(store)
        kb = await _kb()
        write_vault("kb/a.md", "hello")
        store.times = {k: v.replace(microsecond=456_000) for k, v in store.times.items()}
        assert (await sync.reconcile(kb)).added == 1
        jobs.jobs.clear()
        assert not (await sync.reconcile(kb)).any(), "the second pass reads nothing"
        assert not await sync.has_drift(kb)

    async def test_a_disabled_base_never_drifts(self, jobs):
        kb = await _kb()
        _, off = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, status="disabled"))
        write_vault("kb/a.md", "x")
        assert not await sync.has_drift(off)

    async def test_a_disabled_base_is_left_alone(self, jobs):
        kb = await _kb()
        _, off = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, status="disabled"))
        write_vault("kb/a.md", "x")
        assert not (await sync.reconcile(off)).any() and jobs.jobs == []

    async def test_a_job_can_ask_for_it(self, jobs):
        from core.knowledge.jobs import run_ingest_job

        kb = await _kb()
        write_vault("kb/a.md", "x")
        await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="reindex", reason="reconcile"))
        assert _ops(jobs) == [("upsert", "kb/a.md")]

    async def test_a_new_folder_takes_the_documents_with_it(self, jobs):
        kb = await _kb()
        write_vault("kb/a.md", "a")
        write_vault("other/b.md", "b")
        await sync.reconcile(kb)
        before, after = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=1, folder="other"))
        jobs.jobs.clear()
        await sync.rehome(before, after)
        assert sorted(_ops(jobs)) == [("delete", "kb/a.md"), ("reindex", None)]
        await sync.reconcile(after)
        assert ("upsert", "other/b.md") in _ops(jobs)

    async def test_a_new_base_gets_its_folder(self, store_kind):
        kb = await _kb(folder="fresh/kb")
        await sync.ensure_folder(kb)
        store = get_blob_store()
        if store_kind == "local":
            assert store.path_for("vault/fresh/kb").is_dir()
        else:
            assert store.get("vault/fresh/kb/.keep") == "\n"
        assert not (await sync.reconcile(kb)).any()  # the marker is not a document


class TestTheLoop:
    async def test_it_reconciles_at_startup_then_on_a_timer_and_never_while_off(self, jobs, monkeypatch):
        from core.routes.settings import save_settings

        kb = await _kb()
        write_vault("kb/a.md", "x")
        passes: list[bool] = []
        real = sync.reconcile_all

        async def counted(*, startup=False):
            passes.append(startup)
            return await real(startup=startup)

        sleeps = [0]

        async def tick(seconds):
            sleeps[0] += 1
            if sleeps[0] == 2:
                await save_settings({"knowledge_enabled": False})
            if sleeps[0] == 3:
                raise asyncio.CancelledError

        monkeypatch.setattr(sync, "reconcile_all", counted)
        monkeypatch.setattr(sync.asyncio, "sleep", tick)
        with pytest.raises(asyncio.CancelledError):
            await sync.reconcile_forever(interval=60)
        assert passes == [True, False] and (await documents.get_by_path(kb.id, "kb/a.md")) is not None

    async def test_start_installs_the_hook(self, monkeypatch):
        async def idle(interval=60):
            await asyncio.Event().wait()

        monkeypatch.setattr(sync, "reconcile_forever", idle)
        task = sync.start()
        try:
            assert get_vault_observer() is sync.observe and not task.done()
        finally:
            task.cancel()
