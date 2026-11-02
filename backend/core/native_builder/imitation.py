"""When a plan rebuilds one of the product's screens by hand, said from the census (F1257 step 3, GUIDE1; D340 A).

"Create an eval for my curiosity post generator" was planned as a workflow that ran the generator and had an agent
grade it: Evals, made by hand (TRI1's triage part 3). BLD1's F1255 caught that one in code. The census already says
what a stand-in for each screen looks like (`Screen.imitation`, CAP3's rows); here a row gains a code matcher by its id
(`SIGNATURES`), and `findings` reads every row that has one, in the host's census (cloud's is its own), so a
screen and its words live in one place.

A matcher answers from the plan and the workspace only: the row's own sentence, True for words built from the row, or
None. `findings` is pure, so the planner's own read of its draft (`build_plan.check_draft`, D340 D3) carries it;
`build_plan.check_build` says what it finds.
Evals is the only matcher for now (a0, 10-31): the other rows reach the planner as their words in the pack
(`features`), and gain a matcher when a real request shows one.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Callable

from core import feature_census
from core.native_builder import objects as O

if TYPE_CHECKING:
    from core.native_builder.build_plan import Workspace


def _evals(plan: O.Plan, ws: Workspace) -> str | None:
    """Evals made by hand (F1255): a Workflow step running a workflow that is here, a model step reading what it
    wrote, and no tool step or other workflow step. Narrow on purpose: running one and then fetching or sending
    something is a workflow."""
    steps = [s for s in plan.steps if s.type != "end"]
    runs = [s for s in steps if s.type == "workflow"]
    if not runs or any(s.workflow not in ws.workflows for s in runs) or any(s.type == "tool" for s in steps):
        return None
    reads = [s for s in steps if s.type in ("agent", "llm")
             and any(re.search(rf"state\.{re.escape(r.id)}\b", json.dumps(s.model_dump(), default=str)) for r in runs)]
    if not reads:
        return None
    name = ws.workflows[runs[0].workflow].get("name") or runs[0].workflow
    return (f'That is an eval of "{name}": Evals scores its runs by a rubric, from its next run on, and runs its golden '
            f'test cases when you ask. Ask me to add an eval to "{name}" and I will set its rubric and test cases there, '
            "rather than build a workflow that judges it.")


#: A code matcher for a census row, by the row's id.
SIGNATURES: dict[str, Callable[[O.Plan, Any], str | bool | None]] = {
    "evals": _evals,
}


def _said(screen: feature_census.Screen) -> str:
    """A row's words for a plan that stands in for it: what is done from here there, or that it is the screen's."""
    if screen.does:
        return (f"That is {screen.label} made by hand: Synapse AI {screen.does} on {screen.label} ({screen.href}). "
                "Ask for that, rather than build a workflow that stands in for it.")
    return (f"That is {screen.label} made by hand: {screen.cant} is done on {screen.label} ({screen.href}), not from "
            "here yet. Open it there, rather than build a workflow that stands in for it.")


def findings(plan: O.Plan, ws: Workspace) -> list[dict[str, str]]:
    """Each screen this plan rebuilds by hand, in the census's order: {screen, label, href, say}. Pure."""
    found = []
    for screen in feature_census.current():
        match = SIGNATURES.get(screen.id)
        said = match(plan, ws) if match and screen.imitation else None
        if said:
            found.append({"screen": screen.id, "label": screen.label, "href": screen.href,
                          "say": said if isinstance(said, str) else _said(screen)})
    return found
