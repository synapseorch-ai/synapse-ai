"""How a chart the person asks for lands, and whether a plan has its route (F1288, CAP3's pass 2; 79 11-02).

Brief 4 asked for sales by region as a chart. The plan worked the totals out right, but nothing in it could save a
file, and its agent said it could not save the chart. A chart here is a CSV in the Vault, which the Vault draws (its
Chart view, F1204: bar, line or one number, from a label column and a value column); a board card's note links it as
`@[path]`. The plan writes that CSV with `vault_write`, which it has only by naming it.

* `asked(message)`: whether the person's words ask for a chart, by code, as `features` finds a feature.
* `HOW`: what the planner's pack says about it when they do.
* `issues(plan, ws, message)`: a plan for a chart with nothing in it that writes the Vault is sent back; with no
  `vault_write` in this workspace at all, the planner is told to say it cannot.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.native_builder import objects as O

if TYPE_CHECKING:
    from core.native_builder.build_plan import Workspace

#: The words that ask for one, each folded to its singular (`features._norm`).
WORDS = ("chart", "graph", "plot", "histogram", "visualisation", "visualization", "visualise", "visualize")
#: The Vault's write: creates the file or writes over it, so each run's chart replaces the last.
WRITE = "vault_write"

HOW = ("A chart is a CSV file in the Vault, which the Vault draws as a chart (bar, line or one number, from a label "
       "column and a value column). The plan writes it with `vault_write` (path and content) at a path like "
       "reports/<what it shows>.csv, over the last run's: a Tool step on `vault_write` whose content is the CSV text a "
       "step before it made (a transform's), or the agent that works out the numbers, with `vault_write` in its "
       "tools. On a board, its card's note names the file as @[reports/<what it shows>.csv], which opens it in the "
       "Vault.")


def asked(message: str) -> bool:
    """Whether `message` asks for a chart: one of `WORDS` among its words."""
    from core.native_builder.features import _norm

    # A knowledge graph is not a chart.
    said = set(_norm(message).replace("knowledge graph", " ").split())
    return any(w in said for w in WORDS)


def _writes(plan: O.Plan, ws: Workspace) -> bool:
    """Whether anything in the plan can write the Vault: a Tool step on it, a new agent given it, or an agent here
    the plan uses that has it."""
    def named(tools) -> bool:
        return any(str(t).split("__")[-1] == WRITE for t in tools or [])

    steps = [*plan.steps, *(s for w in plan.inner_workflows for s in w.steps)]
    if any(s.type == "tool" and named([s.tool]) for s in steps):
        return True
    if any(named(a.tools) for a in plan.agents):
        return True
    used = set(plan.reuse_agents) | {s.agent for s in steps if s.agent}
    return any(named((ws.agents.get(ref) or {}).get("tools")) for ref in used)


def issues(plan: O.Plan, ws: Workspace, message: str) -> list[str]:
    """What the plan for a chart lacks, where code can tell; [] when it has its route or no chart was asked for."""
    if not asked(message) or _writes(plan, ws):
        return []
    if not any(str(t.get("name") or "").split("__")[-1] == WRITE for t in ws.tools):
        return ["chart: the person asked for a chart, which is a CSV in the Vault, and this workspace has no "
                f"`{WRITE}` (the Vault's tools are off here): finish with `cannot`, saying a chart needs them"]
    return [f"chart: the person asked for a chart, and nothing in this plan writes the Vault: {HOW}"]
