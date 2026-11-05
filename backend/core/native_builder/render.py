"""A Plan as the plan card's text (SAI2-P1, F775): what a person reads before they say Build it.

The card renders from the Plan object in P3; this text is what every other reader gets — channels, v1, MCP, an old
client — and what the panel's `plan.ts` parser still reads for a release (paused runs finish on their own definition,
F443). So its sections are the card's, under the headings that parser looks for: You asked for, Steps, Agents, Schedule,
Asks you first, Acts without asking, Missing here, Not carried over, Open questions. F1228: the planner's summary is
the request ("You asked for", which was "Overview"); what each step does, and what is missing or off, is code's
(`parts_words`). **A section with nothing to say
is left out** — never "(cron expression: )", never "None".
"""
from __future__ import annotations

from typing import Any

from collections.abc import Set as AbstractSet

from core.native_builder.objects import Gate, Plan, Step

_TYPE_WORDS = {"agent": "agent", "llm": "llm", "tool": "tool", "evaluator": "evaluator", "parallel": "parallel",
               "merge": "merge", "loop": "loop", "human": "human", "transform": "transform",
               "extract_json": "extract_json", "if_else": "if_else", "switch": "switch", "print": "print",
               "end": "end", "retrieve": "retrieve", "ingest": "ingest", "workflow": "workflow"}


#: Steps whose `does` is an instruction a model is given: the card quotes it as what the step is asked to do.
_ASKED = {"agent", "llm", "evaluator"}


def _step_line(n: int, step: Step, agent_names: dict[str, str], inner: set[str],
               fills: AbstractSet[str] = frozenset(), words: dict[str, str] | None = None) -> str:
    line = f"{n}. **{step.name}** ({_TYPE_WORDS.get(step.type, step.type)})"
    does = (step.does or "").strip()
    said = (words or {}).get(step.id)
    if said:
        # What the step does is its parts' (F1228); the planner's words are only what a model in it is asked.
        line += f": {said}"
        if step.type in _ASKED and does and does != step.name.strip():
            line += f' — asks it to: "{does}"'
    elif does and does != step.name.strip():
        line += f": {does}"
    extras = []
    if step.tool:
        extras.append(f"uses `{step.tool}`")
    if step.id in fills and not said:
        extras.append("a model fills in its inputs")  # none set on the step (F967)
    if step.agent and not said:
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


def missing_session_line(site: str) -> str:
    """A browser agent's site with no kept session yet (F1223): the card's line, and the build's own words for it
    (F1228's missing or off)."""
    return (f"No kept session for {site} yet: sign in to it under Agents › Browser › Sessions, then pick it in this "
            "agent's Browser settings. It is built without it until then")


def knowledge_part_line(part: Any) -> str:
    """A knowledge base the plan makes or uses (F1224), as the card and the build's parts both say it."""
    from core.product_tools.knowledge import _folder_for

    added = [f for f in part.files if f not in part.files_missing]
    line = (f"“{part.name}”: already here, used as it is" if part.existing
            else f"“{part.name}”: made with it, in the Vault folder {_folder_for(part.name)}")
    line += "; it is given " + ", ".join(f"`{f}`" for f in added) if added else ""
    return line + (f"; {pages_words(part)}" if getattr(part, "urls", None) else "")


def pages_words(part: Any) -> str:
    """What a knowledge base reads from the web (D340 F): the pages, or a sitemap's or a site's up to its cap, read
    when the build is saved, with its embedding model."""
    urls = list(part.urls)
    cap = f", up to {part.pages_cap} pages" if part.pages_cap else ""
    what = {"page": "it reads " + ", ".join(urls[:10]) + (f" and {len(urls) - 10} more" if len(urls) > 10 else ""),
            "sitemap": f"it reads the pages {urls[0]} lists{cap}",
            "site": f"it crawls {urls[0]}{cap}"}[part.kind]
    return f"{what}, when it is made, with its embedding model"


def missing_file_line(base: str, file: str) -> str:
    """A file a knowledge base was to be given that is not in the Vault (F1224): the card's line and the build's."""
    return (f"`{file}` is not in the Vault: add it there, then to “{base}” on the Knowledge screen. It is built "
            "without it")


def switch_lines(a: Any) -> list[str]:
    """A new agent's switches as the agent editor shows them (F1223), one line each: what the card says it gets."""
    out = []
    if getattr(a, "memory", False):
        out.append("Memory: on — it keeps what it learns across runs and conversations")
    if getattr(a, "knowledge", None):
        out.append("Knowledge: answers from " + ", ".join(f"“{k}”" for k in a.knowledge))
    for server in getattr(a, "servers", None) or []:
        out.append(f"MCP server {server}: its tools as of now; tools it adds later are not added")
    browser = getattr(a, "browser", None)
    if getattr(a, "type", "") == "browser" and browser is not None:
        where = ", ".join(browser.sites) if browser.sites else "any public site"
        mode = "types and sends with you watching" if browser.mode == "co_drive" else "reads only"
        out.append(f"Browser: {where}; {mode}")
        signed = [s for s in browser.sessions if s not in browser.sessions_missing]
        if signed:
            out.append("Starts signed in on " + ", ".join(signed))
        out += [missing_session_line(site) for site in browser.sessions_missing]
        if browser.recordings:
            out.append("Runs the recordings " + ", ".join(f"“{r}”" for r in browser.recordings))
        if browser.action_budget is not None:
            out.append(f"At most {browser.action_budget} browser actions in a task")
    return out + setting_lines(a)


def setting_lines(a: Any) -> list[str]:
    """The rest of a new agent's settings (F1259, `agent_census`), one line each, each only when the plan sets it."""
    out: list[str] = []
    if getattr(a, "type", "") == "team":
        members = ", ".join(f"“{m}”" for m in getattr(a, "members", None) or []) or "no members yet"
        team = getattr(a, "team", None)
        if team is None or team.mode == "team":
            at_once = getattr(team, "at_once", None) or 3
            limit = getattr(team, "spend_limit_usd", None)
            spend = f", within ${limit:,.2f}" if limit else "; no spend limit is set"
            out.append(f"Team: {members}, up to {at_once} at once{spend}")
        else:
            out.append(f"Team: {members}, one at a time")
    if getattr(a, "skills", None):
        out.append("Skills: " + ", ".join(f"“{k}”" for k in a.skills))
    if getattr(a, "helpers", False):
        out.append("Helpers: on — it may hand parts of a job to helpers that run beside it, on its own model")
    if getattr(a, "planning", True) is False:
        out.append("Planning: off — it answers straight away")
    if getattr(a, "skip_default_tools", False):
        out.append("Only the tools named here, none of the product's own")
    if getattr(a, "max_turns", None):
        out.append(f"At most {a.max_turns} turns a task")
    if getattr(a, "turn_limit_mode", None):
        out.append({"ask": "At its turn limit: asks you", "continue": "At its turn limit: carries on",
                    "stop": "At its turn limit: stops"}[a.turn_limit_mode])
    if getattr(a, "reasoning_effort", None):
        out.append(f"Reasoning: {a.reasoning_effort}")
    if getattr(a, "max_output_tokens", None):
        out.append(f"Writes at most {a.max_output_tokens:,} tokens an answer")
    if getattr(a, "boards_own", None) is False:
        out.append("Boards: shares a board rather than having its own")
    if getattr(a, "repos", None):
        out.append("Repositories: " + ", ".join(f"“{r}”" for r in a.repos))
    if getattr(a, "db_configs", None):
        out.append("Databases: " + ", ".join(f"“{d}”" for d in a.db_configs))
    if getattr(a, "workflow", None):
        out.append(f"Runs the workflow “{a.workflow}”")
    return out


def knowledge_lines(plan: Any) -> list[str]:
    """Each knowledge base the plan makes or uses (F1224), one line each, and each file it could not find."""
    out = []
    for part in getattr(plan, "knowledge", None) or []:
        out.append(knowledge_part_line(part))
        out += [missing_file_line(part.name, f) for f in part.files_missing]
    return out


def api_part_line(part: Any) -> str:
    """The person's own API (F1225), as the card and the build's parts both say it."""
    tools = ", ".join(f"`{t}`" for t in part.tools)
    return f"“{part.name}”, from the spec at `{part.spec}`: " + (f"made with it as {tools}" if tools
                                                                 else "none of its tools is used")


def api_problem_line(part: Any) -> str:
    """Why the person's own API's spec cannot be used here (F1225): the card's line and the build's."""
    return f"“{part.name}”: {part.problem}"


def mcp_server_line(part: Any) -> str:
    """The person's own MCP server (F1225), connected or not yet: the card's line and the build's."""
    if part.connected:
        return f"MCP server “{part.name}” at {part.url}: connected; its tools as of now are used"
    return unconnected_server_line(part)


def unconnected_server_line(part: Any) -> str:
    """The person's own MCP server before it is connected (F1225): its Connect card comes before the plan."""
    return (f"MCP server “{part.name}” at {part.url} is not connected yet: connect it on its card above, and I'll "
            "plan on its tools")


def own_part_lines(plan: Any) -> list[str]:
    """The person's own APIs and MCP servers in the plan (F1225), one line each."""
    return [*(api_problem_line(p) if p.problem else api_part_line(p) for p in getattr(plan, "apis", None) or []),
            *(mcp_server_line(m) for m in getattr(plan, "mcp_servers", None) or [])]


def connect_first_lead(waiting: list[Any]) -> str:
    """The lead-in when the person's own MCP server is connected before any plan (F1225)."""
    one = len(waiting) == 1
    return (f"Before I plan this, connect the MCP server{'' if one else 's'} you gave: what "
            f"{'it' if one else 'they'} can do is known only once {'it is' if one else 'they are'} connected.")


def connect_first_text(waiting: list[Any]) -> str:
    """What stands in the place of the plan until the person's own MCP server is connected (F1225)."""
    return ("\n".join(f"- {unconnected_server_line(m)}" for m in waiting)
            + f"\n\nOr tell me to plan it without {'it' if len(waiting) == 1 else 'them'}.")


def eval_part_line(part: Any) -> str:
    """A plan's eval (F1255, D340 F), in one line: the card's and the build's parts' words."""
    cases = len(part.cases)
    if part.keeps:
        line = f"its eval: it keeps its rubric “{part.keeps}”, which a judge scores each run that finishes by"
    else:
        many = "criterion" if len(part.criteria) == 1 else "criteria"
        line = (f"its eval: the rubric “{part.name}” ({len(part.criteria)} {many}, scored 0–{part.scale_max:g}), "
                "which a judge scores each run that finishes by")
    line += (f", and a golden set of {cases} case{'s' if cases != 1 else ''} to run on Evals" if cases
             else "; its scores come from its real runs")
    return line + (f"; it replaces the rubric “{part.replaces}”, so its score trend starts again"
                   if part.replaces else "")


def eval_lines(plan: Any) -> list[str]:
    """A plan's eval in full (F1255): each criterion with its weight, and every case as it will be saved, since the
    yes saves exactly these (Naveen, 10-30); and what judging costs, every run (TRI1, D340 F)."""
    part = getattr(plan, "eval", None)
    if part is None:
        return []
    out = [eval_part_line(part)]
    if not part.keeps:
        out += [f"Criterion “{c.name}” (weight {c.weight:g})" + (f": {c.guidance}" if c.guidance else "")
                for c in part.criteria]
        if part.floor is not None:
            out.append(f"A score below {part.floor:g} is a regression")
    out += [f"Case {i}{f' “{c.name}”' if c.name else ''}: {c.input}" + (f" — {c.notes}" if c.notes else "")
            for i, c in enumerate(part.cases, 1)]
    how = ("Every run that finishes is judged" if part.judge_every <= 1 or part.keeps
           else f"Its first finished run is judged, then 1 run in {part.judge_every}")
    out.append(f"{how}: one more model call per judged run" + (f", to {part.judge}" if part.judge else "")
               + (f" ({part.judge_cost} a judged run)" if part.judge_cost else "") + ", on top of the run's own")
    return out


def render(plan: Plan, *, agent_names: dict[str, str] | None = None) -> str:
    """The plan card's text. `agent_names` names the workspace's agents a plan reuses (by id)."""
    names = dict(agent_names or {})
    inner = {w.title for w in plan.inner_workflows}
    parts: list[str] = []

    # The planner's words are the request, never what it built (F1228): what it does is the parts' below.
    overview = (plan.summary or "").strip()
    if overview:
        parts.append(f"## You asked for\n{overview}")
    # What a re-plan changed from the plan the person last saw (F1245), before the steps it changed.
    if since := getattr(plan, "since_last", None):
        parts.append("## Changed from the last plan\n" + _bullets(since))

    words = getattr(plan, "step_words", {}) or {}
    if plan.steps or plan.edits:
        fills = set(getattr(plan, "model_fills", []))
        lines = [_step_line(i, s, names, inner, fills, words)
                 for i, s in enumerate((s for s in plan.steps if s.type != "end"), 1)]
        for e in plan.edits:
            what = {"add_step": f"Add **{e.step.name if e.step else '?'}** after {e.after}",
                    "update_step": f"Change **{e.step_id}**", "remove_step": f"Remove **{e.step_id}**",
                    "set_meta": "Change the workflow's details"}[e.op]
            lines.append(f"{len(lines) + 1}. {what}")
        # A part the card says in its own section below (a knowledge base) is said there, once (F1228).
        said = [*knowledge_lines(plan), *own_part_lines(plan), *eval_lines(plan)]
        own = [w for w in getattr(plan, "part_words", []) or [] if w not in said]
        parts.append("## Steps\n" + "\n".join(lines) + ("\n\n" + _bullets(own) if own else ""))
    for w in plan.inner_workflows:
        steps = [_step_line(i, s, names, inner, words=words)
                 for i, s in enumerate((s for s in w.steps if s.type != "end"), 1)]
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
            agent_lines += [f"  - {s}" for s in switch_lines(a)]
    if plan.reuse_agents:
        agent_lines.append("### Existing agents it uses")
        agent_lines += [f"- {names.get(a, a)}" for a in plan.reuse_agents]
    if agent_lines:
        parts.append("## Agents\n" + "\n".join(agent_lines))
    elif getattr(plan, "calls_no_model", False):
        parts.append("## Agents\nNo agents: it runs on tools only, with no model called.")

    if lines := knowledge_lines(plan):
        parts.append("## Knowledge bases\n" + "\n".join(f"- {line}" for line in lines))
    if lines := own_part_lines(plan):
        parts.append("## Your APIs and MCP servers\n" + "\n".join(f"- {line}" for line in lines))
    if lines := eval_lines(plan):
        parts.append("## Eval\n" + "\n".join(f"- {line}" for line in lines))

    if plan.schedule and (plan.schedule.says or "").strip():
        # The cron's own time on its clock, in code's words (F1393), never the planner's: the clock is said beside it.
        from core.native_builder.schedule_clock import cron_words

        cron = plan.schedule.cron.strip() if plan.schedule.schedule_type == "cron" else ""
        words = cron_words(cron, clock=False) if cron else ""
        when = words if words and words != cron else plan.schedule.says.strip()
        every = (f"every {plan.schedule.interval_value} {plan.schedule.interval_unit}"
                 if plan.schedule.schedule_type == "interval" and plan.schedule.interval_value else "")
        detail = ", ".join(x for x in (f"`{plan.schedule.cron.strip()}`" if plan.schedule.cron.strip() else every,
                                       zone_words(plan.schedule.timezone) if plan.schedule.timezone else "") if x)
        text = f"**When:** {when}" + (f" ({detail})" if detail else "")
        if (plan.schedule.input or "").strip():
            text += f"\n**Each run starts with:** {plan.schedule.input.strip()}"
        text += ("\n**If a time is missed** (the machine was off): "
                 + ("it runs once as soon as it can" if plan.schedule.missed_run_policy == "run_immediately"
                    else "it is skipped, and it runs at the next time"))
        text += "\nIt is made paused: turn it on under Schedules once you have checked what each run starts with."
        parts.append(f"## Schedule\n{text}")

    if getattr(plan, "run_cost", ""):
        # What a run costs in model calls (D340 D3): a rough figure, said as one.
        parts.append(f"## Each run\n{plan.run_cost}")

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

    from core.native_builder.parts_words import missing_words

    # What the card already said under an agent or a knowledge base is not said again (F1228): the panel, which has
    # neither section, shows each in its own "Missing or off".
    shown = [*knowledge_lines(plan), *own_part_lines(plan), *(line for a in plan.agents for line in switch_lines(a))]
    missing = missing_words(plan) + [o for o in getattr(plan, "off", []) or []
                                     if o not in shown and not any(o.endswith(f": {s}") for s in shown)]
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
