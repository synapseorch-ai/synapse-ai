"""
The engine's store: dialect portability and per-tenant keying.

These are the properties that let one code path serve a SQLite single-tenant
install and a Postgres multi-tenant deployment. Each is cheap to break by
accident and expensive to discover in production:

* a Postgres-only column type makes every SQLite install fail at ``create_all``
  on a user's laptop rather than in CI;
* a single-column primary key on a user-chosen name means the second tenant to
  register ``github`` gets a constraint violation naming a row it cannot see;
* the wrong dialect's ``insert`` emits SQL the other database rejects outright.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from core.store import build_engine, build_session_factory, init_db, upsert
from core.store.models import (
    DEFAULT_TENANT,
    Base,
    MCPServerDB,
    OrchestrationRunDB,
    SettingDB,
)

MEMORY_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def store():
    """A fresh in-memory SQLite store, torn down after the test."""
    engine = build_engine(MEMORY_URL)
    await init_db(engine)
    yield build_session_factory(engine)
    await engine.dispose()


# ── dialect portability ──────────────────────────────────────────────────────

def test_no_postgres_only_types_leak_into_the_ddl():
    """Every table must compile for SQLite as well as Postgres.

    A bare `from sqlalchemy.dialects.postgresql import JSONB` in models.py
    passes every Postgres test and makes the default install unrunnable.
    """
    for table in Base.metadata.tables.values():
        pg = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        lite = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        assert "JSONB" not in lite, f"{table.name} emits JSONB on SQLite"
        # Postgres keeps the richer types it always had.
        if "JSON" in lite:
            assert "JSONB" in pg, f"{table.name} lost JSONB on Postgres"


def test_uuid_is_native_on_postgres_and_portable_elsewhere():
    dlq = Base.metadata.tables["dead_letter_queue"]
    pg = str(CreateTable(dlq).compile(dialect=postgresql.dialect()))
    lite = str(CreateTable(dlq).compile(dialect=sqlite.dialect()))
    assert "id UUID" in pg          # unchanged from the Postgres-only original
    assert "id CHAR(32)" in lite


@pytest.mark.parametrize("table,key", [("mcp_servers", "name"), ("scale_settings", "key")])
def test_user_named_tables_key_on_the_tenant(table, key):
    """The natural key is per-tenant, in both dialects."""
    t = Base.metadata.tables[table]
    assert [c.name for c in t.primary_key.columns] == ["tenant_id", key]


# ── per-tenant keying ────────────────────────────────────────────────────────

async def test_two_tenants_can_register_the_same_mcp_server_name(store):
    """The single-column PK on `name` is what D29 had to remove.

    Keyed globally, the second tenant to add `github` gets a primary-key
    violation referring to a row RLS will not let it see.
    """
    async with store() as s:
        for tenant in ("acme", "globex"):
            await upsert(
                s, MCPServerDB,
                values={"tenant_id": tenant, "name": "github", "label": tenant,
                        "definition": {"url": f"https://{tenant}.test"}},
                index_elements=["tenant_id", "name"],
            )
        await s.commit()

    async with store() as s:
        rows = (await s.execute(select(MCPServerDB))).scalars().all()

    assert sorted((r.tenant_id, r.definition["url"]) for r in rows) == [
        ("acme", "https://acme.test"),
        ("globex", "https://globex.test"),
    ]


async def test_two_tenants_can_hold_the_same_setting_key(store):
    """`key` is a bare name like `model`; globally keyed, one tenant takes it."""
    async with store() as s:
        for tenant in ("acme", "globex"):
            await upsert(
                s, SettingDB,
                values={"tenant_id": tenant, "key": "model", "value": f'"{tenant}-4"'},
                index_elements=["tenant_id", "key"],
            )
        await s.commit()

    async with store() as s:
        rows = (await s.execute(select(SettingDB))).scalars().all()

    assert {r.tenant_id: r.value for r in rows} == {"acme": '"acme-4"', "globex": '"globex-4"'}


async def test_upsert_updates_in_place_rather_than_duplicating(store):
    for label in ("first", "second"):
        async with store() as s:
            await upsert(
                s, MCPServerDB,
                values={"tenant_id": "acme", "name": "github", "label": label,
                        "definition": {"v": label}},
                index_elements=["tenant_id", "name"],
            )
            await s.commit()

    async with store() as s:
        rows = (await s.execute(select(MCPServerDB))).scalars().all()

    assert [(r.label, r.definition["v"]) for r in rows] == [("second", "second")]


async def test_upsert_leaves_columns_outside_the_update_set_alone(store):
    """`update=` names what a conflict overwrites; everything else survives."""
    async with store() as s:
        await upsert(
            s, MCPServerDB,
            values={"tenant_id": "acme", "name": "gh", "label": "original",
                    "definition": {"v": 1}},
            index_elements=["tenant_id", "name"],
        )
        await s.commit()

    async with store() as s:
        await upsert(
            s, MCPServerDB,
            values={"tenant_id": "acme", "name": "gh", "label": "ignored",
                    "definition": {"v": 2}},
            index_elements=["tenant_id", "name"],
            update=["definition"],
        )
        await s.commit()

    async with store() as s:
        row = (await s.execute(select(MCPServerDB))).scalar_one()

    assert (row.label, row.definition["v"]) == ("original", 2)


# ── tenancy defaults and JSON fidelity ───────────────────────────────────────

async def test_tenant_id_defaults_rather_than_violating_not_null(store):
    """A row written by code that predates tenancy lands on the default tenant."""
    async with store() as s:
        s.add(OrchestrationRunDB(run_id="r1", orchestration_id="o1"))
        await s.commit()

    async with store() as s:
        row = (await s.execute(select(OrchestrationRunDB))).scalar_one()

    assert row.tenant_id == DEFAULT_TENANT


async def test_run_state_round_trips_through_json(store):
    """`shared_state` and `step_history` are the run, not a summary of it."""
    state = {"nested": [1, {"unicode": "café", "null": None}], "flag": True}
    history = [{"step_name": "a", "output": {"k": [1, 2]}}]

    async with store() as s:
        s.add(OrchestrationRunDB(
            run_id="r1", orchestration_id="o1", tenant_id="acme",
            shared_state=state, step_history=history,
        ))
        await s.commit()

    async with store() as s:
        row = (await s.execute(select(OrchestrationRunDB))).scalar_one()

    assert row.shared_state == state
    assert row.step_history == history


async def test_in_memory_sqlite_keeps_one_connection():
    """Regression: NullPool + `:memory:` gives every session an empty database.

    `init_db` creates the tables in one connection's database and the first
    query then runs against a different, empty one — surfacing as
    "no such table" far from the misconfiguration that caused it.
    """
    engine = build_engine(MEMORY_URL)
    await init_db(engine)
    factory = build_session_factory(engine)
    try:
        async with factory() as s:
            await s.execute(select(MCPServerDB))   # would raise "no such table"
    finally:
        await engine.dispose()
