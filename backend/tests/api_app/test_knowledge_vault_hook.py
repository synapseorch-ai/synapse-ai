"""Every Vault write point announces the change, after the write, and never fails because of it (D157).

The Vault routes on a local Vault and on an object store, and `vault_backend`'s
`write` / `forget` (the engine's own writes). With Knowledge's observer
installed, a route save inside a knowledge base's folder queues its document.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _fakes.blob_stores import MemoryBlobStore
from core.storage import get_blob_store, set_blob_store
from core.vault_events import set_vault_observer


@pytest.fixture
def heard():
    seen = []

    async def observer(change):
        seen.append((change.op, change.path, change.is_dir, change.sha256 is not None, change.source))

    set_vault_observer(observer)
    return seen


@pytest.fixture(params=["local", "object"])
def store_kind(request):
    if request.param == "object":
        set_blob_store(MemoryBlobStore())
    return request.param


class TestTheRoutesAnnounce:
    async def test_create_update_folder_and_delete(self, client, heard, store_kind):
        assert (await client.post("/api/vault/folder", json={"path": "", "name": "kb"})).status_code == 200
        created = await client.post("/api/vault/file", json={"path": "kb", "name": "a.md", "content": "# A"})
        assert created.status_code == 200
        assert (await client.put("/api/vault/file", json={"path": "kb/a.md", "content": "# A2"})).status_code == 200
        assert (await client.request("DELETE", "/api/vault/item", json={"path": "kb/a.md"})).status_code == 200
        assert (await client.request("DELETE", "/api/vault/item", json={"path": "kb"})).status_code == 200
        assert heard[:4] == [("mkdir", "kb", True, False, "route"), ("write", "kb/a.md", False, True, "route"),
                             ("write", "kb/a.md", False, True, "route"), ("delete", "kb/a.md", False, False, "route")]
        # A folder delete: Knowledge tombstones by prefix, so `is_dir` is informative only.
        assert [h[:2] for h in heard[4:]] == [("delete", "kb")]

    async def test_the_hash_is_of_what_was_written(self, client, heard):
        import hashlib

        seen = []

        async def observer(change):
            seen.append(change)

        set_vault_observer(observer)
        await client.post("/api/vault/file", json={"path": "", "name": "n.txt", "content": "héllo"})
        assert seen[0].sha256 == hashlib.sha256("héllo".encode()).hexdigest() and seen[0].size == 6

    async def test_a_follower_that_breaks_never_breaks_the_write(self, client):
        async def broken(change):
            raise RuntimeError("indexer down")

        set_vault_observer(broken)
        resp = await client.post("/api/vault/file", json={"path": "", "name": "a.md", "content": "kept"})
        assert resp.status_code == 200 and get_blob_store().get("vault/a.md") == "kept"

    async def test_a_working_copy_write_is_not_the_vault(self, client, heard):
        set_blob_store(MemoryBlobStore())
        from core.vault import _vault_root

        _vault_root().mkdir(parents=True, exist_ok=True)
        resp = await client.post("/api/vault/file", json={"path": "", "name": "a.md", "source": "local"})
        assert resp.status_code == 200 and heard == []


class TestTheEnginesWritesAnnounce:
    async def test_write_and_forget(self, heard, store_kind):
        from core.vault_backend import get_vault

        vault = get_vault()
        target = Path(vault.root) / "kb" / "note.md"
        vault.write(target, "saved by a tool")
        vault.forget(target)
        for _ in range(5):
            await asyncio.sleep(0)
        assert heard == [("write", "kb/note.md", False, True, "tool"), ("delete", "kb/note.md", False, False, "tool")]

    def test_no_loop_and_no_observer_schedule_nothing(self, tmp_path):
        from core.vault_backend import LocalVault

        vault = LocalVault(tmp_path)
        vault.write(tmp_path / "a.md", "no observer")  # no loop running here either
        set_vault_observer(lambda change: None)
        vault.write(tmp_path / "a.md", "an observer, but no loop")
        assert (tmp_path / "a.md").read_text() == "an observer, but no loop"


async def test_a_route_save_inside_a_knowledge_base_queues_it(client):
    from core.knowledge import bases, documents, seams, sync
    from core.knowledge.api_models import KnowledgeBaseCreate
    from core.knowledge.testing.fakes import RecordingDispatcher

    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    await client.post("/api/settings", json={"knowledge_enabled": True})
    kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
    await sync.ensure_folder(kb)
    sync.install()
    await client.post("/api/vault/file", json={"path": "kb", "name": "a.md", "content": "# Hello"})
    row = await documents.get_by_path(kb.id, "kb/a.md")
    assert row.status == "queued" and [j.path for j in recorder.jobs] == ["kb/a.md"]
