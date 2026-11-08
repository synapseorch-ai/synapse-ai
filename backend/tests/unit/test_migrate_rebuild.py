"""
The one table core/store/migrate.py rebuilds.

`create_all` never touches an existing table, and neither dialect can repoint a
primary key in place, so a table that shipped with the wrong key can only be
fixed by copying it. This is the riskiest statement sequence in the store — it
is the only one that can lose a user's data — so it is pinned here: rows
survive, the new key is in force afterwards, the indexes come back, and a
second run is a no-op.
"""
import json

from sqlalchemy import Column, DateTime, Index, MetaData, String, Table, inspect, text

from core.store.engine import build_engine, build_session_factory, init_db
from core.store.migrate import run_migrations
from core.store.models import JSONType


def _old_chat_sessions(metadata: MetaData) -> Table:
    """`chat_sessions` as it shipped: keyed on session_id alone."""
    return Table(
        "chat_sessions", metadata,
        Column("session_id", String(255), primary_key=True),
        Column("agent_id", String(255)),
        Column("tenant_id", String(255), nullable=False, server_default="default"),
        Column("status", String(50)),
        Column("messages", JSONType, nullable=False),
        Column("last_message_at", DateTime(timezone=True)),
        Column("worker_id", String(255)),
        Column("job_id", String(255)),
        Column("created_at", DateTime(timezone=True)),
        Index("idx_chat_tenant_time", "tenant_id", "last_message_at"),
    )


async def _install_old_table(tmp_path):
    """An engine whose chat_sessions predates the key change, with a row in it."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    metadata = MetaData()
    table = _old_chat_sessions(metadata)

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            table.insert().values(
                session_id="s1", agent_id="agent_a", tenant_id="default",
                status="completed", messages=[{"role": "user", "content": "hello"}],
                worker_id="w1",
            )
        )
    return engine


async def test_rows_survive_the_rebuild(tmp_path):
    engine = await _install_old_table(tmp_path)

    await init_db(engine)   # create_all, then run_migrations

    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT * FROM chat_sessions"))).mappings().all()
    await engine.dispose()

    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["agent_id"] == "agent_a"
    assert rows[0]["worker_id"] == "w1"
    # Read through raw SQL, so this is the stored JSON rather than a decoded list.
    assert json.loads(rows[0]["messages"]) == [{"role": "user", "content": "hello"}]
    # A column the old table did not have arrives with its default, not an error.
    assert "cli_session_ids" in rows[0]


async def test_the_new_key_is_in_force_afterwards(tmp_path):
    engine = await _install_old_table(tmp_path)
    await init_db(engine)

    async with engine.begin() as conn:
        pk = await conn.run_sync(
            lambda c: inspect(c).get_pk_constraint("chat_sessions")["constrained_columns"]
        )
        indexes = await conn.run_sync(
            lambda c: {i["name"] for i in inspect(c).get_indexes("chat_sessions")}
        )
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    await engine.dispose()

    assert pk == ["tenant_id", "session_id", "agent_id"]
    assert "idx_chat_tenant_time" in indexes          # the index came back
    assert "chat_sessions__rebuild" not in tables     # the staging copy did not linger


async def test_two_agents_can_share_a_session_id_after_the_rebuild(tmp_path):
    """The reason the rebuild exists.

    The UI keeps one session id per browser and switches agents under it. On
    the old key the second agent's turn overwrote the first agent's history.
    """
    engine = await _install_old_table(tmp_path)
    await init_db(engine)

    from core.store.models import ChatSessionDB

    factory = build_session_factory(engine)
    async with factory() as s:
        s.add(ChatSessionDB(
            session_id="s1", agent_id="agent_b", tenant_id="default",
            messages=[{"role": "user", "content": "different conversation"}],
        ))
        await s.commit()

    async with engine.begin() as conn:
        count = (await conn.execute(
            text("SELECT COUNT(*) FROM chat_sessions WHERE session_id = 's1'")
        )).scalar()
    await engine.dispose()

    assert count == 2


async def test_running_twice_changes_nothing(tmp_path):
    engine = await _install_old_table(tmp_path)

    first = await run_migrations(engine)
    second = await run_migrations(engine)

    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT COUNT(*) FROM chat_sessions"))).scalar()
    await engine.dispose()

    assert any("chat_sessions" in entry for entry in first)
    assert second == []
    assert rows == 1


async def test_a_current_database_is_left_alone(tmp_path):
    """The guard is the primary key itself, so a fresh install never rebuilds."""
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await init_db(engine)

    assert await run_migrations(engine) == []
    await engine.dispose()
