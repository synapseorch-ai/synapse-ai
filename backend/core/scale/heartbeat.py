"""
Worker heartbeat — runs in the background on each worker process.
Updates the workers table every N seconds; the Scale screen reads it.
"""
import asyncio
import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.store import upsert
from core.store.models import WorkerDB

#: A worker that has not beaten in this long is gone. 3× the 30 s heartbeat —
#: two missed beats — and the threshold `reap_stale_workers` uses.
STALE_WORKER_SECONDS = 90


async def run_heartbeat(
    worker_id: str,
    address: str,
    hostname: str,
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

            # Update the workers table
            async with session_factory() as session:
                await upsert(
                    session,
                    WorkerDB,
                    values={
                        "worker_id": worker_id,
                        "hostname": hostname,
                        "address": address,
                        "status": "online",
                        "active_jobs": active,
                        "max_jobs": max_jobs,
                        "last_heartbeat": datetime.now(timezone.utc),
                        "mcp_disabled": mcp_disabled,
                    },
                    index_elements=["worker_id"],
                    # hostname/address are set once at registration; a heartbeat
                    # that rewrote them would mask a worker_id collision rather
                    # than surfacing it.
                    update=["status", "active_jobs", "max_jobs", "last_heartbeat", "mcp_disabled"],
                )
                await session.commit()

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
    stale_threshold_seconds: int = STALE_WORKER_SECONDS,
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


async def sweep_interrupted_runs() -> list[str]:
    """API-server startup: fail the `running` runs nothing is running any more.

    Runs in every mode, before the scale layer starts. A run the previous server
    process was executing in-process died with it. A run a **worker** owns may
    not have: workers outlive an API restart, and failing that run under a live
    worker left it marked failed while it ran — with Replay offered on it, which
    would run it a second time (F85).

    So a run is left alone when its `worker_id` names a worker that is `online`
    and has beaten within `STALE_WORKER_SECONDS`. Everything else is failed with
    no failed history entry, which is how a redelivered job recognises an
    interrupted run and resumes it (`core/scale/worker.py::_redelivered`).

    Standalone installs have no workers rows, so every `running` run is failed,
    as before. Accepted gap: in-process runs have no owner column, so a second
    API replica restarting fails the in-process runs a live replica is executing.

    Returns the swept run ids.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from core.orchestration.journal import FileRunJournal
    from core.orchestration.state import SharedState
    from core.orchestration.state_store import _is_structural
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        running = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.worker_id)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .where(OrchestrationRunDB.status == "running")
        )).all()
        owners = {w for _, w in running if w}
        workers = (await s.execute(
            select(WorkerDB.worker_id, WorkerDB.status, WorkerDB.last_heartbeat)
            .where(WorkerDB.worker_id.in_(owners))
        )).all() if owners else []

    # Compared here rather than in SQL: SQLite hands the timestamp back naive.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_WORKER_SECONDS)
    live = set()
    for worker_id, status, beat in workers:
        if beat is not None and beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
        if status == "online" and beat is not None and beat >= cutoff:
            live.add(worker_id)

    swept: list[str] = []
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for run_id, worker_id in running:
        if _is_structural(run_id or "") or (worker_id and worker_id in live):
            continue
        try:
            restored = await SharedState.restore(run_id)
            restored.run.status = "failed"
            restored.run.ended_at = now_str
            await restored.checkpoint()
            # Explain the stop to journal replays (reattach UI).
            if FileRunJournal.exists(run_id):
                journal = FileRunJournal(run_id)
                journal.append({"type": "orchestration_error",
                                "error": "Server restarted while the run was in progress"})
                journal.close()
            swept.append(run_id)
        except Exception:
            pass
    return swept
