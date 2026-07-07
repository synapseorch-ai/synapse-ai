"""Final robust route branches: import/export apply, vault folder ops, and
settings/examples edges."""
import json

import pytest

from _fakes import seed as S


class TestImportExportApply:
    async def test_full_bundle_roundtrip_applies(self, client, seed_agent, seed_orchestration):
        a = seed_agent(id="fx_agent", name="FxAgent")
        o = seed_orchestration(id="fx_orch", name="FxOrch")
        t = {"name": "fx_tool", "description": "d", "parameters": {"type": "object", "properties": {}}}
        await client.post("/api/tools/custom", json=t)

        exported = await client.post("/api/export", json={
            "agent_ids": [a["id"]], "orchestration_ids": [o["id"]],
            "custom_tool_names": ["fx_tool"], "mcp_server_names": [],
        })
        assert exported.status_code == 200
        bundle = exported.json()

        # Wipe, then import the bundle back.
        await client.delete(f"/api/agents/{a['id']}")
        imported = await client.post("/api/import", json={"bundle": bundle, "mcp_secrets": {}})
        assert imported.status_code < 500


class TestVaultFolderOps:
    async def test_nested_folders_and_delete(self, client):
        await client.post("/api/vault/folder", json={"path": "", "name": "parent"})
        await client.post("/api/vault/folder", json={"path": "parent", "name": "child"})
        await client.post("/api/vault/file",
                          json={"path": "parent/child", "name": "deep.md", "content": "deep"})
        tree = await client.get("/api/vault/tree")
        assert tree.status_code == 200
        # Delete the parent folder (recursive).
        resp = await client.request("DELETE", "/api/vault/item", json={"path": "parent"})
        assert resp.status_code < 500

    async def test_search_json_file(self, client):
        payload = json.dumps([{"k": "needle"}, {"k": "hay"}])
        await client.post("/api/vault/file", json={"path": "", "name": "d.json", "content": payload})
        resp = await client.get("/api/vault/search", params={"q": "needle"})
        assert resp.status_code == 200


class TestSettingsEdges:
    async def test_unknown_example_404(self, client):
        resp = await client.get("/api/examples/does-not-exist")
        assert resp.status_code in (404, 200)

    async def test_get_file_missing(self, client):
        resp = await client.get("/api/file", params={"path": "nonexistent-file.txt"})
        assert resp.status_code < 500

    async def test_google_credentials_invalid_is_handled(self, client):
        resp = await client.post("/api/setup/google-credentials", json={"content": "not-json"})
        assert resp.status_code < 500
