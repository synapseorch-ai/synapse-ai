"""
Which MCP servers a turn can reach, worked out before the pool connects them.

The fleet's pool used to connect every server an org has before any job could
start, whatever the job was. A phone call's first answer waited 12.9 s, ten of
them for three servers its agent never uses (VOX-MCP, D262). So a job now says
what its turn may call, and the pool connects only those servers
(`mcp_pool.needing`, `WorkerServerModule.ensure`).

The answer is a set of **tool names**, or `EVERY` when it cannot be narrowed.
Names rather than servers, because the org's server list is the pool's to read:
the module maps a name to every server it could belong to
(`servers_for`), and never by splitting the string. OSS lets a server name
contain the separator.

When in doubt, every server: a wrong guess here is an agent that silently lacks
a tool. So every server for these:

* no agent, or one that cannot be read;
* a workflow agent (`orchestrator`) or a `builder`, whose steps and sub-agents
  name their own tools;
* `"all"`;
* a builder tool that lists the whole session set
  (`builder_tools.READS_EVERY_SESSION`);
* a `delegate` that may hand work to any agent.

A delegate with a list needs its agents' servers too. They run in the same turn
on the same module.

**A visitor's turn is bounded by the visitor's list, whatever the agent.** F280
holds every agent and step such a turn reaches to it (`core/visitor.py`), so the
list is an upper bound even for a workflow agent (Naveen, D262).

The native servers are not named here. They are part of every module.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Iterable

from core.log import get_logger

_log = get_logger(__name__)

#: Every server the org has: today's behaviour, and the answer when in doubt.
EVERY = None

#: How deep a chain of delegates is followed before giving up and taking every
#: server. The engine never delegates to a delegate, so one level is all a real
#: agent uses; the bound is for a definition that says otherwise.
_MAX_DEPTH = 2


def servers_for(tools: Iterable[str] | None, servers: Iterable[str]) -> list[str]:
    """The servers among `servers`, in their order, that `tools` could name.

    A tool `{server}__{tool}` names its server by prefix. Every server that
    matches is taken, so `a__b__c` takes both `a` and `a__b` when both exist;
    connecting one server too many is a cost, missing one is a lost tool.
    """
    from core.tool_router import SEP

    servers = list(servers)
    if tools is None:
        return servers
    names = [t for t in tools if isinstance(t, str)]
    return [s for s in servers if any(t.startswith(f"{s}{SEP}") for t in names)]


async def of_agent(
    agent: dict | None,
    *,
    resolve: Callable[[str], Awaitable[dict | None]],
    _depth: int = 0,
) -> "frozenset[str] | None":
    """The tool names `agent` may call in a member's turn, or `EVERY`."""
    from core.builder_tools import READS_EVERY_SESSION
    from core.tools import effective_allowed_tools

    if not isinstance(agent, dict):
        return EVERY
    kind = agent.get("type") or "conversational"
    if kind in ("orchestrator", "builder"):
        return EVERY
    # With the type's defaults, as the engine aggregates them, in case a
    # default is ever a server's tool. Today they are all native.
    allowed = effective_allowed_tools(agent)
    if "all" in allowed:
        return EVERY
    names = {name for name in allowed if isinstance(name, str)}
    if names & READS_EVERY_SESSION:
        return EVERY
    if kind == "delegate":
        ids = agent.get("delegate_agent_ids") or []
        if not ids or _depth >= _MAX_DEPTH:
            return EVERY
        for agent_id in ids:
            target = await resolve(agent_id)
            if target is None:
                continue  # gone: the engine offers only agents that exist
            theirs = await of_agent(target, resolve=resolve, _depth=_depth + 1)
            if theirs is EVERY:
                return EVERY
            names |= theirs
    return frozenset(names)


def bounded(needs: "frozenset[str] | None", visitor) -> "frozenset[str] | None":
    """`needs` held to a visitor's list (`None` is a member's turn)."""
    from core.builder_tools import READS_EVERY_SESSION

    if visitor is None:
        return needs
    allowed = frozenset(visitor.tools) if needs is EVERY else frozenset(visitor.tools) & needs
    if allowed & READS_EVERY_SESSION:
        return EVERY
    return allowed


async def for_turn(agent_id: str | None, visitor=None) -> "frozenset[str] | None":
    """What a turn of `agent_id` may call, for `mcp_pool.needing`. Never raises.

    `visitor` is a `VisitorScope`, its `to_dict()` (the fleet job's kwarg), or
    `None` for a member. Anything that goes wrong here is every server, which is
    what the pool did before this existed.
    """
    from core.scale.context import resolve_agent

    try:
        agent = await resolve_agent(agent_id) if agent_id else None
    except Exception as exc:  # noqa: BLE001 — every server is the safe answer
        _log.debug(f"[mcp_needs] every server for agent '{agent_id}': {exc}")
        return EVERY
    return await for_agent(agent, visitor)


async def for_agent(agent: dict | None, visitor=None) -> "frozenset[str] | None":
    """`for_turn` for an agent already read, as the voice service has it. Never raises."""
    from core import visitor as _visitor
    from core.scale.context import resolve_agent

    name = agent.get("id") if isinstance(agent, dict) else None
    try:
        scope = visitor if isinstance(visitor, _visitor.VisitorScope) else _visitor.VisitorScope.from_dict(visitor)
        needs = bounded(await of_agent(agent, resolve=resolve_agent), scope)
    except Exception as exc:  # noqa: BLE001 — every server is the safe answer
        _log.debug(f"[mcp_needs] every server for agent '{name}': {exc}")
        return EVERY
    _log.debug(
        f"[mcp_needs] agent '{name}': "
        + ("every server" if needs is EVERY else f"{len(needs)} tool name(s)")
    )
    return needs
