"""Resuming a failed or stopped run from the step it stopped at: the one place it is decided (F1260, REACH1).

The run page's Replay (`POST /api/orchestrations/runs/{id}/resume`) decided it inline, so nothing else could resume a
run with the same refusals. Now the route and Synapse AI's card (the product tool `resume_run`, through the run
backend) both come here:

* **why_not**: a nested run is its parent's to replay (F251); only a failed or cancelled run is; one that never
  reached a step has nothing to resume from. Said in the route's own words, which the route answers with as a 409.
* **resume**: a run a worker ran is replayed by a worker (`resume_failed_job`, F61), its stream bridged from the
  tail of its events; any other run is resumed in this process, never two pumps for one run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NotResumable(Exception):
    """Why this run is not resumed now, in a sentence fit to show."""


NESTED = "This is a nested run; replay the run that called it, which reruns this step."
NO_STEP = "This run never reached a step, so there is nothing to resume — start it again."
ACTIVE = "Run is already active"
QUEUED = "A replay of this run is already queued."


def _status_words(status: str) -> str:
    return f"The run is {status}; only a failed or cancelled run can be replayed."


@dataclass
class Resumed:
    """A resume begun: its event queue when it runs in this process, else where its worker's stream starts."""

    run_id: str
    queue: Any = None
    bridge_from: str | None = None

    @property
    def on_a_worker(self) -> bool:
        return self.queue is None


def refusal(run_id: str, status: str | None, current_step_id: str | None) -> str | None:
    """Why a run with this status and step cannot be resumed, or None when it can."""
    if "__" in run_id:
        return NESTED
    if status not in ("failed", "cancelled"):
        return _status_words(status or "not there")
    if not current_step_id:
        return NO_STEP
    return None


async def why_not(run: Any) -> str | None:
    """`refusal` for a run as its checkpoint holds it (the run page's own read)."""
    return refusal(run.run_id, run.status, run.current_step_id)


async def resume(app_state: Any, run_id: str, server_module: Any) -> Resumed:
    """Resume the run, or raise `NotResumable` with why."""
    from core.orchestration.answers import worker_run_row
    from core.orchestration.runner import _active_tasks, spawn_engine_run

    if "__" in run_id:
        raise NotResumable(NESTED)
    worker_run = await worker_run_row(app_state, run_id)
    if worker_run is not None:
        if (why := refusal(run_id, worker_run.status, worker_run.current_step_id)) is not None:
            raise NotResumable(why)
        redis = app_state.redis
        # Bridge from the stream's tail: it still holds the failed attempt's `done`, and a bridge that read from the
        # start would stop there.
        tail = await redis.xrevrange(f"synapse:run:{run_id}:events", "+", "-", count=1)
        start = tail[0][0] if tail else "0"
        start = start.decode() if isinstance(start, bytes) else start

        from core.scale.replay import queue_replay

        if await queue_replay(redis, app_state.arq_redis, run_id) is None:
            raise NotResumable(QUEUED)
        return Resumed(run_id, bridge_from=start)

    # Two pumps for one run would interleave its journal and duplicate work.
    active = _active_tasks.get(run_id)
    if active is not None and not active.done():
        raise NotResumable(ACTIVE)

    from core.orchestration.engine import OrchestrationEngine

    _task, queue = spawn_engine_run(OrchestrationEngine.resume_failed(run_id, server_module), run_id)
    return Resumed(run_id, queue=queue)
