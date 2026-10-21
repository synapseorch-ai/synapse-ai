"""The boards table's scope check is widened on an install that already has the table (SAI2-P1, F776).

Synapse AI's own board is scope `assistant` (Naveen, 10-21). `create_all` never touches an existing table and SQLite
cannot alter a CHECK, so an install whose `boards` table was made with the old check would refuse every write to
that board. `core/store/migrate.py` rebuilds the table once — the copy-and-rename its one primary-key rebuild uses —
when its scope check lacks a value the model now allows; Postgres drops and re-adds the one constraint. Like the
other rebuild this is the store's riskiest statement sequence, so it is pinned: rows survive, the new value is
accepted, the unique key still holds, and a second run is a no-op.
"""
from sqlalchemy import (CheckConstraint, Column, DateTime, MetaData, PrimaryKeyConstraint, String, Table, Text,
                        UniqueConstraint, inspect, text)

from core.store.engine import build_engine, init_db
from core.store.migrate import run_migrations
from core.store.models import JSONType


def _old_boards(metadata: MetaData) -> Table:
    """`boards` as BOARD1a shipped it: four scopes."""
    return Table(
        "boards", metadata,
        Column("tenant_id", String(255), nullable=False, server_default="default"),
        Column("id", String(255), nullable=False),
        Column("scope", String(16), nullable=False),
        Column("scope_id", String(255), nullable=False, server_default=""),
        Column("name", String(255), nullable=False, server_default=""),
        Column("description", Text, nullable=False, server_default=""),
        Column("created_by", JSONType),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        PrimaryKeyConstraint("tenant_id", "id", name="boards_pkey"),
        UniqueConstraint("tenant_id", "scope", "scope_id", "name", name="uq_boards_tenant_scope_name"),
        CheckConstraint("scope IN ('shared', 'workflow', 'schedule', 'run')", name="ck_boards_scope"),
    )


async def _install_old(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    metadata = MetaData()
    table = _old_boards(metadata)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(table.insert().values(
            tenant_id="default", id="brd_1", scope="shared", scope_id="", name="Bugs", description="ours",
            created_by={"person": "Ana"}, created_at=text("CURRENT_TIMESTAMP"), updated_at=text("CURRENT_TIMESTAMP")))
    return engine


async def _insert_assistant(conn, board_id="brd_ai"):
    await conn.execute(text(
        "INSERT INTO boards (tenant_id, id, scope, scope_id, name, description, created_at, updated_at) "
        f"VALUES ('default', '{board_id}', 'assistant', '', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))


async def test_the_old_check_refuses_the_new_scope(tmp_path):
    """Why the rebuild exists."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    engine = await _install_old(tmp_path)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _insert_assistant(conn)
    await engine.dispose()


async def test_rows_survive_and_the_new_scope_is_accepted(tmp_path):
    engine = await _install_old(tmp_path)
    await init_db(engine)

    async with engine.begin() as conn:
        await _insert_assistant(conn)
        rows = (await conn.execute(text("SELECT id, scope, name, description FROM boards ORDER BY id"))).all()
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    await engine.dispose()

    assert [tuple(r) for r in rows] == [("brd_1", "shared", "Bugs", "ours"), ("brd_ai", "assistant", "", "")]
    assert "boards__rebuild" not in tables


async def test_the_unique_key_and_the_rest_of_the_check_still_hold(tmp_path):
    import pytest
    from sqlalchemy.exc import IntegrityError

    engine = await _install_old(tmp_path)
    await init_db(engine)
    async with engine.begin() as conn:
        await _insert_assistant(conn)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _insert_assistant(conn, "brd_ai_2")  # one Synapse AI board per workspace
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO boards (tenant_id, id, scope, scope_id, name, description, created_at, updated_at) "
                "VALUES ('default', 'brd_x', 'nonsense', '', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    await engine.dispose()


async def test_running_twice_changes_nothing(tmp_path):
    engine = await _install_old(tmp_path)
    first = await run_migrations(engine)
    second = await run_migrations(engine)
    await engine.dispose()
    assert any("boards" in entry and "check" in entry for entry in first)
    assert second == []


async def test_a_current_database_is_left_alone(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await init_db(engine)
    assert await run_migrations(engine) == []
    async with engine.begin() as conn:
        await _insert_assistant(conn)
    await engine.dispose()
