"""
Additive schema migrations, run after ``create_all``.

``Base.metadata.create_all`` creates tables that do not exist and then stops.
It never inspects an existing table, so a column added to a model after someone
has already booted the build is invisible to them for good: no error, no
migration, just a query selecting a column the database does not have.

There is no Alembic in this repo, and adding one for a handful of columns would
put a migrations directory, a version table and an offline/online split in the
way of a single-file SQLite install. Instead this module runs idempotent
``ADD COLUMN`` statements at startup.

The scope is deliberately narrow. ``ALTER TABLE ... ADD COLUMN`` is the one
form SQLite and Postgres spell identically and both apply cheaply. Any change
that renames, drops, retypes or backfills needs a real migration tool — if this
file ever wants one of those, that is the signal to bring in Alembic rather
than to widen this.

The one exception is ``_REBUILDS``, below: a table whose *primary key* has
changed cannot be reached by ADD COLUMN at all, and neither dialect can repoint
one in place. It is implemented as the copy-and-rename every such migration
reduces to, and it is guarded so tightly that it runs once and then never
matches again. Two entries here would be the signal for Alembic.
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

#: (table, column, DDL type, backfill-from) — appended to, never edited. The
#: type is written in the portable spelling both dialects accept; `TIMESTAMP` is
#: what `DateTime(timezone=True)` renders to on SQLite and Postgres accepts it.
#:
#: `backfill_from` names an existing column to seed the new one from, for
#: columns that are sorted on. Added-but-empty would otherwise leave every
#: pre-existing row NULL, and the two dialects disagree about where NULLs sort
#: (SQLite first, Postgres last), so the ordering bug would simply change shape
#: instead of being fixed.
_ADDITIONS: tuple[tuple[str, str, str, str | None], ...] = (
    # Stable list ordering. `agents[0]` is the documented fallback for "the
    # user's default agent" in two places, and rows come back in whatever order
    # the database likes — so without a creation timestamp to sort on, a user's
    # default agent changes between restarts.
    ("agents", "created_at", "TIMESTAMP", "updated_at"),
    ("tools", "created_at", "TIMESTAMP", "updated_at"),
    ("mcp_servers", "created_at", "TIMESTAMP", "updated_at"),
    # The run's task list (core/task_list.py). `JSON` is the spelling both
    # dialects accept; new installs get the model's JSONB variant on Postgres
    # via create_all, and readers treat the migration's NULLs as [].
    ("orchestration_runs", "tasks", "JSON", None),
)


#: (table, the primary key it must end up with) — tables whose key changed
#: after they shipped. See the module docstring: this is the exception, and it
#: is meant to stay a list of one.
_REBUILDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # chat_sessions was keyed on session_id alone, which is wrong: the UI keeps
    # one session id per browser and switches agents under it, so that key
    # merges two agents' conversations into one row and then feeds each agent
    # the other's history.
    ("chat_sessions", ("tenant_id", "session_id", "agent_id")),
)


async def run_migrations(engine: AsyncEngine) -> list[str]:
    """Apply any missing changes. Returns what was applied."""
    applied: list[str] = []
    added_columns: list[str] = []

    async with engine.begin() as conn:
        existing = await conn.run_sync(_existing_columns)

        for table, wanted_key in _REBUILDS:
            if table not in existing:
                continue   # create_all just made it with the current key
            current = await conn.run_sync(_primary_key, table)
            if current == list(wanted_key):
                continue
            await _rebuild(conn, table, existing[table])
            applied.append(f"{table} (rebuilt on a new primary key)")
            print(
                f"[store] rebuilt {table} on its new primary key "
                f"({', '.join(wanted_key)}); rows carried across",
                flush=True,
            )
            # The rebuilt table comes from the current model, so it already has
            # every column the additions below would add.
            existing = await conn.run_sync(_existing_columns)

        for table, column, ddl_type, backfill_from in _ADDITIONS:
            if table not in existing:
                # create_all just made it from the current model, so the column
                # is already there.
                continue
            if column in existing[table]:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            if backfill_from and backfill_from in existing[table]:
                await conn.execute(
                    text(
                        f"UPDATE {table} SET {column} = {backfill_from} "
                        f"WHERE {column} IS NULL"
                    )
                )
            applied.append(f"{table}.{column}")
            added_columns.append(f"{table}.{column}")

    if added_columns:
        print(f"[store] added missing columns: {', '.join(added_columns)}", flush=True)
    return applied


def _existing_columns(sync_conn) -> dict[str, set[str]]:
    inspector = inspect(sync_conn)
    return {
        name: {c["name"] for c in inspector.get_columns(name)}
        for name in inspector.get_table_names()
    }


def _primary_key(sync_conn, table: str) -> list[str]:
    return list(inspect(sync_conn).get_pk_constraint(table).get("constrained_columns") or [])


async def _rebuild(conn, table: str, old_columns: set[str]) -> None:
    """Recreate `table` with the key the model declares, carrying rows across.

    Every migration that repoints a primary key reduces to this, because
    neither dialect can do it in place. Written out rather than hidden behind a
    tool so that what it does to a user's data is readable: create the new
    shape beside the old one, copy the columns they share, drop, rename, then
    put the indexes back.

    Two details make it work on both dialects. The staging table is created
    without its indexes, because index names are database-wide and the
    originals are still attached to the table being replaced. And the model
    leaves this table's primary key unnamed, so Postgres derives the constraint
    name from the staging table rather than colliding with the live one.

    Runs inside the caller's transaction, so a failure anywhere leaves the
    original untouched rather than half-migrated.
    """
    from sqlalchemy import MetaData

    from core.store.models import Base

    model_table = Base.metadata.tables[table]
    staging_name = f"{table}__rebuild"

    # A throwaway MetaData, so the staging copy never enters the real one.
    staging = model_table.to_metadata(MetaData(), name=staging_name)
    staging.indexes.clear()

    # Only the columns both shapes have. One the old table lacks takes its
    # default; one it has and the model does not is being dropped on purpose.
    shared = [c.name for c in model_table.columns if c.name in old_columns]
    columns = ", ".join(shared)

    await conn.run_sync(staging.create)
    await conn.execute(text(f"INSERT INTO {staging_name} ({columns}) SELECT {columns} FROM {table}"))
    await conn.execute(text(f"DROP TABLE {table}"))
    await conn.execute(text(f"ALTER TABLE {staging_name} RENAME TO {table}"))
    for index in model_table.indexes:
        await conn.run_sync(index.create)
