"""Stopping a run — one implementation for every route that can.

`POST /api/orchestrations/runs/{id}/cancel`, `POST /api/v1/orchestrations/runs/{id}/cancel`
and Stop on a chat turn whose agent started a run all end up here, so they stop
a run the same way: the in-process flag the engine checks between steps, the
task (to interrupt a model call already in flight), the Redis key workers
watch, and the run's own status.

**A finished run keeps what it finished as.** The status used to be written
`cancelled` whatever it was, so cancelling a completed run — a double click, a
stale tab, an API retry — rewrote its history.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

#: Statuses a run does not leave by being cancelled.
FINISHED = frozenset({"completed", "failed", "cancelled"})


@dataclass
class Cancelled:
    #: False when there is no such run here: no task, no stored state.
    found: bool
    #: True when something was actually stopped.
    stopped: bool
    #: The run's status afterwards, when it has one.
    status: Optional[str]


async def cancel_run(run_id: str, *, redis: Any = None) -> Cancelled:
    from core.orchestration.runner import _active_tasks
    from core.orchestration.state import SharedState, _cancelled_run_ids

    try:
        status = (await SharedState.restore(run_id)).run.status
    except FileNotFoundError:
        status = None  # it may still be inside its first step, with no checkpoint yet

    task = _active_tasks.get(run_id)
    live = task is not None and not task.done()
    if status in FINISHED and not live:
        return Cancelled(found=True, stopped=False, status=status)

    # Signal the engine loop to exit on its next iteration (in-process path)…
    _cancelled_run_ids.add(run_id)
    # …and cancel its task, to interrupt an await already in progress (a model call).
    stopped = False
    if live:
        _active_tasks.pop(run_id, None)
        task.cancel()
        stopped = True

    # Distributed cancel: workers on other machines watch this key.
    if redis is not None:
        try:
            from core.scale.pubsub import publish_cancellation
            await publish_cancellation(redis, run_id)
        except Exception:  # noqa: BLE001 — no Redis is not a failure to cancel here
            pass

    # Read again after the cancel, so what is written back is the run as it
    # stopped, not as it stood before.
    try:
        restored = await SharedState.restore(run_id)
    except FileNotFoundError:
        restored = None
    if restored is not None:
        status = restored.run.status
        if status not in FINISHED:
            restored.run.status = "cancelled"
            restored.run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            await restored.checkpoint()
            status, stopped = "cancelled", True

    return Cancelled(found=restored is not None or stopped, stopped=stopped, status=status)
