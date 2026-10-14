"""What the AI builder may not save: a tool that nothing provides (F489, BWA1, D274).

SW1 saw it in every Agent Builder translation, and VOX1e met it too: `create_agent` took any tool name — the pasted
code's own variables, a tool the model expected to exist — and saved the agent with it. At run time the agent silently
lacked the tool, and a forced `tool` step naming one failed ("Tool '…' not found in available tools"). The builder's
validator never looked.

Every name is checked with the engine's own resolver (`core.tools.resolve_one_tool`), the lookup a run makes, so
"exists" means exactly "a run could call it":

- `unknown(names)` — the names in a list that nothing provides; `create_agent(s)` and `update_agent` refuse them,
  with the nearest names that do exist, at the moment the builder writes them;
- `missing(names)` — the same check for a person (F526): an agent saved through its editor or the API is told,
  not refused, and its page marks the names;
- `missing_in(workflows, agents)` — for `validate_orchestration`: a forced tool step naming one is an issue (it fails
  on the first run, certainly); an agent naming one is a warning, since a workspace's own older agents may name a
  tool from a server that has gone, and refusing every build that uses them would be a new failure.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

#: Not tool names: what a tool list may hold besides one.
_NOT_A_NAME = ("all",)


async def _resolver(server_module: Any):
    from core.routes.tools import load_custom_tools
    from core.tools import resolve_one_tool

    custom = await load_custom_tools()
    cache: dict[str, bool] = {}

    async def exists(name: str) -> bool:
        if name in _NOT_A_NAME or name.startswith("group:"):
            return True
        if name not in cache:
            try:
                cache[name] = await resolve_one_tool(server_module, name, {"tools": [name]}, custom) is not None
            except Exception:  # noqa: BLE001 — a lookup that cannot run is not evidence a tool is missing
                cache[name] = True
        return cache[name]

    return exists


async def _known_names(server_module: Any) -> list[str]:
    """Every tool name this workspace offers, for suggesting the nearest real one."""
    try:
        from core.routes.tools import load_custom_tools
        from core.tools import aggregate_all_tools

        tools = (await aggregate_all_tools(server_module, {"tools": ["all"]}, await load_custom_tools()))[0]
        return sorted({getattr(t, "name", "") for t in tools if getattr(t, "name", "")})
    except Exception:  # noqa: BLE001 — suggestions are a courtesy
        return []


async def missing(names: list[Any], server_module: Any, *, cutoff: float = 0.75) -> list[tuple[str, list[str]]]:
    """Each name nothing provides, with the nearest names that do exist, for a person to read (F526, VOX1f): an
    agent saved through its editor or the API, or shown on its page. `all` and `group:` are lists, not names.
    The cutoff is closer than `unknown()`'s for the builder model: at 0.5 `smtp_send_mail` came back
    "nearest: list_schedules" (BWA1, F492)."""
    exists = await _resolver(server_module)
    gone = [n for n in dict.fromkeys(names) if isinstance(n, str) and n and not await exists(n)]
    if not gone:
        return []
    known = await _known_names(server_module)
    return [(n, difflib.get_close_matches(n, known, n=3, cutoff=cutoff)) for n in gone]


async def unknown(names: list[Any], server_module: Any) -> dict | None:
    """The refusal for a tool list naming tools nothing provides, or None when every name is real."""
    exists = await _resolver(server_module)
    missing = [n for n in names if isinstance(n, str) and not await exists(n)]
    if not missing:
        return None
    known = await _known_names(server_module)
    near = {n: difflib.get_close_matches(n, known, n=3, cutoff=0.5) for n in missing}
    hints = "; ".join(f"'{n}' (did you mean {', '.join(repr(c) for c in near[n])}?)" if near[n] else f"'{n}'"
                      for n in missing)
    return {"error": f"No tool here is called {hints}. Give the agent only tools that exist: read them with "
                     "`get_tools_detail` or `list_server_tools`, or create the tool first (`create_http_tool`). If "
                     "the plan needs a tool nothing provides, say so in your answer instead of naming it."}


async def missing_in(workflows: list[dict], agents: dict[str, dict], server_module: Any) -> tuple[list[str], list[str]]:
    """`(issues, warnings)` for tools the workflows' steps or agents name that nothing provides."""
    exists = await _resolver(server_module)
    issues: list[str] = []
    warnings: list[str] = []
    seen_agents: set[str] = set()
    for workflow in workflows:
        for step in workflow.get("steps") or []:
            label = step.get("name") or step.get("id")
            forced = step.get("forced_tool")
            if isinstance(forced, str) and forced and not await exists(forced):
                issues.append(f"Step '{label}' calls the tool '{forced}', which nothing here provides: its first run "
                              "would fail. Use a tool that exists, or create it first.")
            agent_id = step.get("agent_id") or ""
            agent = agents.get(agent_id)
            if not agent or agent_id in seen_agents:
                continue
            seen_agents.add(agent_id)
            for name in agent.get("tools") or []:
                if isinstance(name, str) and not await exists(name):
                    warnings.append(f"The agent '{agent.get('name')}' (step '{label}') names the tool '{name}', which "
                                    "nothing here provides; it will run without it.")
    return issues, warnings


# ── The plan, before the person approves it (F492) ──────────────────────────
#
# gpt-oss-120b's BWA1 baseline: 37 of 45 saver runs stopped because the plan the person had approved named tools
# nothing here provides ("Tool not found") or the agents it needed were never made (the agent creator refused the
# same tools). The check above comes after the approval; this one reads the plan the drafter wrote, in the shape its
# prompt asks for, and checks each name with the run's own lookup.

#: A new agent's `tools: [a, b]` line (the drafter's "exact shape so the Agent Creator can parse it").
_AGENT_TOOLS = re.compile(r"^[ \t>*`-]*tools:\s*\[([^\]\n]*)\]", re.M | re.I)
#: A tool step's forced tool: `**Tool:** `name``, `Tool: `name``, "forced tool `name`".
_STEP_TOOL = re.compile(r"(?:\*\*tool:?\*\*:?|\btool:|forced[ _]tool(?:\s+name)?(?:\s+is)?:?)\s*`([^`\s(]+)", re.I)
_AGENT_ID = re.compile(r"\b(agent_[A-Za-z0-9_]{6,})\b")


def names_in_plan(plan: str) -> dict[str, list[str]]:
    """`{"tools": [...], "reused_agents": [...]}`: the tool names a plan's agents and tool steps use, and the ids of
    the existing agents it reuses (only from its "Existing agents reused" list: a new agent's placeholder id is not
    one)."""
    tools: list[str] = []
    for listed in _AGENT_TOOLS.findall(plan or ""):
        tools += [t.strip().strip("'\"`") for t in listed.split(",")]
    tools += _STEP_TOOL.findall(plan or "")
    seen: set[str] = set()
    tools = [t for t in tools if re.search(r"[A-Za-z]", t) and t not in seen and not seen.add(t)]
    reused: list[str] = []
    text = plan or ""
    start = text.lower().find("existing agents reused")
    if start >= 0:
        end = text.lower().find("new agents to create", start)
        reused = sorted(set(_AGENT_ID.findall(text[start:end if end > start else None])))
    return {"tools": tools, "reused_agents": reused}


async def check_plan(plan: str, server_module: Any, *, checks: int = 0) -> dict:
    """What a plan names that nothing here provides, before the person sees it.

    `redraft` is true the first time anything is missing, so the drafter gets one chance to use real names; after
    that, what is still missing goes in front of the person (`note`) instead of stopping the build after approval.
    """
    from core.routes.agents import load_user_agents

    names = names_in_plan(plan)
    exists = await _resolver(server_module)
    missing_tools = [t for t in names["tools"] if not await exists(t)]
    have = {a.get("id") for a in await load_user_agents()}
    missing_agents = [a for a in names["reused_agents"] if a not in have]
    checks += 1
    out: dict[str, Any] = {"checks": checks, "missing_tools": missing_tools, "missing_agents": missing_agents,
                           "redraft": bool(missing_tools or missing_agents) and checks == 1, "note": ""}
    if missing_tools:
        known = await _known_names(server_module)
        # Closer than F489's suggestions: a far match ("list_schedules" for `smtp_send_mail`) would steer the
        # drafter to a tool that does something else.
        near = {t: difflib.get_close_matches(t, known, n=3, cutoff=0.75) for t in missing_tools}
        out["nearest"] = {t: n for t, n in near.items() if n}
    if out["redraft"]:
        out["note"] = ("Nothing in this workspace provides " + ", ".join(f"`{t}`" for t in missing_tools + missing_agents)
                       + ". Redraft using only tools and agents that exist" + (
                           " (nearest: " + "; ".join(f"`{t}` → {', '.join(f'`{c}`' for c in n)}"
                                                    for t, n in out.get("nearest", {}).items()) + ")"
                           if out.get("nearest") else "")
                       + ". What has no real tool here becomes an Open question, said plainly, not a step.")
    out["section"] = ""
    if not out["redraft"] and (missing_tools or missing_agents):
        out["note"] = ("This plan uses " + ", ".join(f"`{t}`" for t in missing_tools + missing_agents)
                       + ", which nothing in this workspace provides yet. Connect the app or service it needs, or "
                       "tell me to leave that part out; approved as it is, the build will stop there.")
        # The plan card reads it as its own section (`plan.ts`), drawn as needing the person.
        out["section"] = "## Missing here\n" + out["note"]
    return out


CHECK_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "check_plan",
        "description": (
            "Check the plan this build has just drafted: every tool and reused agent it names, with the lookup a run "
            "makes. Reads the plan from the run itself and takes no arguments; call it with {}."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


async def check_plan_tool(args: dict, server_module: Any, events: list[dict]) -> dict:
    """The builder's `step_check_plan`: the plan and the checks so far come from the run's last checkpoint, which
    the engine takes at every step boundary, so nothing copies the plan through a model."""
    from core.builder_tools import _current_run
    from core.orchestration.state import SharedState

    run_id = _current_run.get()
    if run_id is None:
        return {"error": "check_plan runs inside a build."}
    state = (await SharedState.restore(run_id)).run.shared_state or {}
    prior = state.get("plan_check")
    checks = int(prior.get("checks") or 0) if isinstance(prior, dict) else 0
    return await check_plan(str(state.get("plan_draft") or ""), server_module, checks=checks)


def _register() -> None:
    from core.builder_tools import register_builder_tool

    register_builder_tool(CHECK_PLAN_SCHEMA, check_plan_tool)


_register()
