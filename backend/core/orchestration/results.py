"""What a run made — one rule for every reader of it (F249).

A run's result was the value of the last step with an output key, whatever kind
of step it was. A workflow ending in a person's confirmation made that
confirmation its result (`{'ok': True, …}` reached a parent three times over,
the research before it never did), and one ending in an evaluator answered its
chat with the route label ("pass").

Now an orchestration may name its `result_key`; otherwise the result is the
last step that produced content. A person's answer, an evaluator's label and an
End are how a run got somewhere, not what it made. The engine's final answer,
v1's `response`, the judge's evidence and a nested parent's value all read it
here, and it imports nothing of the engine's itself (the package's own
`__init__` does, so callers outside it import this lazily, as the judge does).
"""
from __future__ import annotations

from typing import Any

#: Step types whose output is not what a run made.
NOT_CONTENT = frozenset({"human", "evaluator", "end"})


def result_of(step_history: list | None, shared_state: dict | None, result_key: str = "") -> Any:
    """The run's result as the value it is, or None when it made nothing.

    `result_key` wins when the run wrote it; a key it never wrote falls back to
    the last content step rather than to nothing. An entry that names no type
    (older records) counts as content.
    """
    state = shared_state or {}
    if result_key and result_key in state:
        return state[result_key]
    for entry in reversed(step_history or []):
        if not isinstance(entry, dict) or entry.get("step_type") in NOT_CONTENT:
            continue
        key = entry.get("output_key")
        if key and key in state:
            return state[key]
    return None


def nested_result(events: list[dict], result_key: str = "") -> Any:
    """What a finished child run hands its parent, read from its own events.

    Its terminal `final` carries the run's history and state, so the value comes
    back as it is — a list stays a list. A run that made nothing hands back its
    final text, as it always has. None when there is no terminal `final` at all.
    """
    for event in reversed(events):
        if event.get("type") == "final" and event.get("intent") == "orchestration":
            data = event.get("data") or {}
            value = result_of(data.get("step_history"), data.get("shared_state"), result_key)
            return value if value is not None else event.get("response", "")
    return None
