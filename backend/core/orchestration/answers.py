"""
Answering a paused run — from the browser or from a messaging channel.

One implementation for both, so a channel reply resumes a run exactly the way
the app's Answer control does (F54): a run a worker ran goes back to the fleet
as `resume_orchestration_job`, with the answer as the job's argument; any other
run is resumed in this process.

**A run is answered once.** A run that is not paused on a question is refused
(`RunNotWaiting`): the browser and a channel can both answer the same question,
and before this the engine had no such guard — a second answer was written into
whatever step the run had reached by then.

**And once when two answers arrive together (F136).** Both used to pass the
check above — here, across the `await` that reads the run; on the fleet, in two
jobs picked up in one poll — and both drove the run from the same checkpoint.
In this process the run is claimed before the first `await`; on the fleet the
worker claims it with one conditional UPDATE (`state_store.claim_answer`).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional


class RunNotWaiting(Exception):
    """The run is not paused on a question. `status` is None when there is no such run."""

    def __init__(self, run_id: str, status: Optional[str]):
        self.run_id = run_id
        self.status = status
        super().__init__(
            f"Run {run_id} not found" if status is None
            else f"Run {run_id} is {status} and is not waiting for an answer"
        )


class RunAlreadyActive(Exception):
    """Another answer already has this run: a pump driving it in this process,
    an answer being applied here, or one just queued for the fleet."""


@dataclass
class Answered:
    #: True when a worker will resume the run; its events are on the run's Redis stream.
    on_fleet: bool
    #: The in-process run's event queue, when resumed here.
    queue: Optional[asyncio.Queue] = None


async def worker_run_row(app_state: Any, run_id: str):
    """The run's row when a worker ran it and the scale layer is up, else None.

    A run a worker ran has `worker_id` set; its state is in the store, so any
    worker can pick it up again, and it goes back to the fleet rather than
    running inside the API process.
    """
    redis = getattr(app_state, "redis", None)
    arq_redis = getattr(app_state, "arq_redis", None)
    pg_session_factory = getattr(app_state, "pg_session_factory", None)
    if not (redis and arq_redis and pg_session_factory):
        return None
    from sqlalchemy import select

    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with pg_session_factory() as session:
        row = (await session.execute(
            select(
                OrchestrationRunDB.worker_id,
                OrchestrationRunDB.status,
                OrchestrationRunDB.current_step_id,
                OrchestrationRunDB.waiting_for_human,
                OrchestrationRunDB.step_history,
            ).where(
                OrchestrationRunDB.run_id == run_id,
                OrchestrationRunDB.tenant_id == get_tenant(),
            )
        )).one_or_none()
    return row if row is not None and row.worker_id else None


def _waiting(status: Optional[str], waiting_for_human: Any) -> bool:
    return status == "paused" and bool(waiting_for_human)


#: Runs an answer is being applied to in this process, from before the answer's
#: first `await` until its pump is registered in `runner._active_tasks`. Taken
#: and checked with no `await` between, which is the whole of its correctness:
#: the event loop cannot hand a second answer the run in between. A run is
#: resumed in-process only by the process that holds it, so this set is enough
#: here; several processes are the fleet's case, and the fleet claims the row.
_answering: set[str] = set()


async def answer_paused_run(app_state: Any, run_id: str, response: Any) -> Answered:
    """Resume `run_id` with `response`, on the fleet or in this process.

    Raises `RunNotWaiting` for a run that is not paused on a question and
    `RunAlreadyActive` when this process is already running it.
    """
    try:
        row = await worker_run_row(app_state, run_id)
    except Exception:  # noqa: BLE001 — the store for the fleet is unreachable; treat as local
        row = None

    if row is not None:
        if not _waiting(row.status, row.waiting_for_human):
            raise RunNotWaiting(run_id, row.status)
        from core.orchestration.pauses import pause_ordinal
        from core.scale.config import QUEUE_NAME

        resp = response if isinstance(response, dict) else {"response": response}
        job = await app_state.arq_redis.enqueue_job(
            "resume_orchestration_job",
            run_id=run_id,
            human_response=resp,
            # The question this answer is for: a job that finds the run asking
            # another does not answer it (F111).
            pause_ordinal=pause_ordinal(row.step_history),
            _queue_name=QUEUE_NAME,
            _job_id=f"resume_{run_id}_{int(time.time() * 1000)}",
        )
        if job is None:
            # ARQ keeps one job per id and answers None for a second: an answer
            # to this run was queued this same millisecond. It was dropped, so
            # it must not be reported as queued.
            raise RunAlreadyActive(run_id)
        return Answered(on_fleet=True)

    from core.orchestration.runner import _active_tasks, spawn_engine_run
    from core.orchestration.state import SharedState

    # Two pumps for one run would interleave its journal and duplicate work.
    # Checked and claimed with no `await` between (F136).
    active = _active_tasks.get(run_id)
    if run_id in _answering or (active is not None and not active.done()):
        raise RunAlreadyActive(run_id)
    _answering.add(run_id)
    try:
        try:
            run = (await SharedState.restore(run_id)).run
        except FileNotFoundError:
            raise RunNotWaiting(run_id, None)
        if not _waiting(run.status, run.waiting_for_human):
            raise RunNotWaiting(run_id, run.status)

        from core.orchestration.engine import OrchestrationEngine

        # Registers the pump in `_active_tasks` before it returns, so the claim
        # can be let go as soon as it has.
        _task, queue = spawn_engine_run(
            OrchestrationEngine.resume(run_id, response, app_state.server_module), run_id
        )
    finally:
        _answering.discard(run_id)
    return Answered(on_fleet=False, queue=queue)
