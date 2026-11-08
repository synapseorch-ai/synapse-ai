"""What one run of a plan would cost in model calls, said on its card (D340 D3, BLD1; TRI1's spec §3.7, Naveen's
"quality and cost efficient").

From the plan alone and the model menu's own prices (`models_menu.menu`, the source the effort check reads): each step
a model answers (an agent's turns, an llm or evaluator step's one call) at its model's price for a typical turn (a model
that reasons thinks too, billed as output), and
nothing for a Tool, transform or code step. An agent with tools takes a few turns; a team's members each take theirs;
a loop's body counts once per item (its `loop_max_iterations` when it has one, else said per item); a plan's eval adds
its judge, on its sample. A rough figure, said as one: a note on the card, never a reason a plan cannot be built.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.native_builder import objects as O

if TYPE_CHECKING:
    from core.native_builder.build_plan import Workspace

#: A typical model turn, in tokens: what a step's prompt, its context and a tool result or two add up to, and a reply.
TURN_IN, TURN_OUT = 4_000, 600
#: What a model that reasons thinks on a typical turn, billed as output (an estimate counts thinking, Naveen's rule):
#: for a model the menu marks `reasoning`.
THINKING_OUT = 2_000
#: Turns an agent with tools takes on a step: read, call, answer.
TOOL_TURNS = 3


@dataclass
class Estimate:
    #: Model dollars for one run, its loops at their caps; None when nothing on it is priced.
    dollars: float | None = None
    #: What each further item of an uncapped loop adds.
    per_item: float = 0.0
    #: Models it uses that the menu has no price for.
    unpriced: list[str] = field(default_factory=list)

    def words(self) -> str:
        if self.dollars is None and not self.per_item:
            return ""
        said = f"About {_money(self.dollars or 0)} a run in model calls"
        if self.per_item:
            said += f", and {_money(self.per_item)} more for each item its loop goes through"
        said += "; Tool, transform and code steps call no model"
        if self.unpriced:
            said += f" (not counted, no price here: {', '.join(sorted(set(self.unpriced)))})"
        return said + "."


def _money(dollars: float) -> str:
    return f"${dollars:.4f}" if dollars < 0.01 else f"${dollars:.2f}"


def _turn(entry: Any) -> float | None:
    if entry is None or entry.price_in is None or entry.price_out is None:
        return None
    out = TURN_OUT + (THINKING_OUT if getattr(entry, "reasoning", False) else 0)
    return (entry.price_in * TURN_IN + entry.price_out * out) / 1_000_000


async def of(plan: O.Plan, ws: Workspace) -> Estimate:
    """One run's model cost, step by step (see the module)."""
    from core.native_builder import models_menu as MM

    menu = {e.id: e for e in await MM.menu()}
    default = MM.workspace_model()
    new = {a.name: a for a in plan.agents}
    out = Estimate()

    def model_of_agent(ref: str) -> str:
        if ref in new:
            picked = plan.models.get(ref)
            return picked.model if picked is not None else default
        return str((ws.agents.get(ref) or {}).get("model") or default)

    def agent_turns(ref: str) -> list[tuple[str, int]]:
        """(model, turns) for an agent's step: its own, and each team member's."""
        if ref in new:
            a = new[ref]
            acts = bool(a.tools or a.servers or a.knowledge or a.browser) or a.type == "team"
            calls = [(model_of_agent(ref), TOOL_TURNS if acts else 1)]
            if a.type == "team":
                calls += [c for m in a.members for c in agent_turns(m)]
            return calls
        here = ws.agents.get(ref) or {}
        calls = [(model_of_agent(ref), TOOL_TURNS if here.get("tools") else 1)]
        for m in here.get("delegate_agent_ids") or []:
            calls += agent_turns(m)
        return calls

    def calls_of(s: O.Step) -> list[tuple[str, int]]:
        if s.type == "agent" and s.agent:
            return agent_turns(s.agent)
        if s.type in ("llm", "evaluator"):
            return [(str((s.config or {}).get("model") or default), 1)]
        return []

    def cost(calls: list[tuple[str, int]]) -> float:
        total = 0.0
        for model, turns in calls:
            each = _turn(menu.get(model))
            if each is None:
                out.unpriced.append(model)
                continue
            total += each * turns
        return total

    inner = {w.title: w for w in plan.inner_workflows}
    steps = {s.id: s for s in plan.steps}
    in_body = {i: s for s in plan.steps if s.type == "loop" for i in (s.body or [])}
    run_cost, per_item = 0.0, 0.0
    for s in plan.steps:
        if s.type == "end" or s.id in in_body:
            continue
        if s.type == "workflow" and s.workflow in inner:
            run_cost += sum(cost(calls_of(x)) for x in inner[s.workflow].steps)
            continue
        if s.type == "loop":
            body = sum(cost(calls_of(steps[i])) for i in (s.body or []) if i in steps)
            cap = (s.config or {}).get("loop_max_iterations")
            if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                run_cost += body * cap
            else:
                per_item += body
            continue
        run_cost += cost(calls_of(s))
    if plan.eval is not None and plan.eval.judge:
        judged = _turn(menu.get(plan.eval.judge))
        if judged is not None:
            run_cost += judged / max(1, plan.eval.judge_every)
    priced = any(_turn(menu.get(m)) is not None for s in plan.steps for m, _ in calls_of(s)) or run_cost > 0
    out.dollars = run_cost if priced else None
    out.per_item = per_item
    return out
