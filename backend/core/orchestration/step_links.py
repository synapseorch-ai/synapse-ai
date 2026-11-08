"""The step fields that name another step, and what removing a step does to them (F1458, ISS1 — GitHub #376).

The canvas's delete (`removeStepFromGraph`, frontend/src/components/orchestration/graph.ts) clears every field that
named the removed step, moves the entry to the first step left and clears a result_key only that step wrote (F249).
The builder's `remove_step` did none of it: a saved `a -> b` with `b` removed kept `a.next_step_id = "b"`, and its
run ran `a` and failed at "Step 'b' not found". `clear_links_to` is the canvas's delete on the server, field for
field; the reference check every save and run door asks (`save_rules.step_reference_problems`, F1459) reads the same
fields from here.
"""
from __future__ import annotations

from typing import Any

#: A field holding one step id (empty or None = end of the workflow).
ONE = ("next_step_id", "on_exhausted_step_id", "if_true_step_id", "if_false_step_id", "switch_default_step_id")
#: A field mapping a label or a value to a step id (None = end).
MAPS = ("route_map", "switch_cases")
#: A field listing step ids, in order.
LISTS = ("loop_step_ids",)
#: A field listing branches, each a list of step ids.
BRANCHES = ("parallel_branches",)


def clear_links_to(orch: dict[str, Any], removed: str) -> list[str]:
    """Clear, in `orch` (its steps already without `removed`), every field that named `removed`; a sentence for
    each, as the answer says it."""
    said: list[str] = []
    steps = [s for s in orch.get("steps") or [] if isinstance(s, dict)]
    for s in steps:
        sid = s.get("id")
        for field in ONE:
            if s.get(field) == removed:
                s[field] = None
                said.append(f"Step '{sid}' {field} named '{removed}'; cleared, so it ends there.")
        for field in MAPS:
            for key, target in list((s.get(field) or {}).items()):
                if target == removed:
                    s[field][key] = None
                    said.append(f"Step '{sid}' {field}[{key!r}] named '{removed}'; cleared, so it ends there.")
        for field in LISTS:
            if removed in (s.get(field) or []):
                s[field] = [x for x in s[field] if x != removed]
                said.append(f"Step '{sid}' {field} listed '{removed}'; taken out.")
        for field in BRANCHES:
            branches = s.get(field) or []
            if any(removed in branch for branch in branches):
                s[field] = [[x for x in branch if x != removed] for branch in branches]
                said.append(f"Step '{sid}' {field} listed '{removed}'; taken out.")
    if orch.get("entry_step_id") == removed:
        orch["entry_step_id"] = steps[0].get("id") if steps else ""
        said.append(f"The workflow started at '{removed}'; it now starts at '{orch['entry_step_id']}'."
                    if steps else f"The workflow started at '{removed}'; it has no steps left.")
    key = orch.get("result_key")
    if key and not any(s.get("output_key") == key for s in steps):
        # Left, it would point at nothing and quietly fall back to the last step (F249).
        orch["result_key"] = ""
        said.append(f"result_key '{key}' was written only by '{removed}'; cleared.")
    return said



def links_of(step: dict[str, Any]) -> list[tuple[str, str]]:
    """(the field as said, the step id it names) for every step id `step` names, in field order."""
    found: list[tuple[str, str]] = []
    for field in ONE:
        if target := step.get(field):
            found.append((field, target))
    for field in MAPS:
        for key, target in (step.get(field) or {}).items():
            if target:
                found.append((f"{field}[{key!r}]", target))
    for field in LISTS:
        found += [(field, x) for x in step.get(field) or [] if x]
    for field in BRANCHES:
        found += [(field, x) for branch in step.get(field) or [] for x in branch or [] if x]
    return found
