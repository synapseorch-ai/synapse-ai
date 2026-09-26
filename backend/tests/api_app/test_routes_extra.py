"""
Extra route coverage: vault CRUD, schedules CRUD, import/export, logs,
api-keys, and MCP listing. File/JSON-backed handlers, all infra-free.
"""
import pytest

from _fakes import seed as S


class TestVaultCrud:
    async def test_folder_file_create_read_delete(self, client):
        assert (await client.post("/api/vault/folder", json={"path": "", "name": "docs"})).status_code < 400
        r = await client.post("/api/vault/file",
                              json={"path": "docs", "name": "note.md", "content": "# Hello"})
        assert r.status_code < 400
        tree = await client.get("/api/vault/tree")
        assert tree.status_code == 200
        got = await client.get("/api/vault/file", params={"path": "docs/note.md"})
        assert got.status_code == 200
        assert "Hello" in got.text
        dele = await client.request("DELETE", "/api/vault/item", json={"path": "docs/note.md"})
        assert dele.status_code < 400

    async def test_vault_search(self, client):
        await client.post("/api/vault/file", json={"path": "", "name": "s.txt", "content": "findme here"})
        resp = await client.get("/api/vault/search", params={"q": "findme"})
        assert resp.status_code == 200


class TestSchedulesCrud:
    def _body(self, target_id="a1"):
        return {"name": "Nightly", "target_type": "agent", "target_id": target_id,
                "prompt": "run", "schedule_type": "interval",
                "interval_value": 5, "interval_unit": "minutes"}

    async def test_schedule_create_get_update_delete(self, client, test_app, seed_agent):
        # create/update only gate on a non-None manager; the work uses the store.
        test_app.state.schedule_manager = object()
        await seed_agent(id="a1")  # a schedule's target must exist (F197)
        created = await client.post("/api/schedules", json=self._body())
        assert created.status_code == 200
        sid = created.json()["id"]
        assert (await client.get(f"/api/schedules/{sid}")).status_code == 200
        upd = await client.put(f"/api/schedules/{sid}", json={**self._body(), "enabled": False})
        assert upd.status_code == 200
        assert (await client.delete(f"/api/schedules/{sid}")).status_code == 200


class TestImportExport:
    async def test_export_then_import_roundtrip(self, client, seed_agent):
        agent = await seed_agent(id="exp_agent", name="Exportable")
        exported = await client.post("/api/export", json={"agent_ids": [agent["id"]]})
        assert exported.status_code == 200
        bundle = exported.json()
        # Re-import the same bundle (idempotent-ish; must not error).
        imported = await client.post("/api/import", json={"bundle": bundle})
        assert imported.status_code < 500

    async def test_export_data_dump(self, client):
        assert (await client.get("/api/export/data")).status_code == 200


class TestLogsRoutes:
    @pytest.mark.parametrize("kind", ["agents", "orchestrations", "schedules"])
    async def test_log_lists(self, client, kind):
        assert (await client.get(f"/api/logs/{kind}")).status_code == 200

    async def test_unknown_log_run(self, client):
        resp = await client.get("/api/logs/agents/ghost-run")
        assert resp.status_code in (404, 200)
        assert (await client.delete("/api/logs/agents/ghost-run")).status_code < 500


class TestApiKeysAndMcp:
    async def test_api_keys_list(self, client):
        resp = await client.get("/api/settings/api-keys")
        assert resp.status_code == 200
        assert isinstance(resp.json(), (list, dict))

    async def test_mcp_servers_list(self, client):
        resp = await client.get("/api/mcp/servers")
        assert resp.status_code == 200


class TestMcpServersKeepTheirSecrets:
    """F135: the list route returned each server's stored config as is — the
    bearer token in clear, and a local server's environment values (its API
    keys) with it — and the add route echoed the token back. What the screen
    needs is whether a token is set, and the name an older preset filled in
    (F103), which is an environment variable's name, not a secret."""

    _SERVERS = [
        {"name": "gh", "server_type": "remote", "url": "https://x.test/mcp",
         "token": "ghp_realsecret", "status": "connected"},
        {"name": "old", "server_type": "remote", "url": "https://y.test/mcp",
         "token": "SLACK_CLIENT_ID", "status": "disconnected"},
        {"name": "local", "server_type": "stdio", "command": "uvx", "args": ["x"],
         "env": {"API_KEY": "sk-secret"}, "status": "connected"},
    ]

    async def test_the_list_serves_no_token_or_environment_value(self, client, monkeypatch):
        import copy
        import json
        import types

        import core.server as server

        manager = types.SimpleNamespace(servers_config=copy.deepcopy(self._SERVERS))
        monkeypatch.setattr(server, "mcp_manager", manager, raising=False)

        body = (await client.get("/api/mcp/servers")).json()

        assert "ghp_realsecret" not in json.dumps(body)
        assert "sk-secret" not in json.dumps(body)
        by = {s["name"]: s for s in body}
        assert (by["gh"]["token_set"], by["gh"]["token_placeholder"]) == (True, None)
        assert by["old"]["token_placeholder"] == "SLACK_CLIENT_ID"
        assert by["local"]["env"] == {"API_KEY": ""} and by["local"]["token_set"] is False
        assert manager.servers_config == self._SERVERS, "the stored config keeps its secrets"

    async def test_the_add_response_serves_no_token(self, client, monkeypatch):
        import types

        import core.server as server

        async def add_server(**kw):
            return {"config": {"name": kw["name"], "server_type": "remote", "url": kw["url"],
                               "token": kw["token"], "status": "disconnected"},
                    "connected": False, "status": "disconnected"}

        monkeypatch.setattr(server, "mcp_manager",
                            types.SimpleNamespace(servers_config=[], add_server=add_server), raising=False)

        resp = await client.post("/api/mcp/servers", json={
            "name": "n", "server_type": "remote", "url": "https://z.test/mcp", "token": "ghp_realsecret"})

        assert resp.status_code == 200, resp.text
        assert "ghp_realsecret" not in resp.text
        assert resp.json()["config"]["token_set"] is True
