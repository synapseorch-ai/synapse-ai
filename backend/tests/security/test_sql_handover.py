"""The SQL tool's server is handed the one connection a call may use (F1118).

On a multi-tenant worker the SQL tool's subprocess no longer opens the store. The worker,
which already reads the tenant's database configs in-process, resolves the one a call names
and sends it with the call, in MCP's request `_meta`. The subprocess then needs no store URL
and is spawned with the same environment allowlist as the shared servers.
"""

from __future__ import annotations

import pathlib

import pytest

from core import tool_scope
from core.tenancy import tenant_scope

CONFIGS = [
    {"id": "db_1", "name": "Reporting", "db_type": "postgres",
     "connection_string": "postgresql://reporting:not-a-real-password@db.example.com/analytics"},
    {"id": "db_2", "name": "Staging", "db_type": "postgres",
     "connection_string": "postgresql://staging:not-a-real-password@staging.example.com/app"},
]


class _Provider:
    async def resolve_agents(self):
        return []

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def tenant_configs(monkeypatch):
    from core.scale.context import set_resource_provider
    from core.store import collections

    seen: list[str] = []

    async def load(name):
        seen.append(name)
        return [dict(c) for c in CONFIGS] if name == "db_configs" else []

    monkeypatch.setattr(collections, "load", load)
    monkeypatch.setattr(tool_scope, "_load_settings", lambda: {"allow_db_write": False})
    set_resource_provider(_Provider())
    yield seen
    set_resource_provider(None)


class TestTheWorkerHandsOverOneConnection:
    @pytest.mark.anyio
    async def test_a_call_naming_a_database_carries_that_one(self, tenant_configs):
        with tenant_scope("org-a"):
            kwargs = await tool_scope.call_kwargs_for("sql", "run-1", {"db_id": "db_2"})

        handed = kwargs["meta"][tool_scope.DB_KEY]
        assert handed == {
            "id": "db_2",
            "connection_string": CONFIGS[1]["connection_string"],
            "allow_write": False,
        }
        assert CONFIGS[0]["connection_string"] not in repr(kwargs)

    @pytest.mark.anyio
    async def test_an_unknown_database_hands_over_nothing(self, tenant_configs):
        with tenant_scope("org-a"):
            kwargs = await tool_scope.call_kwargs_for("sql", "run-1", {"db_id": "db_9"})

        assert tool_scope.DB_KEY not in kwargs.get("meta", {})

    @pytest.mark.anyio
    @pytest.mark.parametrize("server", ["web_scraper", "time", "ext_mcp_github"])
    async def test_every_other_server_is_called_as_before(self, tenant_configs, server):
        with tenant_scope("org-a"):
            kwargs = await tool_scope.call_kwargs_for(server, "run-1", {"db_id": "db_1"})
            before = tool_scope.call_kwargs(server, "run-1")

        assert kwargs == before
        assert "db_configs" not in tenant_configs

    def test_both_call_sites_use_it(self):
        backend = pathlib.Path(__file__).resolve().parents[2]
        for path in ("core/react_engine.py", "core/orchestration/steps.py"):
            text = (backend / path).read_text(encoding="utf-8")
            assert "await call_kwargs_for(" in text, path


class TestAConnectionsOwnWriteSettingHolds:
    """F1119: a connection saved as read-only stays read-only whatever the global setting says."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("global_on", "connection", "expected"),
        [
            (True, False, False),   # global on, the connection read-only: refused
            (True, True, True),     # global on, the connection allows: allowed
            (False, True, False),   # global off: refused either way
            (False, False, False),
            (True, None, True),     # no per-connection field (a single-user install): the global decides
        ],
    )
    async def test_the_handed_write_setting_is_both(self, tenant_configs, monkeypatch, global_on, connection, expected):
        from core.store import collections

        config = dict(CONFIGS[0])
        if connection is not None:
            config["allow_write"] = connection

        async def load(name):
            return [config] if name == "db_configs" else []

        monkeypatch.setattr(collections, "load", load)
        monkeypatch.setattr(tool_scope, "_load_settings", lambda: {"allow_db_write": global_on})

        with tenant_scope("org-a"):
            kwargs = await tool_scope.call_kwargs_for("sql", "run-1", {"db_id": "db_1"})

        assert kwargs["meta"][tool_scope.DB_KEY]["allow_write"] is expected


class TestTheSqlServerUsesWhatItIsHanded:
    @pytest.fixture
    def sql_agent(self, monkeypatch):
        from tools import sql_agent

        async def no_store():
            raise AssertionError("the SQL server opened the store")

        opened: list[str] = []

        def connect(conn_str):
            opened.append(conn_str)
            return object(), object()

        monkeypatch.setattr(sql_agent, "_load_db_configs", no_store)
        monkeypatch.setattr(sql_agent, "_connect", connect)
        sql_agent._engines.clear()
        yield sql_agent, opened
        sql_agent._engines.clear()

    @pytest.mark.anyio
    async def test_a_handed_connection_is_the_one_opened(self, sql_agent, monkeypatch):
        module, opened = sql_agent
        monkeypatch.setenv("SYNAPSE_SQL_HANDOVER", "required")
        handed = {"id": "db_2", "connection_string": CONFIGS[1]["connection_string"], "allow_write": False}

        await module.get_db_engine("db_2", handed=handed)

        assert opened == [CONFIGS[1]["connection_string"]]

    @pytest.mark.anyio
    async def test_an_edited_connection_is_not_served_from_the_cache(self, sql_agent, monkeypatch):
        module, opened = sql_agent
        monkeypatch.setenv("SYNAPSE_SQL_HANDOVER", "required")
        first = {"id": "db_2", "connection_string": CONFIGS[1]["connection_string"], "allow_write": False}
        edited = {**first, "connection_string": CONFIGS[0]["connection_string"]}

        await module.get_db_engine("db_2", handed=first)
        await module.get_db_engine("db_2", handed=edited)

        assert opened == [first["connection_string"], edited["connection_string"]]

    @pytest.mark.anyio
    @pytest.mark.parametrize("arguments", [{"db_id": "db_1"}, {}])
    async def test_without_a_handover_a_worker_call_is_refused(self, sql_agent, monkeypatch, arguments):
        module, opened = sql_agent
        monkeypatch.setenv("SYNAPSE_SQL_HANDOVER", "required")

        result = await module._call("list_tables", arguments, handed=None)

        assert result[0].text.startswith("Error:"), result[0].text
        assert opened == []


def test_the_sql_server_is_spawned_with_the_allowlist(monkeypatch):
    from core.scale.worker_server_module import (
        _BACKEND_ROOT,
        _TOOLS_DIR,
        SHARED_TOOL_ENV_NAMES,
        SHARED_TOOL_ENV_PREFIXES,
        _get_native_mcp_servers,
    )

    for name in ("SYNAPSE_DB_URL", "DATABASE_URL", "REDIS_URL", "SYNAPSE_DOCUMENT_RESOLVER"):
        monkeypatch.setenv(name, "not-for-a-tool")

    env = _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="tenant")["sql"].env

    assert env["SYNAPSE_SQL_HANDOVER"] == "required"
    allowed = SHARED_TOOL_ENV_NAMES | {"SYNAPSE_TENANT_ID", "SYNAPSE_SQL_HANDOVER"}
    stray = {k for k in env if k not in allowed and not k.startswith(SHARED_TOOL_ENV_PREFIXES)}
    assert not stray, sorted(stray)
