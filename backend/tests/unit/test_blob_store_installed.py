"""
A configured bucket is the blob store in every process that reads the vault (F57).

`S3BlobStore` existed and nothing in the shipped product installed it: the API
and the workers kept vault files, run logs and notifications in
`SYNAPSE_BLOB_DIR`, and `S3_*` only fed a mirror inside `tools/sandbox.py` that
copied the sandbox's own vault writes — under keys with no tenant in them. A
multi-host scale deployment needed a shared directory the docs never mentioned.

And a tool subprocess never installed any store, so it read local disk whatever
its parent used. Measured on cloud's dev stack (2026-09-17): the fleet's `vault`
tool bootstraps to `LocalBlobStore` and lists an empty vault for an org whose
files are in MinIO.
"""
import json

import pytest

from core.storage import LocalBlobStore, S3BlobStore, get_blob_store, set_blob_store
from tests.unit.test_vault_backend import _Pathless


@pytest.fixture
def no_bucket(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)


class TestTheConfiguredStoreIsInstalled:
    def test_a_bucket_installs_the_object_store(self, monkeypatch):
        from core.storage import install_configured_blob_store
        monkeypatch.setenv("S3_BUCKET", "synapse-test")

        assert install_configured_blob_store() is True
        assert isinstance(get_blob_store(), S3BlobStore)

    def test_no_bucket_keeps_the_directory(self, no_bucket):
        from core.storage import install_configured_blob_store

        assert install_configured_blob_store() is False
        assert isinstance(get_blob_store(), LocalBlobStore)

    async def test_a_tool_subprocess_uses_the_bucket_too(self, monkeypatch):
        from core import settings_runtime, tool_server
        from core.config import set_settings_provider
        monkeypatch.setenv("S3_BUCKET", "synapse-test")
        monkeypatch.delenv("SYNAPSE_TENANT_ID", raising=False)
        try:
            await tool_server.bootstrap()
            assert isinstance(get_blob_store(), S3BlobStore)
        finally:
            set_settings_provider(None)
            settings_runtime.reset_state()


@pytest.fixture
def object_store():
    store = _Pathless()
    set_blob_store(store)
    return store


def _json(result):
    return json.loads(result[0].text)


class TestTheSandboxWritesThroughTheVault:
    """The sandbox's vault tools go through `get_vault()`, so they reach
    whatever store is installed, under the tenant — not a side copy."""

    def test_a_write_lands_in_the_installed_store(self, object_store):
        from core.storage.base import tenant_key
        from tools import sandbox

        out = _json(sandbox._handle_write({"path": "notes/a.md", "content": "hello"}))

        assert out.get("written") is True, out
        assert object_store.blobs[tenant_key("vault/notes/a.md")] == "hello"

    def test_a_file_only_the_store_has_is_read_listed_patched_and_deleted(self, object_store):
        from core.storage.base import tenant_key
        from tools import sandbox
        object_store.put("vault/reports/q1.md", "draft one")

        listed = _json(sandbox._handle_list({"directory": "reports"}))
        assert [f["relative"] for f in listed["files"]] == ["reports/q1.md"]

        assert _json(sandbox._handle_read({"path": "reports/q1.md"}))["content"] == "draft one"

        patched = _json(sandbox._handle_patch({"path": "reports/q1.md", "find": "one", "replace": "two"}))
        assert patched.get("patched") is True, patched
        assert object_store.blobs[tenant_key("vault/reports/q1.md")] == "draft two"

        assert _json(sandbox._handle_delete({"path": "reports/q1.md"})).get("deleted") is True
        assert tenant_key("vault/reports/q1.md") not in object_store.blobs

    def test_create_refuses_a_file_the_store_already_has(self, object_store):
        from tools import sandbox
        object_store.put("vault/taken.json", "{}")

        out = _json(sandbox._handle_create({"filename": "taken.json", "content": "{}"}))

        assert "already exists" in out.get("error", ""), out


class TestTheNotificationRingFollowsTheStore:
    def test_reload_reads_the_store_installed_after_import(self, object_store):
        """The hub is built at import, before the server installs its store."""
        from core.notifications import NotificationHub
        set_blob_store(None)
        hub = NotificationHub()
        object_store.put("notifications.json", json.dumps([{"id": 7, "kind": "run_completed",
                                                             "title": "t", "run_id": "r"}]))
        set_blob_store(object_store)

        hub.reload()

        assert [i["id"] for i in hub.list()] == [7]
        assert hub.last_id() == 7
