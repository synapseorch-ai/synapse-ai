"""The engine store on SQLite and, when `SYNAPSE_TEST_POSTGRES_URL` is set, on Postgres too.

Import `knowledge_store` into a test module and every test that asks for it runs
once per dialect against the process-wide store (`core.store.session()`), the
same one routes and jobs use. The Postgres database is a throwaway: the
Knowledge tables, `usage_logs`, the settings and `collections` are emptied
before each test. Point the URL at your own container, never a shared one.
"""
from __future__ import annotations

import os

import pytest

PG_URL = os.environ.get("SYNAPSE_TEST_POSTGRES_URL", "")
DIALECTS = ["sqlite"] + (["postgres"] if PG_URL else [])

_EMPTIED = ("knowledge_eval_runs", "knowledge_chunks", "knowledge_costs", "knowledge_queries", "knowledge_snapshots",
            "knowledge_documents", "usage_logs", "scale_settings")


class _Provider:
    """A resource provider: registering one is what unlocks `tenant_scope`."""

    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    from core.scale.context import set_resource_provider

    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


@pytest.fixture(params=DIALECTS)
async def knowledge_store(request, monkeypatch):
    """The dialect's name. SQLite is the per-test file the autouse fixture set up."""
    if request.param == "sqlite":
        yield "sqlite"
        return
    from sqlalchemy import text

    import core.store.engine as store_mod
    from core.store import session
    from core.store.engine import normalise_db_url

    monkeypatch.setenv("SYNAPSE_DB_URL", normalise_db_url(PG_URL))
    monkeypatch.setattr(store_mod, "_engine", None)
    monkeypatch.setattr(store_mod, "_session_factory", None)
    async with session() as s:
        for table in _EMPTIED:
            await s.execute(text(f"DELETE FROM {table}"))
        await s.execute(text("DELETE FROM collections"))
    yield "postgres"
    await store_mod.reset_store()
