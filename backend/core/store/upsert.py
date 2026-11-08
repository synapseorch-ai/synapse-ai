"""
Dialect-aware INSERT ... ON CONFLICT DO UPDATE.

Both SQLite and Postgres support upsert with the same semantics, but through
two different constructs — ``sqlalchemy.dialects.sqlite.insert`` and
``sqlalchemy.dialects.postgresql.insert``. They are not interchangeable: the
Postgres one emits nothing SQLite understands, and picking it unconditionally
is what makes an otherwise portable model layer Postgres-only in practice.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert(
    session: AsyncSession,
    model: Any,
    values: dict,
    index_elements: Sequence[str],
    update: Sequence[str] | None = None,
) -> None:
    """Insert `values` into `model`, updating on a conflict over `index_elements`.

    `update` names the columns to overwrite on conflict; it defaults to every
    supplied column except the conflict key. Naming the key columns in the
    update set is accepted by both dialects but is a no-op that obscures intent.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = _sqlite_insert if dialect == "sqlite" else _pg_insert

    stmt = insert(model).values(**values)
    keys = set(index_elements)
    columns = update if update is not None else [k for k in values if k not in keys]

    stmt = stmt.on_conflict_do_update(
        index_elements=list(index_elements),
        set_={name: getattr(stmt.excluded, name) for name in columns},
    )
    await session.execute(stmt)
