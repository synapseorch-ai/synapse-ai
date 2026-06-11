"""
Async SQLAlchemy engine and session factory for Postgres.
Used by both the API server (lifespan) and ARQ workers (startup hook).
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool

from core.scale.models_db import Base


def build_engine(postgres_url: str, pgbouncer_mode: bool = False) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    PgBouncer transaction-pooling mode is incompatible with SQLAlchemy's
    connection-level pooling, so we use NullPool when pgbouncer_mode=True.
    """
    kwargs = dict(echo=False, future=True)
    if pgbouncer_mode:
        # NullPool: open/close a real DB connection on every async with session
        kwargs["poolclass"] = NullPool
    else:
        kwargs["poolclass"] = AsyncAdaptedQueuePool
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True

    return create_async_engine(postgres_url, **kwargs)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables defined in models_db.py (CREATE TABLE IF NOT EXISTS)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session(session_factory: async_sessionmaker) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
