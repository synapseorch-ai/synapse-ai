"""A Plan as the plan card's text (SAI2-P1, F775): what a person reads before they say Build it.

The card renders from the Plan object in P3; this text is what every other reader gets — channels, v1, MCP, an old
client — and what the panel's `plan.ts` parser still reads for a release (paused runs finish on their own definition,
F443). So its sections are the card's, under the headings that parser looks for: Overview, Steps, Agents, Schedule,
Asks you first, Acts without asking, Missing here, Not carried over, Open questions. **A section with nothing to say
is left out** — never "(cron expression: )", never "None".
"""
from __future__ import annotations

from core.native_builder.objects import Gate, Plan, Step

_TYPE_WORDS = {"agent": "agent", "llm": "llm", "tool": "tool", "evaluator": "evaluator", "parallel": "parallel",
               "merge": "merge", "loop": "loop", "human": "human", "transform": "transform",
               "extract_json": "extract_json", "if_else": "if_else", "switch": "switch", "print": "print",
               "end": "end", "retrieve": "retrieve", "ingest": "ingest", "workflow": "workflow"}


def _step_line(n: int, step: Step, agent_names: dict[str, str], inner: set[str]) -> str:
    line = f"{n}. **{step.name}** ({_TYPE_WORDS.get(step.type, step.type)})"
    if step.does and step.does.strip() and step.does.strip() != step.name.strip():
        line += f": {step.does.strip()}"
    extras = []
    if step.tool:
        extras.append(f"uses `{step.tool}`")
    if step.agent:
        extras.append(f"agent: {agent_names.get(step.agent, step.agent)}")
    if step.workflow:
        extras.append(f"runs the workflow {step.workflow}" + (" (made with this one)" if step.workflow in inner else ""))
    if extras:
        line += f" — {'; '.join(extras)}"
    return line


def _bullets(items: list[str], *, code: bool = False) -> str:
    return "\n".join(f"- `{i}`" if code else f"- {i}" for i in items)


def zone_words(timezone: str | None) -> str:
    """A schedule's clock in the card's words (F948): "New York time"; UTC for none. `workflowList.ts` `zoneWords`
    says the same."""
    tz = (timezone or "").strip()
    if not tz or tz.upper() == "UTC":
        return "UTC"
    return tz.split("/")[-1].replace("_", " ") + " time"


_SET_BY = {"build": "", "agent": " (its own tool policy)", "workspace": " (this workspace's tool policy)"}


def gate_words(gate: Gate) -> str:
    """One approval or block as the card says it (F964): "Vault Clerk asks you before `vault_write`, each time (its own
    tool policy)"; `plan.ts` `gateWords` says the same."""
    who = f'The step "{gate.who}"' if gate.kind == "step" else gate.who
    if gate.decision == "deny":
        whose = "its own tool policy" if gate.set_by == "agent" else "this workspace's tool policy"
        return f"{who} can't use `{gate.tool}`: {whose} blocks it"
    return f"{who} asks you before `{gate.tool}`, each time{_SET_BY[gate.set_by]}"


def render(plan: Plan, *, agent_names: dict[str, str] | None = None) -> str:
    """The plan card's text. `agent_names` names the workspace's agents a plan reuses (by id)."""
    names = dict(agent_names or {})
    inner = {w.title for w in plan.inner_workflows}
    parts: list[str] = []

    overview = (plan.summary or "").strip()
    if overview:
        parts.append(f"## Overview\n{overview}")

    if plan.steps or plan.edits:
        lines = [_step_line(i, s, names, inner) for i, s in enumerate((s for s in plan.steps if s.type != "end"), 1)]
        for e in plan.edits:
            what = {"add_step": f"Add **{e.step.name if e.step else '?'}** after {e.after}",
                    "update_step": f"Change **{e.step_id}**", "remove_step": f"Remove **{e.step_id}**",
                    "set_meta": "Change the workflow's details"}[e.op]
            lines.append(f"{len(lines) + 1}. {what}")
        parts.append("## Steps\n" + "\n".join(lines))
    for w in plan.inner_workflows:
        steps = [_step_line(i, s, names, inner) for i, s in enumerate((s for s in w.steps if s.type != "end"), 1)]
        parts.append(f"## Inner workflow: {w.title}\n{w.does.strip()}\n" + "\n".join(steps))

    agent_lines = []
    if plan.agents:
        agent_lines.append("### New agents to create")
        for a in plan.agents:
            line = f"- **{a.name}**: {a.purpose.strip()}"
            if a.type == "team" and a.members:
                line += f" (a team: {', '.join(names.get(m, m) for m in a.members)})"
            if a.tools:
                line += f" — tools: {', '.join(f'`{t}`' for t in a.tools)}"
            if picked := getattr(plan, "models", {}).get(a.name):
                line += f" — model: {picked.name} ({picked.reason}{f'; {a.why.strip()}' if a.why else ''})"
            agent_lines.append(line)
    if plan.reuse_agents:
        agent_lines.append("### Existing agents it uses")
        agent_lines += [f"- {names.get(a, a)}" for a in plan.reuse_agents]
    if agent_lines:
        parts.append("## Agents\n" + "\n".join(agent_lines))

    if plan.schedule and (plan.schedule.says or "").strip():
        when = plan.schedule.says.strip()
        detail = ", ".join(x for x in (f"`{plan.schedule.cron.strip()}`" if plan.schedule.cron.strip() else "",
                                       zone_words(plan.schedule.timezone) if plan.schedule.timezone else "") if x)
        text = f"**When:** {when}" + (f" ({detail})" if detail else "")
        if (plan.schedule.input or "").strip():
            text += f"\n**Each run starts with:** {plan.schedule.input.strip()}"
        text += "\nIt is made paused: turn it on under Schedules once you have checked what each run starts with."
        parts.append(f"## Schedule\n{text}")

    caps = [f"{plan.caps.credits:g} credits" if plan.caps and plan.caps.credits is not None else "",
            f"${plan.caps.dollars:g} of models" if plan.caps and plan.caps.dollars is not None else ""]
    if any(caps):
        parts.append("## Limits\nEach run is capped at " + " and ".join(c for c in caps if c) + ".")

    code = [(s.name, (s.config or {}).get("transform_code")) for s in plan.steps if s.type == "transform"]
    if any(c for _, c in code):
        parts.append("## Code it runs\n" + "\n\n".join(f"**{name}**\n```python\n{c.strip()}\n```"
                                                       for name, c in code if c))

    confirms = [g for g in plan.gates if g.decision == "confirm"]
    asks = [gate_words(g) for g in confirms]
    asks += [f"`{t}`" for t in plan.approvals.asks_first if t not in {g.tool for g in confirms}]
    if asks:
        text = "## Asks you first\n" + _bullets(asks)
        if confirms and any(s.type == "human" for s in [*plan.steps, *(x for w in plan.inner_workflows
                                                                         for x in w.steps)]):
            text += "\n\nThese are asked as well as the Human steps above."
        parts.append(text)
    if denies := [g for g in plan.gates if g.decision == "deny"]:
        parts.append("## Blocked here\n" + _bullets([gate_words(g) for g in denies]))
    if plan.approvals.acts_without_asking:
        parts.append("## Acts without asking\n" + _bullets(plan.approvals.acts_without_asking, code=True))

    missing = [f"`{n}`: nothing here provides it yet" for n in plan.missing]
    missing += [f"{c}: connect it first" for c in plan.connections_needed]
    if missing:
        parts.append("## Missing here\n" + _bullets(missing))
    if plan.room:
        parts.append(f"## Your plan\n{plan.room}")

    if plan.not_carried_over:
        parts.append("## Not carried over\n" + _bullets(plan.not_carried_over))
    if plan.questions:
        parts.append("## Open questions\n" + _bullets(plan.questions))

    title = (plan.title or "").strip()
    head = f"# {title}\n\n" if title else ""
    return head + "\n\n".join(parts) + "\n"
