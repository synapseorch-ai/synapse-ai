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
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.log import get_logger

_log = get_logger(__name__)

#: `(run_id, step_type) -> the sentence that stops the run, or None`.
RunGate = Callable[[str, str], Awaitable[str | None]]

_run_gate: RunGate | None = None


def set_run_gate(hook: RunGate | None) -> None:
    """Register (or, with None, clear) the host's gate."""
    global _run_gate
    _run_gate = hook


async def stop_reason(run_id: str, step_type: str) -> str | None:
    """Why this run may not start a step of `step_type`, or None to go on."""
    if _run_gate is None:
        return None
    try:
        return await _run_gate(run_id, step_type)
    except Exception:  # noqa: BLE001 — the host's billing must not end a run
        _log.warning("run gate failed for %s; the step goes ahead", run_id, exc_info=True)
        return None
