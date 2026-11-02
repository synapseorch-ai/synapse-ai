"""The structure a plan should have, where code can tell (D340 D3, BLD1; TRI1's spec §4).

Judgement only where it is needed: a fetch is a Tool step, data goes through `{{ state.x }}`, and an agent is kept
for what needs a model. Most of that is the planner's to weigh (its prompt's "# Structure": helpers, a team, a Browser
agent, a transform for reshaping, one known call as a Tool step). Code holds only what it can tell from the plan alone,
never from its words:

* **A fetch is a Tool step**: a new agent the plan makes whose one tool reads a page or an API (a built-in page read,
  or a read-only GET HTTP tool here) does nothing a model is needed for; the read is a Tool step whose output the next
  step reads. An agent already here is the person's pick, and left alone (TRI1).
* **Code's work is a Transform** (F1291, CAP3's cap-04): a new agent holding `execute_python`, where code steps run,
  does with a model what a `transform` step (or a Python tool) does exactly. Where code steps do not run, it is the
  only way, and left alone.

A finding is sent back once, then said on the card: never a reason a plan cannot be built.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.native_builder import objects as O

if TYPE_CHECKING:
    from core.native_builder.build_plan import Workspace

#: The sandbox's code tool an agent runs Python with: a new agent holding it, where code steps run, is a Transform's
#: work done by a model (F1291).
CODE_TOOL = "execute_python"
#: The built-in page reads a workflow's agent can call (the scraper's, `build_plan._BUILT_IN_SCRAPE`).
PAGE_READS = ("scrape_url", "scrape_structured", "search_page", "extract_links")


def _reads(name: str, custom: dict[str, dict]) -> bool:
    """Whether `name` only reads: a built-in page read, or an HTTP tool here that GETs and declares it only reads."""
    if name in PAGE_READS:
        return True
    tool = custom.get(name)
    if not tool or (tool.get("tool_type") or "http") != "http" or str(tool.get("method") or "").upper() != "GET":
        return False
    hints = tool.get("annotations") or {}
    return bool(hints.get("readOnlyHint") or tool.get("read_only"))


async def issues(plan: O.Plan, ws: Workspace) -> list[str]:
    """What the plan's structure should be instead, where code can tell; [] when it holds."""
    from core.store.resources import load_tools

    if not plan.agents:
        return []
    custom = {t.get("name"): t for t in await load_tools()}
    out = []
    steps = [s for s in (*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)) if s.type != "end"]
    for a in plan.agents:
        # Code's work is a Transform (F1291, cap-04): a new agent computing with `execute_python`, where code steps run.
        if ws.code_steps and any(str(t).split("__")[-1] == CODE_TOOL for t in a.tools):
            out.append(f"structure: agent \"{a.name}\" computes with `{CODE_TOOL}`: work that is code over the run's "
                       "data (filtering, totals, a chart's CSV) is a `transform` step whose answer the next step reads, "
                       "or a Python tool; keep an agent for what needs judgement")
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
    return out


def as_notes(found: list[str]) -> list[str]:
    """Structure findings as the card says them, once the planner has had them back."""
    return [f.removeprefix("structure: ") for f in found]


__all__: list[Any] = ["issues", "as_notes", "PAGE_READS"]
