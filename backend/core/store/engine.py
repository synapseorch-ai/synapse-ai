"""
The engine's database connection: one URL, two dialects, one code path.

A plain install gets SQLite in the install directory and needs nothing running.
A scale deployment or an embedder points ``SYNAPSE_DB_URL`` at Postgres. There
is no ``if sqlite:`` branch anywhere above this module — the difference is a
connection string.

Why SQLite is the default rather than "no database"
---------------------------------------------------
The engine previously kept its state in JSON files under a ``DATA_DIR``. That
made the product a thing you install on one machine, and it made every step
towards running more than one of anything a workaround. SQLite keeps the
zero-setup install property that mattered about the JSON files while giving up
none of the structure.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, StaticPool

from core.store.models import Base

#: A binder receives each freshly-opened session and may issue statements on it.
#: Anything it runs lands inside that session's transaction, which is what makes
#: ``SET LOCAL`` the natural thing to say here.
SessionBinder = Callable[[AsyncSession], Awaitable[None]]

#: A default SQLite install puts its database in `core.paths.var_root()`:
#: `backend/var` in a source checkout, `$SYNAPSE_HOME/var` in a packaged one.
#: Overridden wholesale by SYNAPSE_DB_URL; there is no separate "which directory"
#: setting, because two knobs that both decide the same thing is how you get a
#: support thread.

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

#: Called on every session this module opens, before the caller sees it.
#:
#: The engine's own tenancy is the ``tenant_id`` column, and that is complete
#: for a plain install. A deployment may enforce a *second*, independent gate on
#: the same rows — Postgres row-level security is the one that prompted this —
#: and such a gate has to be satisfied on the connection rather than in a WHERE
#: clause. Nothing above this module can do that, because ``collections.load()``
#: and its neighbours open their own sessions; by the time a caller holds a
#: result the opportunity has passed.
#:
#: So this is a seam of the same kind as ``collections.set_document_resolver``:
#: the engine states where the deployment gets to speak, and knows nothing about
#: what it says. ``None`` is the ordinary case and costs a single ``is None``.
_session_binder: SessionBinder | None = None


def normalise_db_url(url: str) -> str:
    """A plain Postgres URL in the asyncpg dialect the engine drives; any other unchanged.

    `postgresql://` is what every provider hands out and what a Kubernetes
    secret usually holds; the async engine cannot open it (F59).
    """
    for scheme in ("postgresql://", "postgres://"):
        if url.startswith(scheme):
            return "postgresql+asyncpg://" + url[len(scheme):]
    return url


def default_url() -> str:
    """The database URL, from the environment or the local SQLite default."""
    configured = normalise_db_url((os.getenv("SYNAPSE_DB_URL") or "").strip())
    if configured:
        return configured
    from core.paths import var_root

    directory = var_root()
    directory.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{directory / 'synapse.db'}"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def is_memory_sqlite(url: str) -> bool:
    return is_sqlite(url) and (":memory:" in url or "mode=memory" in url)


def build_engine(url: str, pgbouncer_mode: bool = False) -> AsyncEngine:
    """Create an async SQLAlchemy engine for either dialect.

    PgBouncer transaction-pooling mode is incompatible with SQLAlchemy's
    connection-level pooling, so NullPool is used when pgbouncer_mode=True.
    """
    kwargs: dict = {"echo": False, "future": True}

    if is_memory_sqlite(url):
        # An in-memory database lives inside its connection. With any pool that
        # opens a second one, `init_db` creates the tables in one database and
        # every query then runs against a different, empty one — which surfaces
        # as "no such table" long after the misconfiguration. StaticPool holds
        # exactly one connection, so the database persists for the engine's
        # lifetime. `check_same_thread` is required because that one connection
        # is then reached from the greenlet worker thread.
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    elif is_sqlite(url):
        # SQLite has one writer at a time. A pool of connections does not buy
        # concurrency it cannot deliver, and it does buy "database is locked"
        # under a UI plus a worker. One connection, and a timeout long enough
        # that a checkpoint waits for a concurrent write instead of failing.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"timeout": 30}
    elif pgbouncer_mode:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["poolclass"] = AsyncAdaptedQueuePool
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(url, **kwargs)

    if is_sqlite(url):
        _enable_wal(engine)

    # Its statements are traced when telemetry is on (the operation, never the
    # text); a no-op otherwise.
    from core import telemetry
    telemetry.instrument_engine(engine)
    return engine


def _enable_wal(engine: AsyncEngine) -> None:
    """Put SQLite in WAL mode so a reader never blocks behind a writer.

    Without this, listing runs in the UI while a run checkpoints is a lock
    contention bug that looks like a hang. WAL is per-database and persists,
    but setting it on every connect is idempotent and survives a file that was
    created by an older build.
    """
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create any missing tables, then apply additive column changes.

    `create_all` only ever creates whole tables — it will not add a column to
    one that already exists, so an install that booted an earlier build needs
    `run_migrations` to catch up. See `core/store/migrate.py`.
    """
    from core.store.migrate import run_migrations

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_migrations(engine)


async def get_store() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first use.

    Deliberately lazy: importing this module must not open a database, or every
    unit test and every `--help` invocation pays for a connection.
    """
    global _engine, _session_factory
    if _session_factory is None:
        _engine = build_engine(default_url())
        await init_db(_engine)
        _session_factory = build_session_factory(_engine)
    return _session_factory


def set_store(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Install an externally-built session factory.

    Used by the scale worker (which builds its engine from scale config) and by
    embedders that already own a connection pool.
    """
    global _session_factory
    _session_factory = session_factory


async def reset_store() -> None:
    """Drop the cached engine. For tests, and for a config reload."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def set_session_binder(binder: SessionBinder | None) -> None:
    """Install the callable run on every session this module opens.

    Installed once at startup, beside the other deployment seams. Passing
    ``None`` removes it, which is what tests do rather than reaching for the
    module global.
    """
    global _session_binder
    _session_binder = binder


@asynccontextmanager
async def session() -> AsyncGenerator[AsyncSession, None]:
    """A transaction against the store, committed on clean exit."""
    factory = await get_store()
    async with factory() as s:
        await _bind(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker,
) -> AsyncGenerator[AsyncSession, None]:
    """Transaction against an explicitly-passed factory.

    Kept for callers that hold their own factory (the scale worker's ARQ ctx).
    """
    async with session_factory() as s:
        await _bind(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def _bind(s: AsyncSession) -> None:
    """Run the installed binder, if there is one.

    **Before the caller's first statement, and inside the same transaction.**
    A binder that says ``SET LOCAL`` is stating something about this
    transaction, so running it afterwards — or in a transaction of its own —
    would be a no-op that looks like it worked.

    Failures propagate. A binder exists to satisfy an access rule, and a
    swallowed failure here would turn "this deployment's isolation is not in
    force" into "that table appears to be empty", which is the harder bug of the
    two by a wide margin.
    """
    if _session_binder is not None:
        await _session_binder(s)
