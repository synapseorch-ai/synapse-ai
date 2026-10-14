"""What the AI builder may not save: a tool that nothing provides (F489, BWA1, D274).

SW1 saw it in every Agent Builder translation, and VOX1e met it too: `create_agent` took any tool name — the pasted
code's own variables, a tool the model expected to exist — and saved the agent with it. At run time the agent silently
lacked the tool, and a forced `tool` step naming one failed ("Tool '…' not found in available tools"). The builder's
validator never looked.

Every name is checked with the engine's own resolver (`core.tools.resolve_one_tool`), the lookup a run makes, so
"exists" means exactly "a run could call it":

- `unknown(names)` — the names in a list that nothing provides; `create_agent(s)` and `update_agent` refuse them,
  with the nearest names that do exist, at the moment the builder writes them;
- `missing_in(workflows, agents)` — for `validate_orchestration`: a forced tool step naming one is an issue (it fails
  on the first run, certainly); an agent naming one is a warning, since a workspace's own older agents may name a
  tool from a server that has gone, and refusing every build that uses them would be a new failure.
"""
from __future__ import annotations

import difflib
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
