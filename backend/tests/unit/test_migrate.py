"""
Additive migrations, because `create_all` will not add a column.

`Base.metadata.create_all` creates whole tables and then stops — it never
inspects one that already exists. So a column added to a model after a build
shipped is invisible to everyone who already booted it: no error at startup,
just a query selecting something the database does not have. There is no
Alembic in this repo, so `core/store/migrate.py` closes that gap for the one
statement type both dialects spell identically.
"""
import pytest
from sqlalchemy import inspect, text

from core.store.engine import build_engine, build_session_factory, init_db
from core.store.migrate import run_migrations


@pytest.mark.asyncio
async def test_adds_a_missing_column_to_an_existing_table(tmp_path):
    """Simulates an install created by a build that predates `created_at`."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    engine = build_engine(url)

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE agents ("
            "  id VARCHAR(255) PRIMARY KEY,"
            "  name VARCHAR(500) NOT NULL,"
            "  definition JSON NOT NULL,"
            "  tenant_id VARCHAR(255) NOT NULL DEFAULT 'default',"
            "  updated_at TIMESTAMP"
            ")"
        ))
        await conn.execute(text(
            "INSERT INTO agents (id, name, definition, tenant_id, updated_at) "
            "VALUES ('a1', 'Old', '{}', 'default', '2026-01-01 00:00:00')"
        ))

    applied = await run_migrations(engine)
    assert "agents.created_at" in applied

    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("agents")}
        )
        assert "created_at" in columns

        # Backfilled, not left NULL: the two dialects disagree about where
        # NULLs sort, so an empty column would only change the bug's shape.
        created = (await conn.execute(text("SELECT created_at FROM agents"))).scalar()
        assert created is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_is_idempotent(tmp_path):
    """Runs on every boot, so a second pass must be a no-op."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}"
    engine = build_engine(url)
    await init_db(engine)

    assert await run_migrations(engine) == []
    assert await run_migrations(engine) == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_migrated_install_can_order_by_the_new_column(tmp_path):
    """The end-to-end point of the column: a stable list on an upgraded install."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'upgraded.db'}"
    engine = build_engine(url)

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE agents ("
            "  id VARCHAR(255) PRIMARY KEY,"
            "  name VARCHAR(500) NOT NULL,"
            "  definition JSON NOT NULL,"
            "  tenant_id VARCHAR(255) NOT NULL DEFAULT 'default',"
            "  updated_at TIMESTAMP"
            ")"
        ))
        for n, when in enumerate(("2026-01-03", "2026-01-01", "2026-01-02")):
            await conn.execute(text(
                "INSERT INTO agents (id, name, definition, tenant_id, updated_at) "
                f"""VALUES ('a{n}', 'A{n}', '{{"id": "a{n}"}}', 'default', '{when} 00:00:00')"""
            ))

    await init_db(engine)

    from core.store import resources
    from core.store.engine import reset_store, set_store

    set_store(build_session_factory(engine))
    try:
        # created_at was backfilled from updated_at, so the oldest row leads —
        # insertion order (a0, a1, a2) is deliberately not the answer here.
        assert [a["id"] for a in await resources.load_agents()] == ["a1", "a2", "a0"]
    finally:
        await reset_store()


@pytest.mark.asyncio
async def test_adds_step_progress_to_an_existing_runs_table(tmp_path):
    """A runs table from before mid-step progress had its own column."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}"
    engine = build_engine(url)

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE orchestration_runs ("
            "  run_id VARCHAR(255) PRIMARY KEY,"
            "  orchestration_id VARCHAR(255) NOT NULL,"
            "  tenant_id VARCHAR(255) NOT NULL DEFAULT 'default',"
            "  status VARCHAR(50) NOT NULL,"
            "  shared_state JSON NOT NULL,"
            "  step_history JSON NOT NULL,"
            "  tasks JSON"
            ")"
        ))
        await conn.execute(text(
            "INSERT INTO orchestration_runs (run_id, orchestration_id, status, shared_state, step_history) "
            "VALUES ('r1', 'o1', 'paused', '{}', '[]')"
        ))

    applied = await run_migrations(engine)
    assert "orchestration_runs.step_progress" in applied

    async with engine.begin() as conn:
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("orchestration_runs")}
        )
        assert "step_progress" in columns

    assert await run_migrations(engine) == []  # idempotent
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_golden_set_run_gets_its_stop_reason(tmp_path):
    """F441: an install whose `eval_suite_runs` predates `stop_reason` gets the
    column, and its old rows read with no reason."""
    from core.store.models import EvalSuiteRunDB
    from core.store.evals import suite_run_to_dict
    from sqlalchemy import select

    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    engine = build_engine(url)

    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE eval_suite_runs ("
            "  id VARCHAR(255) PRIMARY KEY,"
            "  tenant_id VARCHAR(255) NOT NULL DEFAULT 'default',"
            "  suite_id VARCHAR(255) NOT NULL,"
            "  orchestration_id VARCHAR(255) NOT NULL,"
            "  status VARCHAR(50) NOT NULL,"
            "  case_count INTEGER NOT NULL,"
            "  scored_count INTEGER NOT NULL,"
            "  mean_score FLOAT,"
            "  rubric_id VARCHAR(255),"
            "  rubric_version INTEGER,"
            "  started_at TIMESTAMP NOT NULL,"
            "  ended_at TIMESTAMP"
            ")"
        ))
        await conn.execute(text(
            "INSERT INTO eval_suite_runs (id, suite_id, orchestration_id, status, case_count, "
            "scored_count, started_at) VALUES ('esr_old', 'set_1', 'orch_1', 'completed', 2, 2, "
            "'2026-10-01 00:00:00')"
        ))

    applied = await run_migrations(engine)
    assert "eval_suite_runs.stop_reason" in applied

    factory = build_session_factory(engine)
    async with factory() as session:
        row = (await session.execute(select(EvalSuiteRunDB))).scalars().one()
        assert suite_run_to_dict(row)["stop_reason"] == ""

    await engine.dispose()
