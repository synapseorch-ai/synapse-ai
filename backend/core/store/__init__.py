"""
The engine's durable store.

One SQLAlchemy layer over two dialects: SQLite for a plain install, Postgres
for scale deployments and embedders. It replaces the ``DATA_DIR`` JSON files
that used to hold orchestrations, agents, custom tools, MCP servers, settings
and run state.

Those files were the engine's original design assumption — that it is software
you install on one machine — and everything awkward about running more than one
of anything traced back to them: import-time path constants, module-level
caches with second-long TTLs, a one-way "Sync Now" mirror into Postgres that
made two sources of truth out of one, and run checkpoints on a worker's local
disk that could only be resumed by the process that wrote them.

Layout
------
``models``   the tables. Dialect-portable; no Postgres-only types.
``engine``   connection, session factory, ``init_db``.
``upsert``   ``INSERT ... ON CONFLICT DO UPDATE`` for either dialect.
"""
from core.store.engine import (
    build_engine,
    build_session_factory,
    default_url,
    get_session,
    get_store,
    init_db,
    is_sqlite,
    reset_store,
    session,
    set_store,
)
from core.store.models import DEFAULT_TENANT, Base
from core.store.upsert import upsert

__all__ = [
    "Base",
    "DEFAULT_TENANT",
    "build_engine",
    "build_session_factory",
    "default_url",
    "get_session",
    "get_store",
    "init_db",
    "is_sqlite",
    "reset_store",
    "session",
    "set_store",
    "upsert",
]
