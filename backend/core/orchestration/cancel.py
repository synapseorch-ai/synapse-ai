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

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

#: Statuses a run does not leave by being cancelled.
FINISHED = frozenset({"completed", "failed", "cancelled"})

#: What a cancel says on a run's stream: the pump's words for a running run,
#: the fleet's (`_finish_cancelled`) and cloud's (`run_cancel.py`) too.
CANCELLED_EVENT = {"type": "orchestration_error", "error": "Cancelled"}


def _close_open_question(step_history: list) -> None:
    """Close the `paused` entry a cancelled run was waiting on (F141).

    The newest one, whichever step it names — inside a Loop or a Parallel
    branch the run's position is the container, not the step that asked (F109).

    Without this the entry stays open, and anything rendering the timeline
    measures its duration against the wall clock for as long as somebody looks
    at it. Cloud has done this since its own cancel route was written
    (`api/control/run_cancel._close_paused_step`); this is the shape it follows,
    with `cancelled` where a resume writes `completed`.
    """
    from core.orchestration.descendants import close_open_question
    close_open_question(step_history, "cancelled")


@dataclass
class Cancelled:
    #: False when there is no such run here: no task, no stored state.
    found: bool
    #: True when something was actually stopped.
    stopped: bool
    #: The run's status afterwards, when it has one.
    status: Optional[str]


async def _announce(run_id: str, redis: Any = None) -> None:
    """Tell everything fed by the run's events that it was cancelled (F193).

    A running run's pump says so itself when its task is cancelled. A paused
    run has no pump — it ended at the pause — so its cancel was silent: the
    bell kept its "needs your input" item open, and a reopened run replayed
    its journal to the question and offered Answer again. The same two events
    the pump would have written go through the pump's observers: the journal,
    a live tail and the notification hub. With Redis, a scale-mode reader gets
    them too, as the fleet's `_finish_cancelled` sends them.
    """
    from core.orchestration.journal import aclose_journal
    from core.orchestration.runner import _observe

    await _observe(run_id, dict(CANCELLED_EVENT))
    await _observe(run_id, {"type": "run_stream_end", "paused": False})
    await aclose_journal(run_id)
    if redis is not None:
        try:
            from core.scale.pubsub import RunEventPublisher
            publisher = RunEventPublisher(redis, run_id)
            await publisher.publish(dict(CANCELLED_EVENT))
            await publisher.publish_done()
        except Exception:  # noqa: BLE001 — no Redis is not a failure to cancel here
            pass


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
        # Anything still open under it is an orphan of an earlier stop (F257).
        await _settle_children(run_id)
        return Cancelled(found=True, stopped=False, status=status)

    # Signal the engine loop to exit on its next iteration (in-process path)…
    _cancelled_run_ids.add(run_id)
    # …and cancel its task, to interrupt an await already in progress (a model call).
    stopped = False
    if live:
        _active_tasks.pop(run_id, None)
        task.cancel()
        stopped = True
        # Let the cancel land before the record is written: a checkpoint the
        # task was in the middle of must not come after ours. Bounded, and
        # nothing is waited on for a run with no task here.
        if task is not asyncio.current_task():
            await asyncio.wait({task}, timeout=5)

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
        was_paused = status == "paused"
        if status not in FINISHED:
            restored.run.status = "cancelled"
            restored.run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # A cancelled run is not asking anything (F141). The flag and the
            # open entry used to survive the cancel, so every reader still
            # believed it was waiting: the orchestration screen kept its answer
            # strip up, offering an Answer the API refuses with a 409. The
            # fleet's adapter has always cleared the flag here; this is the
            # in-process path catching up.
            restored.run.waiting_for_human = False
            _close_open_question(restored.run.step_history)
            await restored.checkpoint()
            status, stopped = "cancelled", True
            if was_paused and not live:
                await _announce(run_id, redis)
    await _settle_children(run_id)

    return Cancelled(found=restored is not None or stopped, stopped=stopped, status=status)


async def _settle_children(run_id: str) -> None:
    """Every run nested under a cancelled one is cancelled too, saying so (F257)."""
    from core.orchestration.descendants import settle_descendants
    await settle_descendants(run_id, "cancelled", "Stopped: the run that called it was cancelled")
