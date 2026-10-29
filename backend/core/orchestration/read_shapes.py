"""A saved workflow's steps in the shape the engine reads (F1235; the server's twin of the editor's F1054 reader).

The builder's step tools saved what a model wrote until F1047 (rc4.x on the cloud, every v1.9 release): a human
step's form fields as `{"fields": [...]}`, any `*_json` field as whatever it parsed to. The engine's step model
(`StepConfig`) refuses such a step, so the document stays stored but cannot be read: a builder update of it read
"the workflow to update did not read as a Plan" and changed nothing (rc5.3, 10-29). Nothing here loosens that model.
A reader that must take a stored document reads it through here first, and what it gets back validates or is
exactly what was stored.

The repairs follow `StepConfig`'s own errors, so a field added to it later is read the same way without a list here
to keep up: a list held alone in an object is that list (the builder's wrapping); a list entry in the wrong shape is
left out (a lone id where a branch is a list of ids is that branch); a number or true/false where text goes is that
text; a field set on a step type that does not read it is dropped; anything else is the field's default. Never
guessed at, and each repair is said, by step and field. Pure: the stored document is not changed.
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

from core.models_orchestration import StepConfig

#: Fields one step type reads and `StepConfig`'s rules refuse on any other (its `model_validator`s), and what the
#: rule needs: dropped from a step that does not read them, as the save would have refused them (F1047).
_ONLY_ON = (
    ("pass_images", lambda s: s.get("type") == "agent"),
    ("finish_tools", lambda s: s.get("type") == "agent"),
    ("finish_fallback", lambda s: bool(s.get("finish_tools"))),
    ("tool_arguments", lambda s: s.get("type") == "tool"),
)


def read_steps(steps: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Each step in the engine's shape, and what was read differently. A step that is not an object is left out."""
    if not isinstance(steps, list):
        return [], ([] if steps is None else [f"The workflow's steps were saved as {_shape(steps)}, not a list."])
    out, notes = [], []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            notes.append(f"Step {i + 1} was saved as {_shape(raw)}, not a step, and was left out.")
            continue
        step, said = read_step(raw, i)
        out.append(step)
        notes += said
    return out, notes


def read_step(raw: dict[str, Any], index: int = 0) -> tuple[dict[str, Any], list[str]]:
    """One step in the engine's shape, and what was read differently. A step these repairs cannot make readable (a
    type the engine does not know) comes back as it was stored, for its reader to refuse as before."""
    step = copy.deepcopy(raw)
    if not isinstance(step.get("id"), str) or not step["id"]:
        step["id"] = f"step_{index + 1}"
    if not isinstance(step.get("name"), str):
        step["name"] = step["id"]
    notes: list[str] = []

    def say(field: str, what: str) -> None:
        notes.append(f"Step '{step['id']}': its `{field}` {what}.")

    for field, reads in _ONLY_ON:
        if field in step and step[field] != StepConfig.model_fields[field].default and not reads(step):
            del step[field]
            say(field, f"is not read by {step.get('type')} steps, so it was left out")
    for _ in range(1000):  # one error mended a round, each one for good: a field or an entry fewer, or in its shape
        try:
            StepConfig.model_validate(step)
            return step, notes
        except ValidationError as e:
            errors = e.errors()
        if not _mend(step, errors[0], say):
            return copy.deepcopy(raw), []
    return copy.deepcopy(raw), []


def _mend(step: dict[str, Any], err: dict, say) -> bool:
    """The repair for one error, in place, at the deepest part of the step its location reaches. False when it is
    not a field's shape (a type the engine does not know, a rule across fields)."""
    loc = err.get("loc") or ()
    field = loc[0] if loc else None
    if not isinstance(field, str) or field in ("id", "type") or field not in step:
        return False
    parent, key = step, field
    for part in loc[1:]:
        held = parent[key]
        if not (isinstance(held, list) and isinstance(part, int) and part < len(held)
                or isinstance(held, dict) and part in held):
            break  # a union's member, not a place in the value
        parent, key = held, part
    value, kind = parent[key], err.get("type", "")
    if kind == "list_type" and isinstance(value, dict) and len(value) == 1 \
            and isinstance(next(iter(value.values())), list):
        parent[key] = next(iter(value.values()))
        say(field, "was saved inside an object; it is read as the list it holds")
    elif kind == "list_type" and isinstance(value, str) and parent is not step:
        parent[key] = [value]  # a lone id where a branch is a list of ids is that branch
    elif kind == "string_type" and isinstance(value, (int, float, bool)):
        parent[key] = str(value)
    elif parent is step:
        del step[field]
        say(field, f"was saved as {_shape(value)}, which the engine does not read, so it is left unset")
    else:
        parent.pop(key)
        say(field, f"held an entry saved as {_shape(value)}, which the engine does not read, so it was left out")
    return True


def _shape(value: Any) -> str:
    if isinstance(value, bool):
        return "true/false"
    return {dict: "an object", list: "a list", str: "text", int: "a number", float: "a number",
            type(None): "nothing"}.get(type(value), type(value).__name__)
