"""What a build's parts do, in code's words (F1228, GUIDE1; CAP3's P5).

The plan card and the build's last message used to say what a workflow does in the planner's own words, and those
words said what the planner meant to build, not what it built: "posts the results as a chart card" on a board that
holds tasks and notes; "can look up order status" from an agent with no tool for the orders API. These are the parts
instead, one table row per kind of part, each saying what that part does from its fields, and never what a run will
find or achieve:

* **PARTS** (`step_words`, `part_words`): each step by its type, with its agent's own parts (its tools by their own
  description, memory, knowledge, browser sites and kept sessions, an app's tools); then the plan's own parts (a
  board and the cards it holds, a knowledge base, an API's tools, an MCP server, the schedule).
* **OFF** (`off_words`): what is missing or off, each said with where it is put right (a schedule made paused, an
  agent with no tools, a site with no kept session, a knowledge file not in the Vault, an API that did not read, an
  MCP server not connected). A missing tool and a connection still to make keep their own lines (`plan.missing`,
  `plan.connections_needed`).

`fill` writes all three onto the Plan, so the card, its text, the build's last message and a later answer about the
workflow read the same words. Every fact is the Plan's own or the host's (the tool catalogue through the server
module, the agents as `load_user_agents` gives them): cloud fills each the way it fills the plan's tool check.
A new kind of part is one row here: the `_STEP` table, `_AGENT_PARTS`, `_PLAN_PARTS` or `_OFF`.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from core.native_builder.objects import Plan, Step

_log = logging.getLogger(__name__)


def board_kinds() -> list[str]:
    """The cards a board holds, as the board tools' own schema says them."""
    from core.boards.tools import _SCHEMAS

    return list(_SCHEMAS["board_add"][1]["properties"]["kind"]["enum"])


def _kinds_words() -> str:
    kinds = [f"{k}s" for k in board_kinds()]
    return kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1]


def _quoted(names: list[str]) -> str:
    names = [f'"{n}"' for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _plain(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


_SENTENCE = re.compile(r"(?<=[.!?])\s")


def title_of(name: str, does: str = "") -> str:
    """A tool as a person reads it: its own description's first sentence (no id, lower-case start), or its name in
    words when it has none. A board tool says the cards it adds."""
    from core.boards import TOOL_NAMES as BOARD_TOOLS

    if name == "board_add":
        return f"adds cards to a board, as {_kinds_words()}"
    if name in BOARD_TOOLS:
        return f"reads and updates a board's cards ({_kinds_words()})"
    first = _SENTENCE.split(" ".join(str(does or "").split()), maxsplit=1)[0].rstrip(".").strip()
    if not first or len(first) > 90:
        return name.replace("_", " ").replace(".", " ")
    return first[:1].lower() + first[1:]


async def tool_titles(server_module: Any) -> dict[str, str]:
    """Every tool here by its title (`title_of`), read from the workspace's catalogue as the plan's check reads it."""
    from core.builder_tools import _workspace_catalogue

    try:
        catalogue = await _workspace_catalogue(server_module)
    except Exception as exc:  # noqa: BLE001 — a catalogue that cannot be read leaves each tool its name in words
        _log.warning("tool titles not read: %s", exc)
        return {}
    out = {}
    for t in catalogue:
        name = getattr(t, "name", None) or (t.get("function", {}).get("name") if isinstance(t, dict) else None)
        if not name:
            continue
        own = getattr(t, "title", None) or getattr(getattr(t, "annotations", None), "title", None)
        does = getattr(t, "description", None) or (t.get("function", {}).get("description")
                                                    if isinstance(t, dict) else "")
        out[name] = own.strip() if isinstance(own, str) and own.strip() else title_of(name, does or "")
    return out


async def kb_names() -> dict[str, str]:
    """Each knowledge base's name by its id, so an agent already here says its bases by name; none while Knowledge
    is off."""
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        return {}
    try:
        from core.knowledge import bases

        return {kb.id: kb.name for kb in await bases.list_bases()}
    except Exception as exc:  # noqa: BLE001 — a base it cannot name is said by its id
        _log.warning("knowledge base names not read: %s", exc)
        return {}


# ── an agent's parts, as one shape for a new agent and one already here ──────────────────────────────────────────

@dataclass
class AgentParts:
    name: str
    type: str = "agent"
    tools: list[str] = field(default_factory=list)
    memory: bool = False
    knowledge: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    sessions_missing: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    browser: bool = False
    mode: str = "read_only"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def agent_parts(agent: Any, *, kb_names: dict[str, str] | None = None) -> AgentParts:
    """A plan's new agent (`NewAgent`) or one already here (its saved dict) as its parts. BLD1's switches (F1223:
    `memory`, `knowledge`, `browser`, `servers`) are read where the agent has them, and are nothing where not."""
    browser = _get(agent, "browser")
    kbs = [str(k) for k in _get(agent, "knowledge", None) or []]
    return AgentParts(
        name=str(_get(agent, "name", "") or _get(agent, "id", "")), type=str(_get(agent, "type", "agent") or "agent"),
        tools=[str(t) for t in _get(agent, "tools", None) or []], memory=bool(_get(agent, "memory", False)),
        knowledge=[(kb_names or {}).get(k, k) for k in kbs],
        sites=[str(s) for s in _get(browser, "sites", None) or []],
        sessions=[str(s) for s in _get(browser, "sessions", None) or []],
        sessions_missing=[str(s) for s in _get(browser, "sessions_missing", None) or []],
        servers=[str(s) for s in _get(agent, "servers", None) or []],
        browser=browser is not None or _get(agent, "type") == "browser",
        mode=str(_get(browser, "mode", "read_only") or "read_only"))


def _tools_words(a: AgentParts, titles: dict[str, str]) -> str:
    own = a.tools
    if "all" in own:
        return "with every tool here"
    if not own:
        return ""
    return "with tools: " + "; ".join(titles.get(t) or title_of(t) for t in own)


def switches(a: AgentParts) -> list[str]:
    """An agent's switches in the card's own words (BLD1's `render.switch_lines`, F1223), for a new agent and one
    already here alike; a site with no kept session is said once, as what is missing or off (`_off_sessions`)."""
    from core.native_builder.render import missing_session_line, switch_lines

    browser = SimpleNamespace(sites=a.sites, sessions=a.sessions, mode=a.mode, sessions_missing=a.sessions_missing)
    shim = SimpleNamespace(memory=a.memory, knowledge=a.knowledge, servers=a.servers,
                           type="browser" if a.browser else a.type, browser=browser if a.browser else None)
    missing = {missing_session_line(site) for site in a.sessions_missing}
    return [line for line in switch_lines(shim) if line not in missing]


def agent_words(a: AgentParts, titles: dict[str, str]) -> str:
    """An agent by its parts: 'the agent "Poster", with tools: …; Memory: on — …'."""
    said = [w for w in (_tools_words(a, titles), *switches(a)) if w]
    head = f'the {"team" if a.type == "team" else "agent"} "{a.name}"'
    return head + (", " + "; ".join(said) if said else ", with no tools")


# ── each step by its type ────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Ctx:
    titles: dict[str, str]
    agents: dict[str, AgentParts]  # by the name or id a step's `agent` gives
    names: dict[str, str]  # step id → name
    fills: set[str]


def _names(ids: list[str] | None, ctx: _Ctx) -> str:
    return _quoted([ctx.names.get(i, i) for i in ids or []]) if ids else "its steps"


def _agent_step(s: Step, ctx: _Ctx) -> str:
    a = ctx.agents.get(s.agent or "")
    return "done by " + (agent_words(a, ctx.titles) if a else f'the agent "{s.agent}"')


#: An input whose key names where a tool acts (F1245): its value is shown with the step.
_WHERE_KEY = re.compile(r"(?i)(?:^|_)(?:url|uri|link|host|domain|site|path|file|folder|target|to|channel|repo|"
                        r"endpoint|address|email)s?(?:$|_)")
#: An input whose key reads as a secret, by its name: never shown, whatever it holds.
_SECRET_KEY = re.compile(r"(?i)key|secret|token|passw|credential|bearer|cookie|(?:^|_)auth")
#: A value that says where by itself: a URL, a host, a path or an address.
_WHERE_VALUE = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*://\S+|www\.\S+|[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*)?|"
                          r"~?/[\w./-]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)$")
_FROM_THE_RUN = re.compile(r"^\{\{?\s*(?:state|input|global)\.[^{}]+\}\}?$")


def shown_inputs(arguments: Any) -> dict[str, str]:
    """A tool step's set inputs that say where it acts, by key (F1245: a re-plan swapped synapseorch.com for
    synapseorchestrator.com and the card said only "uses `scrape_url`"): a URL, host, path, address or target, by its
    key or its value, a value from the run said as that. Never one whose key or value reads as a secret, and never
    long text: that is the step's own business."""
    from core.native_builder.export import CREDENTIAL_MARK
    from core.secret_scrub import holds_credential

    out: dict[str, str] = {}
    for key, value in (arguments.items() if isinstance(arguments, dict) else ()):
        key = str(key)
        if _SECRET_KEY.search(key) or isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if not text or len(text) > 120 or holds_credential(text) or CREDENTIAL_MARK in text:
            continue
        if _FROM_THE_RUN.match(text):
            if _WHERE_KEY.search(key):
                out[key] = "from the run"
        elif _WHERE_KEY.search(key) or _WHERE_VALUE.match(text):
            out[key] = text
    return out


def _tool_step(s: Step, ctx: _Ctx) -> str:
    said = ctx.titles.get(s.tool or "") or title_of(s.tool or "")
    shown = shown_inputs((s.config or {}).get("tool_arguments"))
    said += f" ({'; '.join(f'{k}: {v}' for k, v in shown.items())})" if shown else ""
    return said + ("; a model fills in its inputs" if s.id in ctx.fills else "")  # F967's words


_STEP: dict[str, Callable[[Step, _Ctx], str]] = {
    "tool": _tool_step,
    "agent": _agent_step,
    "llm": lambda s, _c: "a model writes it, with no tools",
    "evaluator": lambda s, _c: "a model scores the result before it goes on",
    "transform": lambda s, _c: "runs its Python code, shown below, on what the run has so far",
    "extract_json": lambda s, _c: "pulls fields out of the result before it",
    "human": lambda s, _c: "stops and asks you" + (f': "{s.prompt.strip()}"' if (s.prompt or "").strip() else ""),
    "if_else": lambda s, _c: "goes one of two ways" + (f", on: {s.condition.strip()}"
                                                       if (s.condition or "").strip() else ""),
    "switch": lambda s, _c: f"picks one of {len(s.branches or {}) or 'its'} routes",
    "loop": lambda s, c: f"runs {_names(s.body, c)} for each item"
    + (f" of {s.loop_over.strip()}" if (s.loop_over or "").strip() else ""),
    "parallel": lambda s, c: f"runs {_names(s.body, c)} side by side",
    "merge": lambda s, _c: "joins the results of the steps before it",
    "print": lambda s, _c: "shows a message",
    "workflow": lambda s, _c: f'runs the workflow "{s.workflow}"',
    "retrieve": lambda s, _c: "searches a knowledge base",
    "browser": lambda s, _c: "replays a browser recording made in the app" + (
        f", signed in to {_plain(list((s.config or {}).get('browser_sessions') or []))}"
        if (s.config or {}).get("browser_sessions") else "") + (
        "; a page that changed waits for you" if (s.config or {}).get("on_miss", "pause") == "pause" else ""),
    "ingest": lambda s, _c: "adds what the run has to a knowledge base",
}


def _step_words(s: Step, ctx: _Ctx) -> str:
    row = _STEP.get(s.type)
    return row(s, ctx) if row else f"a {s.type.replace('_', ' ')} step"


# ── the plan's own parts ─────────────────────────────────────────────────────────────────────────────────────────

def _boards(plan: Plan, _ctx: _Ctx) -> list[str]:
    return [f'the board "{b.name}" ({"already here" if b.existing else "made with it"}): it holds '
            f"{_kinds_words()}, nothing else" for b in plan.boards]


def _knowledge(plan: Plan, _ctx: _Ctx) -> list[str]:
    from core.native_builder.render import knowledge_part_line

    return [knowledge_part_line(k) for k in getattr(plan, "knowledge", None) or []]  # F1224, as the card says it


def _apis(plan: Plan, _ctx: _Ctx) -> list[str]:
    from core.native_builder.render import api_part_line

    return [api_part_line(p) for p in getattr(plan, "apis", None) or [] if not p.problem]  # F1225, as the card says it


def _mcp_servers(plan: Plan, _ctx: _Ctx) -> list[str]:
    from core.native_builder.render import mcp_server_line

    return [mcp_server_line(m) for m in getattr(plan, "mcp_servers", None) or [] if m.connected]  # F1225


def _schedule(plan: Plan, _ctx: _Ctx) -> list[str]:
    return [f"runs {plan.schedule.says.strip()}, by its schedule"] if plan.schedule and (
        plan.schedule.says or "").strip() else []


_PLAN_PARTS: list[Callable[[Plan, _Ctx], list[str]]] = [_boards, _knowledge, _apis, _mcp_servers, _schedule]


# ── what is missing or off ───────────────────────────────────────────────────────────────────────────────────────

def _off_schedule(plan: Plan, _ctx: _Ctx) -> list[str]:
    return ["its schedule is made paused: turn it on under Schedules"] if plan.schedule else []


def _off_no_tools(plan: Plan, ctx: _Ctx) -> list[str]:
    used = list(dict.fromkeys(s.agent for s in _all_steps(plan) if s.type == "agent" and s.agent))
    return [f'"{a.name}" has no tools: it works only from what the run gives it, and fetches or changes nothing'
            for a in (ctx.agents.get(x) for x in used)
            if a and a.type != "team" and not a.tools and not a.servers and not a.browser and not a.knowledge]


def _off_sessions(plan: Plan, ctx: _Ctx) -> list[str]:
    """F1223's line for each site, the agent named before it: off the card's agent, "this agent" needs its name."""
    from core.native_builder.render import missing_session_line

    return [f'"{a.name}": {missing_session_line(site)}' for a in ctx.agents.values() for site in a.sessions_missing]


def _off_knowledge(plan: Plan, _ctx: _Ctx) -> list[str]:
    from core.native_builder.render import missing_file_line

    return [missing_file_line(k.name, f) for k in getattr(plan, "knowledge", None) or [] for f in k.files_missing]


def _off_apis(plan: Plan, _ctx: _Ctx) -> list[str]:
    from core.native_builder.render import api_problem_line

    return [api_problem_line(p) for p in getattr(plan, "apis", None) or [] if p.problem]  # F1225


def _off_mcp(plan: Plan, _ctx: _Ctx) -> list[str]:
    from core.native_builder.render import unconnected_server_line

    return [unconnected_server_line(m) for m in getattr(plan, "mcp_servers", None) or [] if not m.connected]  # F1225


_OFF: list[Callable[[Plan, _Ctx], list[str]]] = [_off_schedule, _off_no_tools, _off_sessions, _off_knowledge,
                                                 _off_apis, _off_mcp]


# ── the words ────────────────────────────────────────────────────────────────────────────────────────────────────

def _all_steps(plan: Plan) -> list[Step]:
    return [s for s in (*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)) if s.type != "end"]


def _context(plan: Plan, titles: dict[str, str], agents: dict[str, dict] | None,
             kb_names: dict[str, str] | None) -> _Ctx:
    parts: dict[str, AgentParts] = {}
    for aid, saved in (agents or {}).items():
        if aid in plan.reuse_agents or any(s.agent == aid for s in _all_steps(plan)):
            parts[aid] = agent_parts(saved, kb_names=kb_names)
    for new in plan.agents:
        parts[new.name] = agent_parts(new, kb_names=kb_names)
    return _Ctx(titles=titles, agents=parts, names={s.id: s.name for s in _all_steps(plan)},
                fills=set(plan.model_fills))


def describe(plan: Plan, *, titles: dict[str, str], agents: dict[str, dict] | None = None,
             kb_names: dict[str, str] | None = None) -> tuple[dict[str, str], list[str], list[str]]:
    """The plan's words: each step's by its id, the plan's own parts, and what is missing or off."""
    ctx = _context(plan, titles, agents, kb_names)
    steps = {s.id: _step_words(s, ctx) for s in _all_steps(plan)}
    parts = [w for row in _PLAN_PARTS for w in row(plan, ctx)]
    off = list(dict.fromkeys(w for row in _OFF for w in row(plan, ctx)))
    return steps, parts, off


def missing_words(plan: Plan) -> list[str]:
    """The plan's missing tools and connections still to make, as the card has always said them."""
    return ([f"`{n}`: nothing here provides it yet" for n in plan.missing]
            + [f"{c}: connect it first" for c in plan.connections_needed])


def summary(plan: Plan) -> str:
    """The build's last message (Naveen 10-29): each part in short, then everything missing or off, then what it did
    not carry over. Never a result."""
    lines = [f'- **{s.name}**: {plan.step_words[s.id]}' for s in _all_steps(plan) if plan.step_words.get(s.id)]
    lines += [f"- {w[:1].upper()}{w[1:]}" for w in plan.part_words]
    out = "\n\n**What it does**\n" + "\n".join(lines) if lines else ""
    off = [*missing_words(plan), *plan.off]
    if off:
        out += "\n\n**Missing or off**\n" + "\n".join(f"- {w[:1].upper()}{w[1:]}" for w in off)
    if plan.not_carried_over:  # as the card says it (F1246): what a pasted program had that this does not
        out += "\n\n**Not carried over**\n" + "\n".join(f"- {w[:1].upper()}{w[1:]}" for w in plan.not_carried_over)
    return out


async def workflow_said(orch: dict, server_module: Any) -> dict[str, list[str]] | None:
    """A saved workflow in these words, line by line: its numbered steps, its own parts, and what is missing or off,
    schedules read as they are saved. Never its configs or prompts: read_workspace's `workflow` topic answers these
    (F1237), as a later turn's pack does (`workflow_words`). None for a workflow the reader cannot take."""
    from core.native_builder.compile import plan_from_workflow
    from core.routes.agents import load_user_agents
    from core.store import schedules

    try:
        plan = plan_from_workflow(orch)
    except Exception as exc:  # noqa: BLE001 — a workflow the reader cannot take is left unsaid, never guessed
        _log.warning("workflow %s not read for its words: %s", orch.get("id"), exc)
        return None
    agents = {a["id"]: a for a in await load_user_agents() if a.get("id")}
    steps, parts, off = describe(plan, titles=await tool_titles(server_module), agents=agents,
                                 kb_names=await kb_names())
    off = [w for w in off if not w.startswith("its schedule")]  # a saved schedule says its own state
    for sched in await schedules.load():
        if sched.get("target_id") == orch.get("id") and not sched.get("enabled"):
            off.append(f'its schedule "{sched.get("name") or sched.get("id")}" is paused: turn it on under Schedules')
    lines = [f"{n}. {s.name}: {steps[s.id]}" for n, s in enumerate(_all_steps(plan), 1) if steps.get(s.id)]
    return {"steps": lines, "parts": parts, "off": off}


async def workflow_words(orch_id: str, server_module: Any) -> str:
    """A saved workflow in these words, for a later turn that talks about it (F1228: brief 2 said the workflow it had
    just built "can look up order status"). "" for a workflow that is not there."""
    from core.builder_tools import _visible_orchestration

    orch = await _visible_orchestration(orch_id)
    if not isinstance(orch, dict) or not orch.get("steps"):
        return ""
    said = await workflow_said(orch, server_module)
    if said is None:
        return ""
    lines = [*said["steps"], *(f"- {w}" for w in said["parts"])]
    text = f'The workflow "{orch.get("name") or orch_id}" ({orch.get("id")}), step by step:\n' + "\n".join(lines)
    return text + ("\nMissing or off:\n" + "\n".join(f"- {w}" for w in said["off"]) if said["off"] else "")

