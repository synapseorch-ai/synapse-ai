"""
Tests for the OpenAPI/Swagger tool importer (core.openapi_import), the
configurable-timeout helpers (core.config), and the import API endpoints.
"""
import pytest

from core.openapi_import import parse_openapi_spec


# ── OpenAPI 3.x ──────────────────────────────────────────────────────────────

OAS3 = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com/v1/"}],
    "components": {"schemas": {"Pet": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "tag": {"type": "string"}},
        "required": ["name"],
    }}},
    "paths": {
        "/pets/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {
                "operationId": "getPetById",
                "summary": "Get a pet",
                "parameters": [
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                ],
            },
        },
        "/pets": {
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Pet"}
                }}},
            },
        },
    },
}


class TestOpenAPI3:
    def test_one_tool_per_operation(self):
        tools = parse_openapi_spec(OAS3)
        assert len(tools) == 2
        assert {t["name"] for t in tools} == {"getpetbyid", "createpet"}

    def test_path_param_stays_in_url_and_required(self):
        get = next(t for t in tools_by_method(OAS3, "GET"))
        assert get["url"] == "https://api.example.com/v1/pets/{petId}"
        assert "petId" in get["inputSchema"]["properties"]
        assert "petId" in get["inputSchema"]["required"]

    def test_query_param_included_header_skipped(self):
        get = next(t for t in tools_by_method(OAS3, "GET"))
        props = get["inputSchema"]["properties"]
        assert "verbose" in props        # query param surfaced
        assert "X-Trace" not in props    # header param intentionally skipped

    def test_requestbody_ref_resolved(self):
        post = next(t for t in tools_by_method(OAS3, "POST"))
        props = post["inputSchema"]["properties"]
        assert "name" in props and "tag" in props
        assert post["inputSchema"]["required"] == ["name"]
        assert post["method"] == "POST"

    def test_headers_and_prefix_applied(self):
        tools = parse_openapi_spec(OAS3, headers={"Authorization": "Bearer X"}, name_prefix="petstore")
        assert all(t["headers"] == {"Authorization": "Bearer X"} for t in tools)
        assert all(t["name"].startswith("petstore_") for t in tools)

    def test_base_url_override_wins(self):
        tools = parse_openapi_spec(OAS3, base_url="http://localhost:9000")
        assert all(t["url"].startswith("http://localhost:9000/") for t in tools)


# ── Swagger 2.0 ──────────────────────────────────────────────────────────────

SW2 = {
    "swagger": "2.0",
    "host": "petstore.swagger.io",
    "basePath": "/v2",
    "schemes": ["https"],
    "definitions": {"Pet": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "status": {"type": "string"}},
        "required": ["name"],
    }},
    "paths": {
        "/pet/{petId}": {"get": {
            "operationId": "getPetById", "summary": "Find pet by ID",
            "parameters": [{"name": "petId", "in": "path", "required": True, "type": "integer"}],
        }},
        "/pet": {"post": {
            "operationId": "addPet", "summary": "Add a new pet",
            "parameters": [{"name": "body", "in": "body", "required": True,
                            "schema": {"$ref": "#/definitions/Pet"}}],
        }},
    },
}


class TestSwagger2:
    def test_base_url_from_host_scheme_basepath(self):
        get = next(t for t in tools_by_method(SW2, "GET"))
        assert get["url"] == "https://petstore.swagger.io/v2/pet/{petId}"
        assert get["inputSchema"]["required"] == ["petId"]

    def test_body_definition_ref_resolved(self):
        post = next(t for t in tools_by_method(SW2, "POST"))
        props = post["inputSchema"]["properties"]
        assert "name" in props and "status" in props
        assert post["inputSchema"]["required"] == ["name"]


# ── Edge cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_missing_paths_raises(self):
        with pytest.raises(ValueError):
            parse_openapi_spec({"openapi": "3.0.0"})

    def test_no_operations_raises(self):
        with pytest.raises(ValueError):
            parse_openapi_spec({"openapi": "3.0.0", "paths": {"/x": {}}})

    def test_name_dedup_when_no_operation_id(self):
        spec = {"openapi": "3.0.0", "servers": [{"url": "https://x"}], "paths": {
            "/a": {"get": {"summary": "a"}},
            "/b": {"get": {"summary": "b"}},
        }}
        tools = parse_openapi_spec(spec)
        assert len({t["name"] for t in tools}) == 2  # generated names are unique

    def test_cyclic_ref_does_not_hang(self):
        spec = {"openapi": "3.0.0", "servers": [{"url": "https://x"}],
                "components": {"schemas": {"Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                }}},
                "paths": {"/n": {"post": {"operationId": "makeNode", "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}
                }}}}}
        tools = parse_openapi_spec(spec)
        assert tools[0]["name"] == "makenode"


def tools_by_method(spec, method):
    return [t for t in parse_openapi_spec(spec) if t["method"] == method]


# ── Configurable timeouts ────────────────────────────────────────────────────

class TestTimeoutConfig:
    def test_defaults_match_previous_hardcoded_values(self):
        from core import config
        assert config.MCP_SESSION_READ_TIMEOUT == 60.0
        assert config.MCP_TOOL_CALL_TIMEOUT == 60.0
        assert config.MCP_LIST_TOOLS_TIMEOUT == 15.0
        assert config.LLM_REQUEST_TIMEOUT == 180.0
        assert config.HTTP_TOOL_TIMEOUT == 30.0
        assert config.ORCH_STEP_TIMEOUT == 300.0
        assert config.ORCH_GLOBAL_TIMEOUT_MIN == 30
        assert config.ORCH_HUMAN_TIMEOUT == 3600.0

    def test_env_float_reads_override(self, monkeypatch):
        from core import config
        monkeypatch.setenv("SYNAPSE_TEST_TIMEOUT", "5")
        assert config._env_float("SYNAPSE_TEST_TIMEOUT", 60.0) == 5.0

    def test_env_int_reads_override(self, monkeypatch):
        from core import config
        monkeypatch.setenv("SYNAPSE_TEST_MIN", "12")
        assert config._env_int("SYNAPSE_TEST_MIN", 30) == 12

    def test_unset_falls_back_to_default(self):
        from core import config
        assert config._env_float("SYNAPSE_DEFINITELY_UNSET", 42.0) == 42.0
        assert config._env_int("SYNAPSE_DEFINITELY_UNSET", 7) == 7

    def test_bad_value_falls_back(self, monkeypatch):
        from core import config
        monkeypatch.setenv("SYNAPSE_TEST_BAD", "not-a-number")
        assert config._env_float("SYNAPSE_TEST_BAD", 60.0) == 60.0
        assert config._env_int("SYNAPSE_TEST_BAD", 30) == 30


# ── Import API endpoints ─────────────────────────────────────────────────────

class TestImportEndpoints:
    async def test_preview_does_not_save(self, client):
        resp = await client.post("/api/tools/import/openapi", json={"spec": _json(OAS3)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2 and body["saved"] is False
        # Nothing persisted on a preview.
        assert (await client.get("/api/tools/custom")).json() == []

    async def test_save_persists_tools(self, client):
        resp = await client.post("/api/tools/import/openapi",
                                 json={"spec": _json(OAS3), "save": True})
        assert resp.status_code == 200 and resp.json()["saved"] is True
        names = {t["name"] for t in (await client.get("/api/tools/custom")).json()}
        assert {"getpetbyid", "createpet"} <= names

    async def test_yaml_spec_supported(self, client):
        yaml_spec = (
            "openapi: 3.0.0\n"
            "servers:\n  - url: https://api.example.com\n"
            "paths:\n"
            "  /ping:\n"
            "    get:\n"
            "      operationId: ping\n"
            "      summary: Ping\n"
        )
        resp = await client.post("/api/tools/import/openapi", json={"spec": yaml_spec})
        assert resp.status_code == 200
        assert resp.json()["tools"][0]["url"] == "https://api.example.com/ping"

    async def test_bad_spec_returns_400(self, client):
        resp = await client.post("/api/tools/import/openapi", json={"spec": "{ not valid"})
        assert resp.status_code == 400

    async def test_bulk_upsert(self, client):
        payload = {"tools": [
            {"name": "bulk_a", "tool_type": "http", "url": "https://x/a", "method": "GET"},
            {"name": "bulk_b", "tool_type": "http", "url": "https://x/b", "method": "GET"},
        ]}
        resp = await client.post("/api/tools/custom/bulk", json=payload)
        assert resp.status_code == 200 and resp.json()["imported"] == 2
        names = {t["name"] for t in (await client.get("/api/tools/custom")).json()}
        assert {"bulk_a", "bulk_b"} <= names

    async def test_bulk_rejects_empty(self, client):
        assert (await client.post("/api/tools/custom/bulk", json={"tools": []})).status_code == 400


def _json(obj):
    import json
    return json.dumps(obj)
