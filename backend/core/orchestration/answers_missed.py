"""What a Tool step's answer left out of what its tool declares, kept for the steps after it (F1620).

A Tool step whose answer lacks a field its tool declares says so as a warning and keeps the answer as given
(`steps._answer_lacks`). A later step that reads what is missing fails where it reads it, and a condition reading it
takes its unwritten answer (F1433); neither said why. So the step's answer key is recorded here with the paths it
lacked (paths only, never a value) under a private key the run's views never show (`MISSED_KEY`), and dropped when
that step's next answer holds. A step that fails on a lookup, or a condition's warning about a value not written, adds
one bounded clause (`clause`) naming the Tool step, its tool and what it left out, when that step reads the key there.
"""
from __future__ import annotations

import re
from typing import Any

#: The run's record, by the Tool step's answer key: {"step", "name", "tool", "paths", "count"}.
MISSED_KEY = "_answers_missed"

#: The most paths a record keeps, and the most a clause names.
_KEPT = 20
_NAMED = 5

_LOOKUPS = (KeyError, IndexError, AttributeError, TypeError)
_LOOKUP_WORDS = re.compile(r"\b(?:KeyError|IndexError|AttributeError|TypeError)\b")


def paths_of(problems: list[str]) -> list[str]:
    """The paths `output_fields.held`'s problems name (`leads[1].email`), in order: never the values they quote."""
    return [p[1:p.index("`", 1)] for p in problems if p.startswith("`") and "`" in p[1:]]


def note(state: dict, key: str | None, step_id: str, step_name: str, tool: str | None,
         problems: list[str] | None) -> None:
    """Records what the Tool step's answer at `key` lacked, or drops the record when it lacked nothing."""
    if not key:
        return
    missed = state.get(MISSED_KEY)
    if not problems:
        if isinstance(missed, dict) and key in missed:
            missed.pop(key)
            if not missed:
                state.pop(MISSED_KEY, None)
        return
    paths = paths_of(problems)
    if not isinstance(missed, dict):
        missed = state[MISSED_KEY] = {}
    missed[key] = {"step": step_id, "name": step_name, "tool": tool, "paths": paths[:_KEPT], "count": len(paths)}


def is_lookup(exc: BaseException) -> bool:
    """Whether a step's failure is a lookup: a key, an index, an attribute or a type a value does not have, raised or
    named by what the step's code raised (a Transform's failure carries its code's error as text)."""
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, _LOOKUPS) or _LOOKUP_WORDS.search(str(cause)):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _first(path: str) -> str:
    return re.split(r"[.\[]", path, maxsplit=1)[0]


def clause(step_id: str, steps: list[Any], state: dict) -> str | None:
    """One line naming each Tool step whose answer left out what step `step_id` reads, its tool and what it left out
    (five paths at most, then how many more), or None. A read counts when it is of a field the answer lacked at its
    first level, or of any field of an answer that was no object."""
    missed = state.get(MISSED_KEY)
    if not isinstance(missed, dict) or not missed:
        return None
    from core.builder_tools import answer_reads

    saved = [s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in steps or []]
    reads = answer_reads([s for s in saved if isinstance(s, dict)], set(missed))
    said: list[str] = []
    named = 0
    for key, record in missed.items():
        firsts = {_first(p) for p in record.get("paths") or []}
        if not any(sid == step_id and parts and (not firsts or parts[0] in firsts)
                   for sid, parts, _, _ in reads.get(key) or []):
            continue
        whose = f"the answer of step '{record.get('name') or record.get('step')}' (`{record.get('tool')}`)"
        paths = list(record.get("paths") or [])
        if not paths:
            said.append(f"{whose} was no object, where its tool declares one (warned at that step)")
            continue
        count = int(record.get("count") or len(paths))
        shown = paths[:max(_NAMED - named, 0)]
        named += len(shown)
        more = count - len(shown)
        listed = (", ".join(f"`{p}`" for p in shown) + (f" and {more} more" if more > 0 else "")
                  if shown else f"{count} of its fields")
        said.append(f"{whose} did not hold what its tool declares at {listed} (warned at that step)")
    return "; ".join(said) or None
