"""
Compatibility shim. The connection layer moved to ``core/store/engine.py``.

``build_engine`` here keeps its original Postgres-shaped signature
(``postgres_url``) so scale callers are unchanged; it now delegates to the
dialect-aware builder, which also means a scale deployment can be pointed at
SQLite for a local smoke test without a second code path.

Import from ``core.store`` in new code.
"""
from core.store.engine import (  # noqa: F401
    build_session_factory,
    get_session,
    init_db,
)
from core.store.engine import build_engine as _build_engine
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["build_engine", "build_session_factory", "get_session", "init_db"]


def build_engine(postgres_url: str, pgbouncer_mode: bool = False) -> AsyncEngine:
    return _build_engine(postgres_url, pgbouncer_mode=pgbouncer_mode)
