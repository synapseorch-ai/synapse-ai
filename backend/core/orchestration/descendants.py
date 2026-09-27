"""Settling the runs nested under a run that stopped (F257).

A nested child runs inside its parent's task, so a parent that stops — a step
timeout, a cancel, a crash — takes the child down with it. But the child's own
record was never told: the stop reaches it as a cancellation, which neither
its executor nor its loop records, and every path that settles a stopped run
(the engine's finalize, the cancel route, the fleet's cancel, the startup
sweep) settled the root alone. So the children read `running`, or `paused` on
a question nobody could answer any more, for ever.

Every one of those paths now hands the root's ending to each unfinished run
under it (`{run_id}__…`, at any depth), with the reason. A child that already
finished — completed, or failed on its own — keeps its ending.
"""
from __future__ import annotations

import time

from core.log import get_logger

from .run_error import set_run_error

_log = get_logger(__name__)

FINISHED = frozenset({"completed", "failed", "cancelled"})


def close_open_question(step_history: list, status: str) -> None:
    """Close the newest `paused` entry, as `status` — the wait it held is over."""
    now = time.gmtime()
    for entry in reversed(step_history or []):
        if not isinstance(entry, dict) or entry.get("status") != "paused":
            continue
        entry["status"] = status
        entry["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", now)
        try:
            began = time.strptime(str(entry.get("started_at")), "%Y-%m-%dT%H:%M:%SZ")
            entry["duration_seconds"] = max(0.0, time.mktime(now) - time.mktime(began))
        except (ValueError, TypeError):
            entry["duration_seconds"] = 0.0
        return


async def settle_descendants(run_id: str, status: str, reason: str) -> list[str]:
    """Give every unfinished run nested under `run_id` the ending `status`, with `reason`.

    Returns the ids settled. Never raises: a run that stopped has stopped, and a
    child it could not settle is logged, not a second failure.
    """
    from .state import SharedState
    from .state_store import unfinished_descendants

    try:
        child_ids = await unfinished_descendants(run_id)
    except Exception as exc:  # noqa: BLE001 — settling is best effort on a run already over
        _log.warning(f"descendants of {run_id}: could not be listed: {exc}")
        return []

    settled: list[str] = []
    for child_id in child_ids:
        try:
            restored = await SharedState.restore(child_id)
        except FileNotFoundError:
            continue
        child = restored.run
        if child.status in FINISHED:
            continue
        child.status = status
        child.waiting_for_human = False
        child.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        close_open_question(child.step_history, status)
        set_run_error(child, reason)
        try:
            await restored.checkpoint()
            settled.append(child_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning(f"descendant {child_id} of {run_id} could not be settled: {exc}")
    return settled
