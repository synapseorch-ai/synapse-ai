"""
Worker heartbeat — runs in the background on each worker process.
Updates Postgres workers table and publishes to Redis pub/sub every N seconds.
"""
import asyncio
import time
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.scale.models_db import WorkerDB
from core.scale.pubsub import publish_worker_heartbeat


async def run_heartbeat(
    worker_id: str,
    address: str,
    hostname: str,
    redis_client,
    session_factory,
    get_active_jobs_fn,          # callable() -> int — returns current active job count
    max_jobs: int = 10,
    mcp_disabled: list | None = None,
    interval: int = 30,
) -> None:
    """Continuously publish heartbeat at `interval` seconds until cancelled."""
    mcp_disabled = mcp_disabled or []

    while True:
        try:
            active = get_active_jobs_fn()

            # Update Postgres workers table
            async with session_factory() as session:
                stmt = pg_insert(WorkerDB).values(
                    worker_id=worker_id,
                    hostname=hostname,
                    address=address,
                    status="online",
                    active_jobs=active,
                    max_jobs=max_jobs,
                    last_heartbeat=datetime.now(timezone.utc),
                    mcp_disabled=mcp_disabled,
                ).on_conflict_do_update(
                    index_elements=["worker_id"],
                    set_={
                        "status": "online",
                        "active_jobs": active,
                        "max_jobs": max_jobs,
                        "last_heartbeat": datetime.now(timezone.utc),
                        "mcp_disabled": mcp_disabled,
                    },
                )
                await session.execute(stmt)
                await session.commit()

            # Publish to Redis pub/sub for real-time UI updates
            await publish_worker_heartbeat(redis_client, worker_id, active, max_jobs)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[heartbeat] error: {e}", flush=True)

        await asyncio.sleep(interval)


async def mark_worker_offline(
    worker_id: str,
    session_factory,
) -> None:
    """Called during worker shutdown to mark the worker as offline in Postgres."""
    try:
        from sqlalchemy import update
        async with session_factory() as session:
            await session.execute(
                update(WorkerDB)
                .where(WorkerDB.worker_id == worker_id)
                .values(status="offline", last_heartbeat=datetime.now(timezone.utc))
            )
            await session.commit()
    except Exception as e:
        print(f"[heartbeat] failed to mark worker offline: {e}", flush=True)


async def reap_stale_workers(
    session_factory,
    stale_threshold_seconds: int = 90,
    interval: int = 60,
) -> None:
    """API-server background task: marks workers offline when heartbeat goes silent.

    Runs every `interval` seconds. A worker is considered stale when it has been
    `status='online'` but hasn't sent a heartbeat in `stale_threshold_seconds`.
    The default threshold (90 s) is 3× the normal 30-second heartbeat interval,
    giving workers two missed beats before being reaped.
    """
    from sqlalchemy import update
    from datetime import timedelta

    while True:
        await asyncio.sleep(interval)
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_threshold_seconds)
            async with session_factory() as session:
                result = await session.execute(
                    update(WorkerDB)
                    .where(WorkerDB.status == "online")
                    .where(WorkerDB.last_heartbeat < cutoff)
                    .values(status="offline")
                    .returning(WorkerDB.worker_id)
                )
                reaped = [row[0] for row in result.fetchall()]
                await session.commit()
            if reaped:
                print(f"[heartbeat] reaped {len(reaped)} stale worker(s): {reaped}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[heartbeat] reap_stale_workers error: {e}", flush=True)
