"""A multi-tenant worker serves the vault tools in its own process, one server per tenant (F1118).

The vault's four tools read the blob store by key, and the store applies the tenant itself
(`core/storage/base.py::tenant_key`). A subprocess added nothing but a copy of the worker's
environment, so a worker runs them in-process instead: one server per tenant's pool entry,
built in that tenant's scope, over in-memory streams.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from core.scale.context import set_resource_provider
from core.storage import set_blob_store
from core.storage.base import LocalBlobStore
from core.tenancy import tenant_scope


class _Provider:
    async def resolve_agents(self):
        return []

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BLOB_DIR", str(tmp_path))
    store = LocalBlobStore(tmp_path)
    set_blob_store(store)
    set_resource_provider(_Provider())
    with tenant_scope("org-a"):
        store.put("vault/a-notes.txt", "org a's notes")
    with tenant_scope("org-b"):
        store.put("vault/b-notes.txt", "org b's notes")
    yield store
    set_resource_provider(None)
    set_blob_store(None)


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def _listing_and_read(tenant: str, path: str) -> tuple[list[str], dict]:
    from core.vault_tools import build_server

    with tenant_scope(tenant):
        async with create_connected_server_and_client_session(build_server()) as session:
            await asyncio.sleep(0)
            listing = _payload(await session.call_tool("list_files", {}))
            read = _payload(await session.call_tool("read_file", {"path": path}))
    return listing["files"], read


@pytest.mark.anyio
async def test_each_tenant_sees_only_its_own_vault(vault):
    files, read = await _listing_and_read("org-a", "a-notes.txt")

    assert files == ["a-notes.txt"]
    assert read["content"] == "org a's notes"


@pytest.mark.anyio
async def test_two_tenants_at_once_never_see_each_others_files(vault):
    (a_files, a_read), (b_files, b_read) = await asyncio.gather(
        _listing_and_read("org-a", "b-notes.txt"),
        _listing_and_read("org-b", "a-notes.txt"),
    )

    assert a_files == ["a-notes.txt"] and b_files == ["b-notes.txt"]
    assert "content" not in a_read and "content" not in b_read


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["../logs/run.log", "/etc/hostname", "../../org-a/vault/a-notes.txt", "notes/../../memory/x"],
)
async def test_a_path_outside_the_vault_is_refused(vault, path):
    _, read = await _listing_and_read("org-b", path)

    assert "content" not in read, read
    assert "org a's notes" not in json.dumps(read)


def test_a_multi_tenant_worker_spawns_no_vault_subprocess():
    from core.scale.worker_server_module import (
        _BACKEND_ROOT,
        _TOOLS_DIR,
        InProcessServer,
        _get_native_mcp_servers,
    )

    per_tenant = _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="tenant")

    assert isinstance(per_tenant["vault"], InProcessServer)


@pytest.mark.anyio
async def test_closing_a_tenants_module_closes_its_vault_server(vault):
    from core.scale.worker_server_module import WorkerServerModule

    with tenant_scope("org-a"):
        module = await WorkerServerModule.build_for_tenant(
            disabled_mcp_names=["sql"], connect_user=False
        )
        try:
            session = module.agent_sessions["vault"]
            assert _payload(await session.call_tool("list_files", {}))["files"] == ["a-notes.txt"]
            handles = [h for h in module._servers if h.name == "vault"]
        finally:
            await module.close()

    assert handles and all(h.task.done() for h in handles)
    with pytest.raises(Exception):
        await asyncio.wait_for(session.call_tool("list_files", {}), timeout=2)
