"""
Import / Export endpoints for orchestrations, agents, MCP servers, and custom tools.

Export:
  GET  /api/export/data   — fetch all entities (for UI cascade-selection)
  POST /api/export        — build sanitized downloadable bundle

Import:
  POST /api/import        — import a bundle with user-supplied secret values
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# --------------------------------------------------------------------------- #
# Resources come from the store, through the same functions the CRUD routes
# use. This module used to hold a second, independent set of JsonStore handles
# over three of the four collections — with their own caches, so it could and
# did disagree with core/routes/*.py about what was on disk.
# --------------------------------------------------------------------------- #

# The AI builder's own orchestration and sub-agents ship with the product, so
# they are never part of an export: carrying them would put a stale copy of the
# builder into whatever workspace imports the bundle, where seeding would then
# fight it on every restart.
async def _load_agents() -> List[dict]:
    from core.store.resources import load_agents
    return [a for a in await load_agents() if not a.get("system")]


async def _load_orchestrations() -> List[dict]:
    from core.store.resources import load_orchestrations
    return [o for o in await load_orchestrations() if not o.get("system")]


async def _load_custom_tools() -> List[dict]:
    from core.store.resources import load_tools
    return await load_tools()


async def _load_mcp_servers() -> List[dict]:
    from core.store.resources import load_mcp_servers
    return await load_mcp_servers()


# --------------------------------------------------------------------------- #
# GET /api/export/data
# --------------------------------------------------------------------------- #

@router.get("/api/export/data")
async def get_export_data():
    """
    Returns all entities needed for the export UI in a single call.
    MCP server env values and custom tool header values are replaced
    with empty strings so the UI can show keys without exposing secrets.
    """
    orchestrations = await _load_orchestrations()
    agents = await _load_agents()
    mcp_servers = _sanitize_mcp_servers(await _load_mcp_servers())
    custom_tools = _sanitize_custom_tools(await _load_custom_tools())

    return {
        "orchestrations": orchestrations,
        "agents": agents,
        "mcp_servers": mcp_servers,
        "custom_tools": custom_tools,
    }


# --------------------------------------------------------------------------- #
# POST /api/export
# --------------------------------------------------------------------------- #

class ExportRequest(BaseModel):
    orchestration_ids: List[str] = []
    agent_ids: List[str] = []
    mcp_server_names: List[str] = []
    custom_tool_names: List[str] = []


@router.post("/api/export")
async def export_bundle(req: ExportRequest):
    """
    Build and return a sanitized export bundle as JSON.
    - MCP server env: keys kept, values redacted to ""
    - Custom HTTP tool headers: keys kept, values redacted to ""
    - Python tools: included as-is; has_python_tools flag set to True
    """
    all_agents = await _load_agents()
    all_orchs = await _load_orchestrations()
    all_tools = await _load_custom_tools()
    all_mcp = await _load_mcp_servers()

    # --- Filter selected entities ---
    selected_orchs = [o for o in all_orchs if o["id"] in req.orchestration_ids]
    selected_agents = [a for a in all_agents if a["id"] in req.agent_ids]
    selected_tools_raw = [t for t in all_tools if t["name"] in req.custom_tool_names]
    selected_mcp_raw = [m for m in all_mcp if m["name"] in req.mcp_server_names]

    # --- Sanitize secrets ---
    selected_mcp = _sanitize_mcp_servers(selected_mcp_raw)
    selected_tools = _sanitize_custom_tools(selected_tools_raw)

    has_python_tools = any(t.get("tool_type") == "python" for t in selected_tools)

    bundle = {
        "synapse_export": True,
        "version": "1.0",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "has_python_tools": has_python_tools,
        "orchestrations": selected_orchs,
        "agents": selected_agents,
        "mcp_servers": selected_mcp,
        "custom_tools": selected_tools,
    }

    return bundle


# --------------------------------------------------------------------------- #
# POST /api/import
# --------------------------------------------------------------------------- #

class ImportRequest(BaseModel):
    bundle: Dict[str, Any]
    # Secrets provided by the user for redacted fields
    # Format: { "mcp_server_name": { "ENV_KEY": "value" }, ... }
    mcp_secrets: Dict[str, Dict[str, str]] = {}
    # Format: { "tool_name": { "Header-Key": "value" }, ... }
    tool_secrets: Dict[str, Dict[str, str]] = {}
    # Format: { "mcp_server_name": "actual_token_value", ... }
    mcp_tokens: Dict[str, str] = {}
    # Which entity IDs/names the user selected to import (subset of bundle)
    selected_orchestration_ids: List[str] = []
    selected_agent_ids: List[str] = []
    selected_mcp_server_names: List[str] = []
    selected_custom_tool_names: List[str] = []


@router.post("/api/import")
async def import_bundle(req: ImportRequest):
    """
    Import a Synapse export bundle.
    - Validates bundle format
    - Orchestrations / Agents / Custom Tools: upserted by id/name
    - MCP Servers: skipped if name already exists
    - Merges user-provided secrets back into env/headers before saving
    """
    bundle = req.bundle

    # --- Validate bundle ---
    if not bundle.get("synapse_export"):
        raise HTTPException(status_code=400, detail="Not a valid Synapse export bundle.")

    results: Dict[str, List[Dict[str, str]]] = {
        "orchestrations": [],
        "agents": [],
        "mcp_servers": [],
        "custom_tools": [],
    }

    # ── Orchestrations ─────────────────────────────────────────────────────────
    existing_orchs = await _load_orchestrations()
    existing_orch_ids = {o["id"] for o in existing_orchs}
    orch_changed = False

    for orch in bundle.get("orchestrations", []):
        oid = orch.get("id")
        if not oid or oid not in req.selected_orchestration_ids:
            continue
        # Upsert
        existing_orchs = [o for o in existing_orchs if o["id"] != oid]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        orch = dict(orch)
        if oid not in existing_orch_ids:
            orch["created_at"] = now
        orch["updated_at"] = now
        existing_orchs.append(orch)
        orch_changed = True
        results["orchestrations"].append({"id": oid, "name": orch.get("name", oid), "status": "imported"})

    if orch_changed:
        from core.store.resources import save_orchestration
        for o in existing_orchs:
            await save_orchestration(o)

    # ── Agents ─────────────────────────────────────────────────────────────────
    existing_agents = await _load_agents()
    agent_changed = False

    for agent in bundle.get("agents", []):
        aid = agent.get("id")
        if not aid or aid not in req.selected_agent_ids:
            continue
        existing_agents = [a for a in existing_agents if a["id"] != aid]
        existing_agents.append(dict(agent))
        agent_changed = True
        results["agents"].append({"id": aid, "name": agent.get("name", aid), "status": "imported"})

    if agent_changed:
        from core.store.resources import save_agent
        for a in existing_agents:
            await save_agent(a)

    # ── MCP Servers ─────────────────────────────────────────────────────────────
    existing_mcp = await _load_mcp_servers()
    existing_mcp_names = {m["name"] for m in existing_mcp}
    mcp_changed = False

    for mcp in bundle.get("mcp_servers", []):
        mname = mcp.get("name")
        if not mname or mname not in req.selected_mcp_server_names:
            continue

        if mname in existing_mcp_names:
            results["mcp_servers"].append({
                "name": mname,
                "label": mcp.get("label", mname),
                "status": "skipped_existing",
                "message": "Already exists — will attempt connection",
            })
            continue

        # Merge user-supplied secrets into env and token
        mcp_entry = dict(mcp)
        if "server_type" not in mcp_entry:
            mcp_entry["server_type"] = "remote" if mcp_entry.get("url") else "stdio"
        user_secrets = req.mcp_secrets.get(mname, {})
        if user_secrets and isinstance(mcp_entry.get("env"), dict):
            merged_env = {k: user_secrets.get(k, "") for k in mcp_entry["env"].keys()}
            mcp_entry["env"] = merged_env
        user_token = req.mcp_tokens.get(mname, "")
        if user_token:
            mcp_entry["token"] = user_token
        elif mcp_entry.get("token") == "xxxxxxxxx":
            mcp_entry["token"] = ""
        mcp_entry["status"] = "disconnected"
        existing_mcp.append(mcp_entry)
        mcp_changed = True
        results["mcp_servers"].append({
            "name": mname,
            "label": mcp.get("label", mname),
            "status": "imported",
            "message": "Saved — connecting…",
        })

    if mcp_changed:
        from core.store.resources import save_mcp_server
        for m in existing_mcp:
            await save_mcp_server(m)

    # Always sync manager with authoritative disk state and attempt connections
    # for all selected servers (covers both newly imported and previously saved
    # but not-yet-in-memory cases that happen after a server restart).
    import core.server as _server
    if _server.mcp_manager and req.selected_mcp_server_names:
        _server.mcp_manager.servers_config = await _load_mcp_servers()

        for mcp_result in results["mcp_servers"]:
            if mcp_result["status"] not in ("imported", "skipped_existing"):
                continue
            name = mcp_result["name"]

            # Skip servers that are already connected in this session
            if name in _server.mcp_manager.sessions:
                mcp_result["status"] = "connected"
                mcp_result["message"] = "Already connected"
                continue

            config = next((m for m in _server.mcp_manager.servers_config if m["name"] == name), None)
            if not config:
                continue

            server_type = config.get("server_type", "stdio")
            session = None

            if server_type == "stdio":
                session = await _server.mcp_manager.connect_stdio_server(config)
            elif config.get("token"):
                session = await _server.mcp_manager.connect_remote_server(config)
            else:
                # remote without token → trigger OAuth, surface auth_url to frontend
                import asyncio
                loop = asyncio.get_event_loop()
                auth_url_future: asyncio.Future = loop.create_future()
                from core.background import spawn_background
                spawn_background(_server.mcp_manager.start_oauth_connect(config, auth_url_future),
                                 name="mcp-oauth-connect")
                try:
                    auth_url = await asyncio.wait_for(asyncio.shield(auth_url_future), timeout=15.0)
                    mcp_result["status"] = "oauth_pending"
                    mcp_result["auth_url"] = auth_url
                    mcp_result["message"] = "Authorization required — click Authorize to connect"
                except asyncio.TimeoutError:
                    mcp_result["status"] = "disconnected"
                    mcp_result["message"] = "Saved — use 'Retry' in MCP Servers to connect"
                continue  # session check below doesn't apply for OAuth path

            if session:
                await _server.mcp_manager._set_status(name, "connected")
                await _server.mcp_manager._auto_register(name)
                mcp_result["status"] = "connected"
                mcp_result["message"] = "Connected successfully"
            else:
                await _server.mcp_manager._set_status(name, "disconnected")
                mcp_result["status"] = "disconnected"
                mcp_result["message"] = "Saved — use 'Retry' in MCP Servers to connect"

    # ── Custom Tools ────────────────────────────────────────────────────────────
    existing_tools = await _load_custom_tools()
    tool_changed = False

    for tool in bundle.get("custom_tools", []):
        tname = tool.get("name")
        if not tname or tname not in req.selected_custom_tool_names:
            continue

        # Merge user-supplied header secrets (HTTP tools)
        tool_entry = dict(tool)
        user_secrets = req.tool_secrets.get(tname, {})
        if user_secrets and isinstance(tool_entry.get("headers"), dict):
            merged_headers = {k: user_secrets.get(k, "") for k in tool_entry["headers"].keys()}
            tool_entry["headers"] = merged_headers

        existing_tools = [t for t in existing_tools if t["name"] != tname]
        existing_tools.append(tool_entry)
        tool_changed = True
        results["custom_tools"].append({
            "name": tname,
            "label": tool.get("generalName", tname),
            "status": "imported",
        })

    if tool_changed:
        from core.store.resources import save_tools
        await save_tools(existing_tools)

    return {"status": "success", "results": results}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _sanitize_mcp_servers(servers: List[dict]) -> List[dict]:
    """Replace MCP server env values with empty strings (keep keys). Redact token with placeholder."""
    sanitized = []
    for s in servers:
        entry = dict(s)
        if isinstance(entry.get("env"), dict):
            entry["env"] = {k: "" for k in entry["env"].keys()}
        if entry.get("token"):
            entry["token"] = "xxxxxxxxx"
        sanitized.append(entry)
    return sanitized


def _sanitize_custom_tools(tools: List[dict]) -> List[dict]:
    """Replace HTTP tool header values with empty strings (keep keys)."""
    sanitized = []
    for t in tools:
        entry = dict(t)
        if isinstance(entry.get("headers"), dict):
            entry["headers"] = {k: "" for k in entry["headers"].keys()}
        sanitized.append(entry)
    return sanitized
