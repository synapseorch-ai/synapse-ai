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

What still does not read is refused to a run in words (`load_to_run`): the step by its name and id, the field, the
shape it needs, and where to mend it, never pydantic's text or a server error.
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

from core.models_orchestration import Orchestration, StepConfig, StepType

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


class Unreadable(ValueError):
    """A stored workflow the engine does not read, said as what to mend and where (F1235). Its words are the
    refusal: a route answers 422 with them, and a run that stops on it keeps them as its reason (`reason_of`)."""


def load_to_run(doc: dict[str, Any]) -> Orchestration:
    """The engine's own strict read of a stored workflow, for a run; `Unreadable` in words when it does not read."""
    try:
        return Orchestration.model_validate(doc)
    except ValidationError as e:
        raise Unreadable(unreadable_words(doc, e)) from e


def unreadable_words(doc: Any, exc: ValidationError) -> str:
    """Why a workflow does not read, a sentence per step: the step by its name and id, never `steps[0]`."""
    steps = doc.get("steps") if isinstance(doc, dict) else None
    by_step: dict[str, list[str]] = {}
    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        if len(loc) >= 2 and loc[0] == "steps" and isinstance(loc[1], int):
            step = steps[loc[1]] if isinstance(steps, list) and loc[1] < len(steps) else None
            by_step.setdefault(_named(step, loc[1]), []).append(shape_problem({**err, "loc": loc[2:]}))
        else:
            by_step.setdefault("", []).append(shape_problem(err))
    said = [f"{f'Step {who}' if who else 'The workflow'}: {'; '.join(dict.fromkeys(p))}." for who, p in by_step.items()]
    return ("This workflow can't run: it was saved in a shape the engine does not read. " + " ".join(said)
            + " Open it in the editor and save it, or ask Synapse AI to change it.")


def _named(step: Any, index: int) -> str:
    if not isinstance(step, dict):
        return f"{index + 1}"
    name, sid = step.get("name"), step.get("id")
    if isinstance(name, str) and name and isinstance(sid, str) and sid and name != sid:
        return f'"{name}" ({sid})'
    return f"'{sid}'" if isinstance(sid, str) and sid else f"{index + 1}"


def shape_problem(err: dict) -> str:
    """One pydantic error as the field, the shape it needs and what it was given (F1047's words: the builder's save
    refusal and `Unreadable` both say them)."""

    loc = err.get("loc") or ()
    if not loc:  # a rule across fields: its own sentence
        return str(err.get("msg", "")).removeprefix("Value error, ")
    field = str(loc[0]) + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc[1:])
    kind = err.get("type", "")
    if kind == "missing":
        return f"`{field}` is required"
    if kind == "enum" and loc == ("type",):
        return f"`type` must be one of {', '.join(t.value for t in StepType)}, not {err.get('input')!r}"
    need = _NEEDS.get(kind)
    if need is None:
        return f"`{field}`: {err.get('msg', 'not readable')}"
    if field == "human_fields":
        need = 'a list of objects, e.g. [{"name": "email", "type": "string"}]'
    return f"`{field}` must be {need}, not {_given(err.get('input'))}"


_NEEDS = {
    "list_type": "a list", "dict_type": "an object", "string_type": "a string",
    "int_type": "a whole number", "int_parsing": "a whole number", "int_from_float": "a whole number",
    "float_type": "a number", "float_parsing": "a number", "bool_type": "true or false", "bool_parsing": "true or false",
}


def _given(value: Any) -> str:
    if isinstance(value, bool):
        return "true/false"
    return {dict: "an object", list: "a list", str: "a string", int: "a number", float: "a number",
            type(None): "null"}.get(type(value), type(value).__name__)
