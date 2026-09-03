"""
Broad CRUD + read coverage across the app route modules (settings, usage,
sessions, vault, tools, repos, db_configs, logs, schedules, import/export,
profiling, data). Read/list handlers are hit generically; create/delete
roundtrips use the modules whose request models are simple + infra-free.
"""
import json

import pytest


# ── safe read-only endpoints: must never 500 ──────────────────────────────────
SAFE_GETS = [
    "/api/status", "/api/settings", "/api/config",
    "/api/personal-details", "/api/examples",
    "/api/usage/summary", "/api/usage/cache_summary", "/api/usage/logs",
    "/api/usage/pricing",
    "/api/sessions",
    "/api/vault/tree", "/api/vault/search?q=test",
    "/api/tools/custom", "/api/tools/docker/status",
    "/api/repos", "/api/db-configs",
    "/api/logs/agents", "/api/logs/orchestrations", "/api/logs/schedules",
    "/api/schedules",
    "/api/export/data",
    "/api/models",
    "/api/synthetic/status", "/api/synthetic/datasets",
    "/api/messaging/channels",
    "/stats", "/status",
]


@pytest.mark.parametrize("path", SAFE_GETS)
async def test_safe_get_never_crashes(client, path):
    resp = await client.get(path)
    # A handled "not enabled/available" (501/503) is fine; an unhandled 500 is not.
    assert resp.status_code not in (500, 502, 504), f"{path} -> {resp.status_code}: {resp.text[:200]}"


class TestSettingsRoutes:
    async def test_get_and_update_settings(self, client):
        resp = await client.post("/api/settings", json={"agent_name": "Cov Agent", "model": "mistral"})
        assert resp.status_code == 200
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["agent_name"] == "Cov Agent"

    async def test_personal_details_roundtrip(self, client):
        resp = await client.post("/api/personal-details",
                                 json={"first_name": "Ada", "last_name": "Lovelace"})
        assert resp.status_code == 200
        resp = await client.get("/api/personal-details")
        assert resp.json()["first_name"] == "Ada"

    async def test_config_shape(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)


class TestReposRoutes:
    async def test_repo_create_list_delete(self, client):
        body = {"id": "repo_cov", "name": "Cov Repo", "path": "/tmp/cov-repo"}
        assert (await client.post("/api/repos", json=body)).status_code == 200
        assert "repo_cov" in [r["id"] for r in (await client.get("/api/repos")).json()]
        assert (await client.delete("/api/repos/repo_cov")).status_code == 200


class TestDbConfigRoutes:
    async def test_db_config_create_list_delete(self, client):
        body = {"id": "db_cov", "name": "Cov DB", "db_type": "sqlite",
                "connection_string": "sqlite:///tmp/cov.db"}
        r = await client.post("/api/db-configs", json=body)
        assert r.status_code == 200
        listed = await client.get("/api/db-configs")
        assert "db_cov" in [c["id"] for c in listed.json()]
        assert (await client.delete("/api/db-configs/db_cov")).status_code == 200


class TestCustomToolRoutes:
    async def test_custom_tool_create_list_delete(self, client):
        tool = {"name": "cov_tool", "description": "A test tool",
                "parameters": {"type": "object", "properties": {}}}
        assert (await client.post("/api/tools/custom", json=tool)).status_code == 200
        listed = await client.get("/api/tools/custom")
        assert "cov_tool" in [t["name"] for t in listed.json()]
        assert (await client.delete("/api/tools/custom/cov_tool")).status_code == 200

    async def test_available_tools_listing(self, client):
        # No cache priming: /api/tools/available calls session.list_tools()
        # directly and never reaches aggregate_all_tools, so the line that used
        # to sit here was load-bearing only by accident.
        resp = await client.get("/api/tools/available")
        assert resp.status_code < 500


class TestUsageRoutes:
    async def test_pricing_update_and_read(self, client):
        # PUT pricing then read it back.
        resp = await client.put("/api/usage/pricing",
                                json={"stub-model": {"provider": "anthropic",
                                                     "input_per_1m": 1.0, "output_per_1m": 2.0}})
        assert resp.status_code in (200, 204)
        assert (await client.get("/api/usage/pricing")).status_code == 200

    async def test_clear_usage_logs(self, client):
        assert (await client.delete("/api/usage/logs")).status_code in (200, 204)


class TestHistoryRoutes:
    @pytest.mark.parametrize("path", ["/api/history/recent", "/api/history/all"])
    async def test_clear_history(self, client, path):
        assert (await client.delete(path)).status_code < 500


class TestSessionRoutes:
    async def test_unknown_session_history(self, client):
        resp = await client.get("/api/sessions/ghost-session/history")
        assert resp.status_code < 500  # 200 empty or 404

    async def test_delete_session(self, client):
        assert (await client.delete("/api/sessions/ghost-session")).status_code < 500


PETSTORE = {
    "openapi": "3.0.0",
    "info": {"title": "Pet Store", "description": "Pets, mostly."},
    "servers": [{"url": "https://pets.example.test"}],
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets"},
            "post": {"operationId": "addPet", "summary": "Add a pet",
                     "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}}}},
        }
    },
}


class TestToolGroups:
    async def test_groups_crud_and_delete_ungroups_the_tools(self, client):
        created = await client.post("/api/tools/groups", json={"name": "Billing API", "description": "Invoices"})
        assert created.status_code == 200, created.text
        group = created.json()
        assert group["id"] == "grp_billing_api"

        other = await client.post("/api/tools/groups", json={"name": "Other"})
        assert other.status_code == 200
        listed = (await client.get("/api/tools/groups")).json()
        assert {g["id"] for g in listed} >= {"grp_billing_api", "grp_other"}, "a second group did not replace the first"

        renamed = await client.put(f"/api/tools/groups/{group['id']}", json={"name": "Billing", "description": ""})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Billing"

        tool = {"name": "grp_tool", "description": "x", "tool_type": "http", "url": "https://x.test/", "method": "GET",
                "inputSchema": {"type": "object"}, "group": group["id"]}
        assert (await client.post("/api/tools/custom", json=tool)).status_code == 200
        gone = await client.delete(f"/api/tools/groups/{group['id']}")
        assert gone.status_code == 200 and gone.json()["ungrouped"] == 1
        remaining = (await client.get("/api/tools/custom")).json()
        assert next(t for t in remaining if t["name"] == "grp_tool").get("group") is None
        assert (await client.put("/api/tools/groups/grp_nope", json={"name": "n"})).status_code == 404

    async def test_an_openapi_import_is_one_group(self, client):
        preview = await client.post("/api/tools/import/openapi", json={"spec": json.dumps(PETSTORE)})
        assert preview.status_code == 200, preview.text
        assert preview.json()["group"] == {"id": "grp_pet_store", "name": "Pet Store", "description": "Pets, mostly."}
        assert all(t["group"] == "grp_pet_store" for t in preview.json()["tools"])
        assert (await client.get("/api/tools/groups")).json() == [] or all(
            g["id"] != "grp_pet_store" for g in (await client.get("/api/tools/groups")).json()
        ), "a preview writes nothing"

        saved = await client.post("/api/tools/import/openapi", json={"spec": json.dumps(PETSTORE), "save": True, "group": "Pets v2"})
        assert saved.status_code == 200 and saved.json()["group"]["id"] == "grp_pets_v2"
        assert any(g["id"] == "grp_pets_v2" for g in (await client.get("/api/tools/groups")).json())
        tools = {t["name"]: t for t in (await client.get("/api/tools/custom")).json()}
        assert tools["listpets"]["group"] == "grp_pets_v2" and tools["addpet"]["group"] == "grp_pets_v2"


class TestGlobalVariables:
    async def test_roundtrip_and_key_rules(self, client):
        assert (await client.get("/api/settings/variables")).json() == {"values": {}}
        r = await client.post("/api/settings/variables", json={"values": {"api_base": "https://api.acme.test", "team": "ops"}})
        assert r.status_code == 200, r.text
        assert (await client.get("/api/settings/variables")).json()["values"]["api_base"] == "https://api.acme.test"
        r = await client.post("/api/settings/variables", json={"values": {"1bad": "x"}})
        assert r.status_code == 400
        r = await client.post("/api/settings/variables", json={"values": {"has space": "x"}})
        assert r.status_code == 400
        r = await client.post("/api/settings/variables", json={"nope": 1})
        assert r.status_code == 400
