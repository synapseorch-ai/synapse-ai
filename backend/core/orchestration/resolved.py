"""
An agent and its tools, resolved once per run.

Every agent step used to pay the full resolution bill: the agent read from the
store (twice — once by the executor to check for an orchestrator, once by
`run_agent_step`), the tenant's global variables read again though the engine
already held them, the custom tools listed, and every MCP server's advertised
tools re-filtered, re-mapped and re-serialised into the three shapes the loop
needs. Per step, per delegate. None of it changes while a run is in flight.

`resolve_for_run` does that work once per agent per engine and hands
`run_agent_step` the result through `preresolved=`. That parameter is a cache,
not `tools_override`: the task tool, skills and memory tools are still injected
on top of what it returns, exactly as they would be after a fresh aggregation.

The lists are copied on the way out. `run_agent_step` appends the task tool and
the skill tool to `all_tools` and rewrites `ollama_tools`; a second step on the
same agent must start from the aggregated set, not from the first step's edits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import OrchestrationEngine


@dataclass
class ResolvedAgent:
    agent: dict
    custom_tools: list[dict]
    # None until a caller needs the aggregated tool set — a tool step needs the
    # agent and the custom tools but must not aggregate every server for it.
    all_tools: list | None = None
    tool_schema_map: dict | None = None
    ollama_tools: list | None = None
    tools_json: str | None = None

    @property
    def has_tools(self) -> bool:
        return self.all_tools is not None

    def tool_lists(self) -> tuple[list, dict, list, str]:
        """Fresh copies of the aggregated shapes, for one `run_agent_step`."""
        assert self.all_tools is not None, "tools not aggregated for this agent"
        return (
            list(self.all_tools),
            dict(self.tool_schema_map or {}),
            list(self.ollama_tools or []),
            self.tools_json or "",
        )


async def run_custom_tools(engine: "OrchestrationEngine") -> list[dict]:
    """The tenant's custom tools, listed once per run."""
    cached = getattr(engine, "_custom_tools", None)
    if cached is None:
        from core.scale.context import resolve_custom_tools
        cached = await resolve_custom_tools()
        engine._custom_tools = cached
    return cached


async def resolve_for_run(
    engine: "OrchestrationEngine", agent_id: str | None, *, tools: bool = True,
) -> ResolvedAgent | None:
    """The agent named by `agent_id`, resolved and cached on `engine` for the
    life of the run. Returns None when no such agent exists — the caller's
    own error path takes it from there, as it always has.

    `tools=True` also aggregates the agent's tool set (once; a later call with
    `tools=True` fills in an entry that was first resolved without them).
    """
    if not agent_id:
        return None
    cache: dict[str, ResolvedAgent] = engine.__dict__.setdefault("_resolved", {})
    entry = cache.get(agent_id)

    if entry is None:
        from core.scale.context import resolve_agent
        agent = await resolve_agent(agent_id)
        if agent is None:
            # The same fallback run_agent_step applies when the provider has
            # nothing: a deleted agent must fail the step, not resolve to
            # whichever agent sorts first.
            from core.react_engine import _resolve_agent_by_id
            try:
                agent = await _resolve_agent_by_id(agent_id)
            except Exception:
                agent = None
        if not agent:
            return None
        entry = ResolvedAgent(agent=agent, custom_tools=await run_custom_tools(engine))
        cache[agent_id] = entry

    if tools and not entry.has_tools:
        from core.tools import aggregate_all_tools
        all_tools, schema_map, ollama_tools, tools_json = await aggregate_all_tools(
            engine.server_module, entry.agent, entry.custom_tools
        )
        entry.all_tools = all_tools
        entry.tool_schema_map = schema_map
        entry.ollama_tools = ollama_tools
        entry.tools_json = tools_json

    return entry
