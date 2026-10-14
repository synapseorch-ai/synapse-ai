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
    redis=None,
) -> None:
    """API-server background task: marks workers offline when heartbeat goes silent.

    Runs every `interval` seconds. A worker is considered stale when it has been
    `status='online'` but hasn't sent a heartbeat in `stale_threshold_seconds`.
    The default threshold (90 s) is 3× the normal 30-second heartbeat interval,
    giving workers two missed beats before being reaped.

    Given `redis`, each round then fails the runs nothing is running any more
    (`sweep_orphaned_runs`, F470).
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
        if redis is not None:
            try:
                await sweep_orphaned_runs(redis)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[heartbeat] sweep_orphaned_runs error: {e}", flush=True)


#: Why a run nothing was running any more stopped (F470): the worker pumping it
#: in a task of its own was killed, or ran out of memory, and nothing hands such
#: a run out again.
RUN_ORPHANED = (
    "Stopped: the worker running this workflow stopped before it finished (a crash or a forced "
    "restart), and nothing was left running it."
)

#: The runs the last scale sweep found nothing running: failed if the next one
#: finds the same, so no run is ended on a single look (F470).
_suspects: set[str] = set()


async def _running_and_live():
    """The tenant's `running` runs as (run_id, worker_id), and the workers alive now."""
    from datetime import timedelta

    from sqlalchemy import select

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
    return running, live


async def _fail_stopped_run(run_id: str, reason: str) -> bool:
    """Fail a `running` run nothing is running, with `reason`; settle what it ran nested. False if not running."""
    from core.orchestration.descendants import settle_descendants
    from core.orchestration.journal import FileRunJournal
    from core.orchestration.run_error import set_run_error
    from core.orchestration.state import SharedState

    restored = await SharedState.restore(run_id)
    if restored.run.status != "running":
        return False
    restored.run.status = "failed"
    restored.run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Why, on the run itself (F257): it has no failed step to say it.
    set_run_error(restored.run, reason)
    await restored.checkpoint()
    # What it was running nested stopped with it (F257).
    await settle_descendants(run_id, "failed", reason)
    # Explain the stop to journal replays (reattach UI).
    if FileRunJournal.exists(run_id):
        journal = FileRunJournal(run_id)
        journal.append({"type": "orchestration_error", "error": reason})
        journal.close()
    return True


async def sweep_interrupted_runs(redis=None) -> list[str]:
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
    as before. In scale mode (`redis` given), a run whose lease is held is left
    too: a worker's chat turn pumps its workflow agent's run in a task of its own,
    with no owner on the row, and an API restart failed it under the live worker
    (F470). Accepted gap: an in-process run of a replica that holds no lease.

    Golden sets left in flight end too (F471): a set runs in the API process, so
    one this restart left is over, unless another server's lease holds it.

    A nested run (`parent__step`) is left to its parent, which this fails and
    whose resume re-enters it. The AI builder's own runs are swept like any
    other: skipping every "structural" run left an interrupted build `running`
    for ever with no pump behind it (F230).

    Returns the swept run ids.
    """
    from sqlalchemy import select

    from core.orchestration.state import SharedState
    from core.scale import run_lease
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    running, live = await _running_and_live()

    from core.orchestration.descendants import FINISHED, settle_descendants

    reason = "Server restarted while the run was in progress"
    swept: list[str] = []
    for run_id, worker_id in running:
        if "__" in (run_id or "") or (worker_id and worker_id in live):
            continue
        if redis is not None and await run_lease.held(run_id, redis):
            continue
        try:
            if await _fail_stopped_run(run_id, reason):
                swept.append(run_id)
        except Exception:
            pass

    # Nested runs left open by a root that had already stopped — before this
    # sweep settled a root's children, every timeout, cancel and crash left
    # some. A root still running on a live worker, or paused on a real
    # question, keeps its children as they are.
    async with session() as s:
        open_children = (await s.execute(
            select(OrchestrationRunDB.run_id)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .where(OrchestrationRunDB.run_id.contains("__", autoescape=True))
            .where(OrchestrationRunDB.status.in_(("running", "paused", "queued")))
        )).scalars().all()
    for root_id in sorted({cid.split("__", 1)[0] for cid in open_children} - set(swept)):
        try:
            root_status = (await SharedState.restore(root_id)).run.status
        except FileNotFoundError:
            root_status = None
        except Exception:  # noqa: BLE001
            continue
        if root_status is None or root_status in FINISHED:
            await settle_descendants(root_id, "failed", "Stopped: the run that called it had already ended")

    await _end_sets_nothing_holds(redis)
    return swept


async def _end_sets_nothing_holds(redis) -> list[str]:
    """End the golden sets left in flight that no lease holds (F471). With no `redis`, every one."""
    from core.evals.suites import CANCELLING, SET_GONE, end_set
    from core.scale import run_lease
    from core.store import evals as evals_store

    ended: list[str] = []
    try:
        rows = await evals_store.suite_runs_in(("running", CANCELLING))
    except Exception:  # noqa: BLE001 — a set's sweep never stops a run's
        return ended
    for row in rows:
        if redis is not None and await run_lease.held(row["id"], redis):
            continue
        try:
            if await end_set(row["id"], SET_GONE):
                ended.append(row["id"])
        except Exception:  # noqa: BLE001
            pass
    return ended


async def sweep_orphaned_runs(redis) -> list[str]:
    """Scale mode, every minute: fail the `running` runs nothing is running any more (F470).

    The boot sweep's rule, while the server is up: a root run is left alone when
    a live worker owns it (a run job's: ARQ hands it out again), when this
    process pumps it, or when its lease is held — a task of its own pumps it
    somewhere (a workflow agent's turn, a delegated workflow, a golden set's
    case). Anything else is failed with `RUN_ORPHANED`, but only when two looks
    in a row find it so: a run is never ended on one. Golden sets no lease
    holds end too (F471).
    """
    from core.orchestration.runner import _active_tasks
    from core.scale import run_lease

    running, live = await _running_and_live()
    seen: set[str] = set()
    swept: list[str] = []
    for run_id, worker_id in running:
        if "__" in (run_id or "") or (worker_id and worker_id in live) or run_id in _active_tasks:
            continue
        if await run_lease.held(run_id, redis):
            continue
        seen.add(run_id)
        if run_id not in _suspects:
            continue
        try:
            if await _fail_stopped_run(run_id, RUN_ORPHANED):
                swept.append(run_id)
                print(f"[heartbeat] run {run_id}: nothing was running it — failed (F470)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[heartbeat] run {run_id}: could not be failed: {e}", flush=True)
    _suspects.clear()
    _suspects.update(seen - set(swept))
    await _end_sets_nothing_holds(redis)
    return swept
