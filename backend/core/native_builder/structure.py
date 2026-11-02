"""The structure a plan should have, where code can tell (D340 D3, BLD1; TRI1's spec §4).

Judgement only where it is needed: a fetch is a Tool step, data goes through `{{ state.x }}`, and an agent is kept
for what needs a model. Most of that is the planner's to weigh (its prompt's "# Structure": helpers, a team, a Browser
agent, a transform for reshaping, one known call as a Tool step). Code holds only what it can tell from the plan alone,
never from its words:

* **A fetch is a Tool step**: a new agent the plan makes whose one tool reads a page or an API (a built-in page read,
  or a read-only GET HTTP tool here) does nothing a model is needed for; the read is a Tool step whose output the next
  step reads. An agent already here is the person's pick, and left alone (TRI1).
* **Agents asked for as a team are a team** (F1284, prod-06): two or more new agents, no team holding them, one's step
  reading another's through `{{ state.x }}`, in a build the quick classify itself gave the reason `team` (its closed
  list, `request.why`, as the effort offer reads it): one agent of type team, its lead, with them as its members. A
  pipeline of agents not asked for as a team is a plan like any other.

A finding is sent back once, then said on the card: never a reason a plan cannot be built.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from core.native_builder import objects as O

if TYPE_CHECKING:
    from core.native_builder.build_plan import Workspace

#: The built-in page reads a workflow's agent can call (the scraper's, `build_plan._BUILT_IN_SCRAPE`).
PAGE_READS = ("scrape_url", "scrape_structured", "search_page", "extract_links")
#: A step's read of another's answer, `{{ state.<step id> }}`.
_STATE_READ = re.compile(r"\{\{\s*state\.([A-Za-z_][A-Za-z0-9_]*)")


def _reads(name: str, custom: dict[str, dict]) -> bool:
    """Whether `name` only reads: a built-in page read, or an HTTP tool here that GETs and declares it only reads."""
    if name in PAGE_READS:
        return True
    tool = custom.get(name)
    if not tool or (tool.get("tool_type") or "http") != "http" or str(tool.get("method") or "").upper() != "GET":
        return False
    hints = tool.get("annotations") or {}
    return bool(hints.get("readOnlyHint") or tool.get("read_only"))


async def issues(plan: O.Plan, ws: Workspace, asked: dict | None = None) -> list[str]:
    """What the plan's structure should be instead, where code can tell; [] when it holds. `asked` is the quick
    classify's request, as the run holds it."""
    from core.store.resources import load_tools

    if not plan.agents:
        return []
    custom = {t.get("name"): t for t in await load_tools()}
    out = []
    steps = [s for s in (*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)) if s.type != "end"]
    for a in plan.agents:
        if a.type != "agent" or len(a.tools) != 1 or a.memory or a.knowledge or a.browser or a.servers:
            continue
        if not _reads(a.tools[0], custom):
            continue
        using = [s for s in steps if s.type == "agent" and s.agent == a.name]
        if len(using) != 1:
            continue
        out.append(f"structure: step {using[0].id}: \"{a.name}\" only reads with `{a.tools[0]}`, which needs no model: "
                   f"make it a Tool step calling `{a.tools[0]}`, and have the next step read its output as "
                   f"{{{{ state.{using[0].id} }}}}")
    if "team" in ((asked or {}).get("why") or []) and (team := _a_team(plan, ws, steps)):
        out.append(team)
    return out


def _a_team(plan: O.Plan, ws: Workspace, steps: list[O.Step]) -> str | None:
    """The finding for new agents that hand work to each other through state with no team holding them (F1284)."""
    teams = {k for i, x in ws.agents.items() if x.get("type") == "delegate" for k in (i, x.get("name"))}
    if any(a.type == "team" for a in plan.agents) or any(s.agent in teams for s in steps if s.agent):
        return None
    plain = {a.name for a in plan.agents if a.type == "agent"}
    if len(plain) < 2:
        return None
    by = {s.id: s.agent for s in steps if s.type == "agent" and s.agent in plain}
    for s in steps:
        if s.id not in by:
            continue
        for read in _STATE_READ.findall(s.prompt or ""):
            if by.get(read) not in (None, by[s.id]):
                names = " and ".join(f'"{n}"' for n in (by[read], by[s.id]))
                return (f"structure: agents {names} hand their work to each other through state (step {s.id} reads "
                        f"{{{{ state.{read} }}}}): these agents work as a team: make an agent of type team with a lead "
                        "and its members (type team, members: the agents it hands to), and one agent step for it in "
                        "place of theirs")
    return None


def as_notes(found: list[str]) -> list[str]:
    """Structure findings as the card says them, once the planner has had them back."""
    return [f.removeprefix("structure: ") for f in found]


__all__: list[Any] = ["issues", "as_notes", "PAGE_READS"]
