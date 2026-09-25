"""
A backend with an S3 bucket finishes starting (F225), and no error while the
native MCP servers are built can hang startup again (F177).

The lifespan starts `_filesystem_mcp_manager` and waits for `_filesystem_ready`,
which the task set only after `_connect_filesystem_mcp` returned. That function
awaits `_build_native_mcp_servers()` outside its own `try`, so anything the
builder raised killed the task before the event was set, and the lifespan
waited for ever with no log: F176's NameError did it, and so did the Memory
server's `tenant_dir("memory")` on any install with an S3 bucket — since F57,
OSS installs the S3 blob store at startup, and `S3BlobStore.path_for` is None
by design, so `tenant_dir` raises `NoDurableStorage` (Naveen's own install,
2026-09-25). The unit tests' app skips the lifespan, so none of them noticed.
"""
import asyncio

import pytest

from core.storage import set_blob_store
from core.storage.base import BlobStore


class _NoFilesystemStore(BlobStore):
    """What S3 looks like to `tenant_dir`: a store with no directory."""

    def put(self, key, content):
        raise AssertionError("nothing is written while the list is built")

    def get(self, key):
        return None

    def delete(self, key):
        return False

    def list(self, prefix=""):
        return []

    def path_for(self, key):
        return None


@pytest.fixture
def native_list(monkeypatch, tmp_path):
    """The native server list's inputs, with the default (local) blob store."""
    import core.server as server
    import core.vault as vault

    async def no_google():
        return {}

    async def filesystem_spec():
        return {"name": "Filesystem", "command": "npx", "args": []}

    # The vault keeps a working copy for a store with no directory; the list
    # needs only its path.
    monkeypatch.setattr(vault, "_vault_root", lambda: tmp_path / "vault")
    monkeypatch.setattr(server, "_get_google_oauth_env", no_google)
    monkeypatch.setattr(server, "_filesystem_server_spec", filesystem_spec)


@pytest.fixture
def s3_like_store(native_list):
    set_blob_store(_NoFilesystemStore())
    yield
    set_blob_store(None)


async def test_the_native_server_list_builds_with_an_s3_blob_store(s3_like_store):
    import core.server as server

    servers = await server._build_native_mcp_servers()

    names = [s["name"] for s in servers]
    assert "Filesystem" in names and "Browser Automation" in names


async def test_the_memory_mcp_server_is_not_a_native_server(native_list):
    """F226: the engine's agent memory replaced it. It asked the blob store for
    a directory on every start — the ask that hung an S3 install (F225)."""
    import core.server as server

    servers = await server._build_native_mcp_servers()

    assert "Memory" not in [s["name"] for s in servers]
    assert not any("server-memory" in " ".join(s.get("args", [])) for s in servers)


async def test_a_builder_error_does_not_hang_startup(monkeypatch, capsys):
    """F177: the manager says it is done whatever the first attempt did."""
    import core.server as server

    async def broken():
        raise RuntimeError("the builder broke")

    monkeypatch.setattr(server, "_build_native_mcp_servers", broken)
    monkeypatch.setattr(server, "_filesystem_ready", asyncio.Event())
    monkeypatch.setattr(server, "_filesystem_restart_queue", asyncio.Queue())

    task = asyncio.create_task(server._filesystem_mcp_manager())
    try:
        await asyncio.wait_for(server._filesystem_ready.wait(), timeout=2)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    assert "the builder broke" in capsys.readouterr().out
