"""A plan waiting for its review takes only its own answers; anything else typed is a new request (F759, D291).

ASSIST1's dev pass (acme, 2026-10-17): with the change route's review waiting, three unrelated requests typed in the
panel were each sent as the review's answer; F754's gate redrafted on each and the planner folded them into the plan,
and the question was never answered. Naveen: while a plan waits, a typed message is a new request — the plan is set
aside — except an explicit yes, which the review's own sentence asks for (BWA1), and except while the person is
changing the plan (its card's Change something). A clarifying question is still answered by typing.

The panels decide from the synced reducer (`frontend/src/lib/builderEvents.ts`): `PLAN_REVIEW_STEPS` and
`APPROVAL_WORDS`. Two copies drift, so these read both from the definition: the review steps are the human steps that
feed a gate deciding approval in code (F754), and the yes words are exactly what those gates take. A new review step
or a new yes word fails here until the panel has it. (OSS has no unit runner; the functions are tested in cloud.)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from core.native_builder import bundled_definitions

REDUCER = Path(__file__).resolve().parents[3] / "frontend/src/lib/builderEvents.ts"


def _ts_list(name: str) -> list[str]:
    found = re.search(rf"export const {name}\b[^=]*=\s*\[(.*?)\];", REDUCER.read_text(), re.S)
    assert found, f"{name} is not in builderEvents.ts"
    return re.findall(r"'([^']*)'", found.group(1))


def _gates() -> dict[str, tuple[str, ...]]:
    """Each gate that decides approval in code (F754), by the human step whose answer it reads, with the words it
    takes. A gate reads `state.<key>.feedback`; the review is the human step that writes `<key>`."""
    steps = bundled_definitions()["orchestration"]["steps"]
    writes = {s.get("output_key"): s["id"] for s in steps if s["type"] == "human"}
    gates: dict[str, tuple[str, ...]] = {}
    for gate in steps:
        read = re.search(r"state\.(\w+)\.feedback", gate.get("if_condition") or "")
        if gate["type"] != "if_else" or not read or read.group(1) not in writes:
            continue
        compare = ast.parse(gate["if_condition"], mode="eval").body
        # An approval gate takes any of a list of yes words; a gate on one phrase (the research card's
        # "find who can", read off the same answer) asks something else.
        if not (isinstance(compare, ast.Compare) and isinstance(compare.ops[0], ast.In)
                and isinstance(compare.comparators[0], ast.Tuple)):
            continue
        gates[writes[read.group(1)]] = tuple(c.value for c in compare.comparators[0].elts)
    return gates


def test_the_panel_knows_every_plan_review():
    gates = _gates()
    assert {"step_human_review", "step_manage_review"} <= set(gates), gates
    assert sorted(_ts_list("PLAN_REVIEW_STEPS")) == sorted(gates)


def test_a_typed_yes_is_exactly_what_the_gates_take():
    words = {frozenset(w) for w in _gates().values()}
    assert len(words) == 1, "the two gates take different words"
    assert sorted(_ts_list("APPROVAL_WORDS")) == sorted(next(iter(words)))


def test_the_panel_normalises_a_typed_yes_as_the_gates_do():
    """The gates read `str(…).strip().lower().strip(" .!")`; the panel trims, lowers and drops the same ends."""
    gate = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_manage_route")
    assert '.strip().lower().strip(" .!")' in gate["if_condition"]
    source = REDUCER.read_text()
    body = source[source.index("export function isApproval"):].split("\n}\n", 1)[0]
    assert ".trim().toLowerCase()" in body and "[ .!]" in body
