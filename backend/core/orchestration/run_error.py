"""Why a run did not complete, kept on the run (F257).

A run that failed at a step has the reason on that step's history entry. One
that failed outside any step — its time budget, its turn limit, a step that
does not exist, a restart, a parent that stopped — had nowhere to keep it: the
reason was said once on the stream and lost, and everything reading the run
afterwards ("Orchestration failed. Error: Unknown error", v1, the run list)
had nothing to say.

It lives in `step_progress["__run__"]` beside the run's active seconds (F258):
no column and no migration, stripped from every public view, never read by a
prompt, a transform or a memory summary. A completed run drops the entry, and
any Replay starts without it. Readers ask `failure_reason`, which falls back to
the newest step error.
"""
from __future__ import annotations

from typing import Any

#: The run's own entry in `step_progress` — `engine.ACTIVE_TIME_KEY`.
RUN_KEY = "__run__"


def run_error(run: Any) -> str | None:
    """The reason recorded for the run itself, if any."""
    record = (getattr(run, "step_progress", None) or {}).get(RUN_KEY) or {}
    error = record.get("error") if isinstance(record, dict) else None
    return str(error) if error else None


def set_run_error(run: Any, reason: str | None) -> None:
    """Record (or, with None, clear) the run's own reason, keeping its active seconds."""
    record = dict((run.step_progress or {}).get(RUN_KEY) or {})
    if reason:
        record["error"] = reason
    else:
        record.pop("error", None)
    if record:
        run.step_progress[RUN_KEY] = record
    else:
        run.step_progress.pop(RUN_KEY, None)


def failure_reason(status: str | None, step_history: list | None, step_progress: dict | None) -> str | None:
    """Why a run that did not complete stopped: its own reason, else its newest step error.

    From the parts rather than a run, so a stored row (the run list) and a live
    run read the same answer. None for a run that completed or is still going.
    """
    if status not in ("failed", "cancelled"):
        return None
    record = (step_progress or {}).get(RUN_KEY) or {}
    if isinstance(record, dict) and record.get("error"):
        return str(record["error"])
    for entry in reversed(step_history or []):
        if isinstance(entry, dict) and entry.get("error"):
            return str(entry["error"])
    return None
