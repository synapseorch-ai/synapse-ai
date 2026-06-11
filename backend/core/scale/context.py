"""
Global worker context — set once in worker_main.py at process startup.

When IS_SCALE_WORKER is True, all resolver functions go to Postgres first
and fall back to local JSON only if nothing is found. Non-worker processes
(the main API server) leave IS_SCALE_WORKER=False, so all JSON paths are
used as before — no behaviour change for V1 mode.
"""
from __future__ import annotations

IS_SCALE_WORKER: bool = False
_session_factory = None


def set_session_factory(sf) -> None:
    global _session_factory
    _session_factory = sf


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

async def resolve_agent(agent_id: str) -> dict | None:
    """Return agent dict — Postgres first (worker), JSON fallback."""
    if IS_SCALE_WORKER and _session_factory and agent_id:
        try:
            from sqlalchemy import select
            from core.scale.models_db import AgentDB
            async with _session_factory() as session:
                result = await session.execute(
                    select(AgentDB).where(AgentDB.id == agent_id)
                )
                row = result.scalar_one_or_none()
            if row is not None:
                return row.definition
        except Exception as e:
            print(f"[scale.context] resolve_agent PG error: {e}", flush=True)

    # JSON fallback
    try:
        from core.routes.agents import load_user_agents
        agents = load_user_agents()
        return next((a for a in agents if a.get("id") == agent_id), None)
    except Exception:
        return None


async def resolve_custom_tools() -> list[dict]:
    """Return all custom tools — Postgres first (worker), JSON fallback."""
    if IS_SCALE_WORKER and _session_factory:
        try:
            from sqlalchemy import select
            from core.scale.models_db import ToolDB
            async with _session_factory() as session:
                result = await session.execute(select(ToolDB))
                rows = result.scalars().all()
            if rows:
                return [r.definition for r in rows]
        except Exception as e:
            print(f"[scale.context] resolve_custom_tools PG error: {e}", flush=True)

    # JSON fallback
    try:
        from core.routes.tools import load_custom_tools
        return load_custom_tools()
    except Exception:
        return []


async def resolve_mcp_servers() -> list[dict]:
    """Return MCP server configs — Postgres first (worker), JSON fallback."""
    if IS_SCALE_WORKER and _session_factory:
        try:
            from sqlalchemy import select
            from core.scale.models_db import MCPServerDB
            async with _session_factory() as session:
                result = await session.execute(select(MCPServerDB))
                rows = result.scalars().all()
            if rows:
                return [r.definition for r in rows]
        except Exception as e:
            print(f"[scale.context] resolve_mcp_servers PG error: {e}", flush=True)

    # JSON fallback
    try:
        from core.mcp_client import MCP_SERVERS_FILE
        import json
        with open(MCP_SERVERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []
