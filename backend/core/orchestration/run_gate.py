"""Whether a run may start its next step: a host's gate (F419).

A host that charges for each step — cloud's credit wallet — needs to stop a run
when what it has run would cost more than the workspace can pay. Its run length
(`time_limits.set_run_ceiling`) bounds time, not steps: a Free run of nested
loops ran 1,519 steps in its 30 minutes and overdrew its wallet by 833 credits,
because nothing between its first step and its last asked whether there was
anything left to pay with.

So the engine asks before every step it starts — at the top of the run, in a
Loop's body and in a Parallel's branches, and in a nested run, whose own steps
ask under its own run id. The host answers with the sentence that stops the run,
or None. A run the gate stops ends `failed` with that sentence as its own reason
(`run_error`), as a run its length stops does.

The self-hosted product sets no gate. A gate that fails stops nothing: billing
is the host's, and a lookup error must not end a customer's run.

The workflow's own cap on a run (`run_cap`, CAP1) is asked here too, first: the
same four places ask, and its stop ends the run the same way. A run at its cap
stops in the cap's words even when the workspace's wallet is empty as well.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.log import get_logger

from . import run_cap

_log = get_logger(__name__)

#: `(run_id, step_type) -> the sentence that stops the run, or None`.
RunGate = Callable[[str, str], Awaitable[str | None]]

_run_gate: RunGate | None = None


def set_run_gate(hook: RunGate | None) -> None:
    """Register (or, with None, clear) the host's gate."""
    global _run_gate
    _run_gate = hook


#: `(run_id, step_type) -> None`: a task a delegate's step just ran, priced on its entry 1 + tasks (TEAM1d).
TaskHook = Callable[[str, str], Awaitable[None]]

_task_hook: TaskHook | None = None


def set_task_hook(hook: TaskHook | None) -> None:
    """Register (or, with None, clear) the host's ear for a step's tasks (F669, TEAM1e).

    A host's gate is asked before each step for the next step's own price; a delegate's or a Team's step adds a
    price per task inside itself (TEAM1d), which the gate never saw until a new job read the run's history again —
    so a run of several such steps in one job went past what its workspace could pay. The host hears each task as
    the step counts it, and adds it where it keeps the run's tally."""
    global _task_hook
    _task_hook = hook


async def note_task(run_id: str, step_type: str) -> None:
    """A step counted one task. Never raises: billing is the host's, and must not end a run."""
    if _task_hook is None:
        return
    try:
        await _task_hook(run_id, step_type)
    except Exception:  # noqa: BLE001
        _log.warning("task hook failed for %s; the step goes on", run_id, exc_info=True)


async def stop_reason(
    run_id: str, step_type: str, cap: run_cap.RunCap | None = None,
) -> str | None:
    """Why this run may not start a step of `step_type`, or None to go on.

    `cap` is the run's own (`OrchestrationEngine.run_cap`): its root's, in a
    nested run.
    """
    capped = await run_cap.stop_reason(run_id, step_type, cap)
    if capped:
        return capped
    if _run_gate is None:
        return None
    try:
        return await _run_gate(run_id, step_type)
    except Exception:  # noqa: BLE001 — the host's billing must not end a run
        _log.warning("run gate failed for %s; the step goes ahead", run_id, exc_info=True)
        return None
