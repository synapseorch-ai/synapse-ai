"""Run ids: one per run, even when two runs start in the same millisecond (F259).

An id was `run_{orchestration}_{ms}`, and two requests in one millisecond — a v1
integration's burst, a webhook, two widget visitors, a schedule firing beside a
Run Now — were handed the same one. One run's record overwrote the other's, and
the caller polling its run read another request's input and result.

Six random hex characters go *after* the millisecond, so ids still sort by when
their run started (the run list and the cost sample read them newest first).
A leaf module: the loggers that mint ids are imported by the engine itself.
"""
from __future__ import annotations

import re
import secrets
import time


def new_run_id(name: str, prefix: str = "run") -> str:
    """`{prefix}_{name}_{ms}_{6 hex}` — `name` is the orchestration, schedule or agent."""
    return f"{prefix}_{name}_{int(time.time() * 1000)}_{secrets.token_hex(3)}"


def new_child_run_id(parent_run_id: str, step_id: str, depth: int) -> str:
    """`{parent}__{step}_d{depth}_{6 hex}` — one per run of a nested step (F251).

    `{parent}__{step}_d{depth}` alone was one id per step, so every Loop
    iteration and every retry of the step overwrote one record and one log.
    The `__` stays: it is what hides a child from every run list, the startup
    sweep and cloud's public views.
    """
    return f"{parent_run_id}__{step_id}_d{depth}_{secrets.token_hex(3)}"


_RUN_ID = re.compile(r"^run_(.+)_\d{13}_[0-9a-f]{6}$")


def orchestration_of(run_id: str) -> str | None:
    """The orchestration a `run_…` id was minted for, or None for another shape.

    An id minted before the suffix, `run_{orchestration}_{ms}`, still reads. A
    nested run's id names its parent, not its own orchestration: None.
    """
    if "__" in run_id:
        return None
    match = _RUN_ID.match(run_id)
    if match:
        return match.group(1)
    if run_id.startswith("run_") and run_id.count("_") >= 2:
        return run_id[len("run_"):].rsplit("_", 1)[0]
    return None
