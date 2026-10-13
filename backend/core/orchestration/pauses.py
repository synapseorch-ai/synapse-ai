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

import time

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


def pause_ordinal(step_history: list | None) -> int | None:
    """Which question a paused run is asking: its newest `paused` entry's index.

    A step id does not say — a human step in a loop asks once per iteration
    under one id — but each pause opens an entry of its own, and an answered
    one is closed where it stands, so the index names this question and no
    later one. An answer carries it (F111): an answer job whose question is no
    longer the one asked must not answer the one that is. None when no entry
    is paused (a run paused before the entry existed).
    """
    history = step_history or []
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if isinstance(entry, dict) and entry.get("status") == "paused":
            return index
    return None


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


def record_pause(step_history: list, entry: dict) -> None:
    """Record a pause: a step's first question adds its entry; a later question in
    the same execution closes the first as the wait it was (F463).

    A step whose approval was answered is `running` in its reopened entry. When it
    asked again, a second entry was added and the first stayed `running` for ever:
    one execution read as two by the loop guard and a host's pricing, and as a step
    that never ended in the run's log. Pausing the one entry again would give both
    questions one `pause_ordinal`, the index an answer names its question by
    (F136, F140), so a stale answer could approve a different call.

    F140's shape instead (Naveen, D267): the reopened entry becomes a closed `human`
    wait — free, outside active time and the executed-step count, and true, a
    person's wait inside one execution — from the first pause to its answer. The new
    pause, typed as the step, carries the step's start and every second it worked,
    so its end prices and times one execution.
    """
    for existing in reversed(step_history):
        if not isinstance(existing, dict) or existing.get("step_id") != entry.get("step_id"):
            continue
        if existing.get("status") == "running" and existing.get("step_type") != "human":
            worked = (existing.pop("worked_seconds", None) or 0) + (entry.get("worked_seconds") or 0)
            for key in ("started_at", "started_ms"):
                if existing.get(key):
                    entry[key] = existing[key]
            if worked:
                entry["worked_seconds"] = round(worked, 2)
            now = time.time()
            answered = existing.pop("resumed_ms", None)
            asked = existing.pop("paused_ms", None)
            waited_to = answered / 1000 if answered else now
            waited_from = min(asked / 1000, waited_to) if asked else waited_to
            existing.update(
                status="completed", step_type="human",
                started_at=_stamp(waited_from), started_ms=int(waited_from * 1000),
                ended_at=_stamp(waited_to), ended_ms=int(waited_to * 1000),
                duration_seconds=round(waited_to - waited_from, 2),
            )
        break
    step_history.append(entry)


def _stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def end_step(step_history: list, entry: dict) -> None:
    """Record a step's end: close the entry it has open, or add one.

    A step re-entered after its tool call was approved already has an entry —
    the pause's, reopened `running` by the resume. Its end closes that entry
    instead of adding a second, so a step that ran once has one entry: that is
    what a run's timeline shows and what cloud prices (F109). The entry keeps
    its start, whole-second and exact; its duration is this execution's, never
    the person's wait.
    """
    for existing in reversed(step_history):
        if (isinstance(existing, dict) and existing.get("step_id") == entry.get("step_id")
                and existing.get("status") == "running"):
            kept = {key: existing[key] for key in ("started_at", "started_ms") if existing.get(key)}
            # And what it worked before it asked (F439): the resumed stretch alone
            # left out every model call before the question.
            worked = existing.pop("worked_seconds", None)
            existing.pop("resumed_ms", None)
            existing.pop("paused_ms", None)
            existing.update(entry)
            existing.update(kept)
            if worked and isinstance(existing.get("duration_seconds"), (int, float)):
                existing["duration_seconds"] = round(existing["duration_seconds"] + worked, 2)
            if "started_ms" not in kept:
                # Opened before the exact stamps: its window stays the whole-second one.
                existing.pop("started_ms", None)
                existing.pop("ended_ms", None)
            return
    step_history.append(entry)
