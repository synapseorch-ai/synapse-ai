"""Where a paused run is waiting, and which step asked.

A run's `current_step_id` is where the engine resumes: the top-level step it
drives. When the question was asked inside a Loop body, a Parallel branch or a
nested orchestration, that is the container, not the step that asked. So the
two are recorded apart:

- the step that asked is the run's newest `paused` entry in `step_history`,
  written by the engine at the pause (with `within`, the container holding it);
- each container between the two keeps a `paused` record in its progress key,
  saying where inside it the run stopped, so answering can carry the container
  on from there instead of stepping past it (F109).

Nothing here imports the engine: anything that needs to know which step is
waiting — the messaging layer, cloud's notifier and cancel route — reads these.
"""
from __future__ import annotations

from typing import Any, Mapping

#: The container step types that keep a pause record, and the key each keeps it
#: under. The one spelling of these keys: the executors read and write them
#: through `progress_key`.
_PROGRESS_PREFIX = {"loop": "_loop_progress_", "parallel": "_parallel_progress_"}


def _type_of(step: Any) -> str:
    kind = getattr(step, "type", None)
    return str(getattr(kind, "value", kind) or "")


def progress_key(step: Any) -> str | None:
    """The shared-state key a Loop or Parallel step keeps its progress under."""
    prefix = _PROGRESS_PREFIX.get(_type_of(step))
    return f"{prefix}{step.id}" if prefix else None


def asking_step_id(step_history: list | None, current_step_id: str | None) -> str | None:
    """The step a paused run is waiting on: its newest `paused` history entry.

    Falls back to `current_step_id`, which is the same step for a question asked
    at the top level — and for a run paused before the entry existed.
    """
    for entry in reversed(step_history or []):
        if isinstance(entry, dict) and entry.get("status") == "paused":
            return entry.get("step_id") or current_step_id
    return current_step_id


def pause_chain(step_map: Mapping[str, Any], shared_state: Mapping[str, Any],
                position: str | None) -> list[dict]:
    """The containers' pause records from `position` inwards, outermost first.

    Each is the `paused` dict a container wrote when a step inside it stopped to
    ask; its `step_id` is either the step that asked or the next container in.
    Empty when `position` is not a container holding a pause.
    """
    chain: list[dict] = []
    seen: set[str] = set()
    step_id = position
    while step_id and step_id not in seen:
        seen.add(step_id)
        step = step_map.get(step_id)
        key = progress_key(step) if step is not None else None
        record = shared_state.get(key) if key else None
        paused = record.get("paused") if isinstance(record, dict) else None
        if not isinstance(paused, dict) or not paused.get("step_id"):
            break
        chain.append(paused)
        step_id = paused["step_id"]
    return chain


def innermost_container(position: str, chain: list[dict]) -> str:
    """The container holding the step that asked — `position` itself at one level."""
    containers = [position] + [p["step_id"] for p in chain[:-1]]
    return containers[-1]


def end_step(step_history: list, entry: dict) -> None:
    """Record a step's end: close the entry it has open, or add one.

    A step re-entered after its tool call was approved already has an entry —
    the pause's, reopened `running` by the resume. Its end closes that entry
    instead of adding a second, so a step that ran once has one entry: that is
    what a run's timeline shows and what cloud prices (F109). The entry keeps
    its start; its duration is this execution's, never the person's wait.
    """
    for existing in reversed(step_history):
        if (isinstance(existing, dict) and existing.get("step_id") == entry.get("step_id")
                and existing.get("status") == "running"):
            started_at = existing.get("started_at")
            existing.update(entry)
            if started_at:
                existing["started_at"] = started_at
            return
    step_history.append(entry)
