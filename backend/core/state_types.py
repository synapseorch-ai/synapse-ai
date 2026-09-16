"""One vocabulary for `state_schema` types.

An orchestration's `state_schema` maps a key to `{type, default, description}`.
Until now `type` had two spellings and no owner: `builder_tools` told the model
"Allowed types: str, int, float, bool, list, dict", while the editor's dropdown
offered `string, number, boolean, list, dict`. Radix renders a *blank* trigger —
not its placeholder — for a value matching no option, so every AI-built `str`
key showed an empty type box next to a `dict` key that looked fine, and nothing
anywhere reconciled the two lists.

The backend's spelling wins. It is what the builder already emits and what four
prompt strings already teach, so nothing model-facing has to change; and `int`
vs `float` is a distinction `number` cannot express but the engine's coercion
needs. The editor's spelling is read as an alias, so no stored orchestration
needs migrating — `Orchestration`'s validator canonicalises each one the next
time it is saved.

`frontend/src/components/orchestration/stateTypes.ts` is this table in
TypeScript. Change both together.
"""
from __future__ import annotations

from typing import Any

#: The only spellings `type` is ever stored as.
CANONICAL_STATE_TYPES: tuple[str, ...] = ("str", "int", "float", "bool", "list", "dict")

#: Everything else that has meant one of the above. `number` widens to `float`:
#: the old dropdown's number input accepted 7.5, so reading it as `int` would
#: start rejecting values the editor used to take. Widening never does.
STATE_TYPE_ALIASES: dict[str, str] = {
    "string": "str",
    "text": "str",
    "integer": "int",
    "number": "float",
    "double": "float",
    "decimal": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "json": "dict",
    "map": "dict",
}


def normalize_state_type(raw: Any) -> str | None:
    """The canonical spelling of `raw`, or None if it is not a type we know.

    None rather than a default on purpose: a key whose type cannot be read is a
    mistake someone has to see, and the engine must never guess at what a value
    was meant to be. `_validate_orchestration` is what tells the builder.
    """
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in CANONICAL_STATE_TYPES:
        return key
    return STATE_TYPE_ALIASES.get(key)


def matches_declared_type(declared: Any, value: Any) -> bool:
    """Is `value` already of the declared type?

    An unrecognised `declared` contradicts nothing, so it matches: nothing can
    be claimed about a type we cannot read.

    `bool` is excluded from `int` and `float` by hand. Python says
    `isinstance(True, int)`, which would otherwise let `true` quietly satisfy a
    key declared `int` — and, worse, let a `1` satisfy one declared `bool`.
    """
    canonical = normalize_state_type(declared)
    if canonical is None:
        return True
    if canonical == "str":
        return isinstance(value, str)
    if canonical == "bool":
        return isinstance(value, bool)
    if canonical == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if canonical == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if canonical == "list":
        return isinstance(value, list)
    return isinstance(value, dict)
