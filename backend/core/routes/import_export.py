"""
Import / Export endpoints for orchestrations, agents, MCP servers, and custom tools.

Export:
  GET  /api/export/data   — fetch all entities (for UI cascade-selection)
  POST /api/export        — build sanitized downloadable bundle

Import:
  POST /api/import        — import a bundle with user-supplied secret values

One file for both products (F464, P-2): this install and SynapseOrch Cloud both
write `{synapse_export, version "1.1", exported_from, exported_at, orchestrations,
agents, mcp_servers, custom_tools}`, and both read `1.0` too. This importer also
reads the file Cloud wrote before that envelope (`version: 1`, its tools under
`tools`, workflows without ids): a workflow with no id is keyed by its place in
the file, `#<index>`, and gets a new id here. What arrives but can't run here (a
model this install can't serve, a tool it doesn't have, a workflow or agent the
file doesn't carry) is imported anyway and listed in `warnings` (Naveen, D266).
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

#: The envelope's version. "1.1" added `exported_from`; "1.0" files still import.
BUNDLE_VERSION = "1.1"

#: Said of each of Synapse AI's own agents and its workflow a file carries (F1373; cloud's import says the same,
#: F1372): the product's, never the person's to import.
SYNAPSE_AIS_OWN = ("One of Synapse AI's own, from the install this file came from, so it isn't imported: "
                   "this workspace has its own Synapse AI.")


def _synapse_ais_own(kind: str, entity: dict) -> bool:
    """Whether a file's agent or workflow is the builder's own, of any release, by its id alone (F1351)."""
    from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID, is_builder_agent

    return is_builder_agent(entity.get("id")) if kind == "agents" else entity.get("id") == NATIVE_BUILDER_ORCH_ID

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
        "version": BUNDLE_VERSION,
        "exported_from": "self-host",
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
    bundle = normalize_bundle(req.bundle)
    if bundle is None:
        raise HTTPException(status_code=400, detail="Not a valid Synapse export bundle.")

    from core.store.ids import new_id
    from core.store.resources import save_agent, save_orchestration

    results: Dict[str, List[Dict[str, str]]] = {
        "orchestrations": [],
        "agents": [],
        "mcp_servers": [],
        "custom_tools": [],
    }

    # ── Orchestrations ─────────────────────────────────────────────────────────
    # Each one saved on its own: the store upserts by id, so re-saving every
    # workflow the install already had (as this did) bought nothing.
    existing_orch_ids = {o["id"] for o in await _load_orchestrations()}
    selected_orchs = set(req.selected_orchestration_ids)
    imported_orchs: List[dict] = []

    picked: list[dict] = []
    # Synapse AI's own, from the install the file came from (F1373): never written, so never over the builder's
    # own; each named. A step of the file's that used one says it isn't here (`_warnings`).
    left_out: List[Dict[str, str]] = []
    for index, orch in enumerate(bundle["orchestrations"]):
        if not isinstance(orch, dict) or bundle_key(orch, index) not in selected_orchs:
            continue
        if _synapse_ais_own("orchestrations", orch):
            left_out.append(_warning("orchestrations", orch, SYNAPSE_AIS_OWN))
            continue
        orch = dict(orch)
        oid = orch.get("id")
        if not oid or str(oid).startswith("#"):
            # Cloud's file before the envelope carried no workflow ids.
            oid = orch["id"] = new_id("orch")
        picked.append(orch)

    # A workflow the engine would refuse to run as written refuses the whole
    # file, before anything is saved (F850, F853): the bundle's agents count,
    # as they arrive with it.
    from core.routes.orchestrations import _refuse_to_save

    arriving = {a["id"]: a for a in bundle["agents"]
                if isinstance(a, dict) and a.get("id") in set(req.selected_agent_ids)
                and not _synapse_ais_own("agents", a)}
    for orch in picked:
        await _refuse_to_save(orch, arriving)

    for orch in picked:
        oid = orch["id"]
        replaced = oid in existing_orch_ids
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not replaced:
            orch["created_at"] = now
        orch["updated_at"] = now
        await save_orchestration(orch)
        imported_orchs.append(orch)
        results["orchestrations"].append(
            {"id": oid, "name": orch.get("name", oid), "status": _status(replaced)})

    # ── Agents ─────────────────────────────────────────────────────────────────
    existing_agent_ids = {a["id"] for a in await _load_agents()}
    selected_agents = set(req.selected_agent_ids)
    imported_agents: List[dict] = []

    for agent in bundle["agents"]:
        aid = agent.get("id") if isinstance(agent, dict) else None
        if not aid or aid not in selected_agents:
            continue
        if _synapse_ais_own("agents", agent):
            left_out.append(_warning("agents", agent, SYNAPSE_AIS_OWN))
            continue
        await save_agent(dict(agent))
        imported_agents.append(agent)
        results["agents"].append({"id": aid, "name": agent.get("name", aid),
                                  "status": _status(aid in existing_agent_ids)})

    # ── MCP Servers ─────────────────────────────────────────────────────────────
    existing_mcp = await _load_mcp_servers()
    existing_mcp_names = {m["name"] for m in existing_mcp}

    for mcp in bundle["mcp_servers"]:
        mname = mcp.get("name") if isinstance(mcp, dict) else None
        if not mname or mname not in req.selected_mcp_server_names:
            continue

        if mcp.get("platform"):
            # A Cloud connected app (Google, …): its address is Cloud's own
            # service, which nothing here can reach.
            results["mcp_servers"].append({
                "name": mname,
                "label": mcp.get("label", mname),
                "status": "skipped",
                "message": "A Cloud connected app; connect it from this install's own settings.",
            })
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
        from core.store.resources import save_mcp_server
        await save_mcp_server(mcp_entry)
        results["mcp_servers"].append({
            "name": mname,
            "label": mcp.get("label", mname),
            "status": "imported",
            "message": "Saved — connecting…",
        })

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
                # remote without token → connected, or a URL to authorise at (U61):
                # a public server used to wait out the 15 s and read "Retry"
                outcome = await _server.mcp_manager.connect_or_authorise(config)
                if outcome["connected"]:
                    mcp_result["status"] = "connected"
                    mcp_result["message"] = "Connected successfully"
                elif outcome["status"] == "oauth_pending":
                    mcp_result["status"] = "oauth_pending"
                    mcp_result["auth_url"] = outcome["auth_url"]
                    mcp_result["message"] = "Authorization required — click Authorize to connect"
                elif outcome["status"] == "connecting":
                    mcp_result["status"] = "disconnected"
                    mcp_result["message"] = "Saved — still connecting; it will show as connected in MCP Servers"
                else:
                    mcp_result["status"] = "disconnected"
                    mcp_result["message"] = (f"Saved, but it did not connect: {outcome['error']}. "
                                             "Use Retry in MCP Servers")
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
    existing_tool_names = {t.get("name") for t in await _load_custom_tools()}
    imported_tools: List[dict] = []

    for tool in bundle["custom_tools"]:
        tname = tool.get("name") if isinstance(tool, dict) else None
        if not tname or tname not in req.selected_custom_tool_names:
            continue

        # Merge user-supplied header secrets (HTTP tools)
        tool_entry = dict(tool)
        user_secrets = req.tool_secrets.get(tname, {})
        if user_secrets and isinstance(tool_entry.get("headers"), dict):
            merged_headers = {k: user_secrets.get(k, "") for k in tool_entry["headers"].keys()}
            tool_entry["headers"] = merged_headers

        # A file's tags are a claim (TAG1): believed when they restrict, until a person saves the tool.
        from core.tool_tags import IMPORT_HINTS, clean_hints
        if isinstance(tool_entry.get("annotations"), dict):
            tool_entry["annotations"] = clean_hints(tool_entry["annotations"])
            tool_entry["annotations_by"] = tool_entry.get("annotations_by") or IMPORT_HINTS
        else:
            tool_entry.pop("annotations", None)
            tool_entry.pop("annotations_by", None)

        imported_tools.append(tool_entry)
        results["custom_tools"].append({
            "name": tname,
            "label": tool.get("generalName") or tname,
            "status": _status(tname in existing_tool_names),
        })

    if imported_tools:
        # Upserts by name, leaving every other tool alone.
        from core.store.resources import save_tools
        await save_tools(imported_tools)

    warnings = [*left_out, *await _warnings(imported_orchs, imported_agents, [t["name"] for t in imported_tools])]
    return {"status": "success", "results": results, "warnings": warnings}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def normalize_bundle(bundle: Any) -> Optional[dict]:
    """The bundle as this importer reads it, or None when it isn't one.

    Three shapes arrive: the envelope both products write (`synapse_export`,
    "1.0" or "1.1"), and the file Cloud wrote before it (`version: 1`, an
    integer, with its tools under `tools`).
    """
    if not isinstance(bundle, dict):
        return None
    if bundle.get("synapse_export"):
        out = dict(bundle)
    elif type(bundle.get("version")) is int and bundle["version"] == 1 and any(
        isinstance(bundle.get(k), list) for k in ("orchestrations", "agents", "tools", "mcp_servers")
    ):
        out = {**bundle, "synapse_export": True, "exported_from": "cloud"}
    else:
        return None
    if not isinstance(out.get("custom_tools"), list) and isinstance(out.get("tools"), list):
        out["custom_tools"] = out["tools"]
    for key in ("orchestrations", "agents", "mcp_servers", "custom_tools"):
        if not isinstance(out.get(key), list):
            out[key] = []
    return out


def bundle_key(orch: dict, index: int) -> str:
    """How the import screen and a request name a workflow: its id, or `#<index>`
    for one the file carries without an id."""
    return str(orch.get("id") or f"#{index}")


def _status(replaced: bool) -> str:
    return "replaced" if replaced else "imported"


def _warning(kind: str, entity: dict, message: str) -> Dict[str, str]:
    return {"kind": kind, "id": str(entity.get("id") or ""),
            "name": str(entity.get("name") or entity.get("id") or ""), "message": message}


async def _available_tool_names() -> set:
    """Every tool name this install offers right now: its running servers' and
    its custom tools (the agent editor's picker, `/api/tools/available`)."""
    try:
        from core.routes.tools import get_available_tools
        return {t.get("name") for t in (await get_available_tools()).get("tools", []) if t.get("name")}
    except Exception:  # noqa: BLE001 — a warning list is never worth failing an import
        return set()


async def _warnings(orchs: List[dict], agents: List[dict], tool_names: List[str]) -> List[Dict[str, str]]:
    """What was imported but can't run here yet, one sentence each (F464).

    Nothing is refused for these: a self-hosted Ollama agent is still worth
    having in Cloud, and the other way round, once someone picks a model.
    """
    out: List[Dict[str, str]] = []
    agent_ids = {a["id"] for a in await _load_agents()}
    orch_ids = {o["id"] for o in await _load_orchestrations()}

    for agent in agents:
        target = agent.get("orchestration_id")
        if agent.get("type") == "orchestrator" and target and target not in orch_ids:
            out.append(_warning("agents", agent, f"It runs the workflow {target}, which isn't in this "
                                                 "file or on this install. Open it and pick its workflow."))
        for other in agent.get("delegate_agent_ids") or []:
            if other not in agent_ids:
                out.append(_warning("agents", agent, f"It hands work to the agent {other}, which isn't "
                                                     "in this file or on this install."))
    for orch in orchs:
        for step in orch.get("steps") or []:
            if isinstance(step, dict) and step.get("type") == "transform" and str(step.get("transform_code") or "").strip():
                # Code from a file: said, so it is read before it runs (F968). It runs with no network on the
                # sandbox; on a server started with the host runtime, as the server.
                out.append(_warning("orchestrations", orch,
                                    f"Its step {step.get('name') or step.get('id')} runs Python from this file. "
                                    "Read it before running the workflow."))
            used = step.get("agent_id") if isinstance(step, dict) else None
            if used and used not in agent_ids:
                out.append(_warning("orchestrations", orch,
                                    f"Its step {step.get('name') or step.get('id')} uses the agent {used}, "
                                    "which isn't in this file or on this install."))
            runs = step.get("workflow_id") if isinstance(step, dict) and step.get("type") == "workflow" else None
            if runs and runs not in orch_ids:
                out.append(_warning("orchestrations", orch,
                                    f"Its step {step.get('name') or step.get('id')} runs the workflow {runs}, "
                                    "which isn't in this file or on this install. Open it and pick its workflow."))

    # Models: imported as they are, and named when this install can't serve them.
    uses: Dict[str, List[tuple]] = {}
    for agent in agents:
        if agent.get("model"):
            uses.setdefault(str(agent["model"]), []).append(("agents", agent))
    for orch in orchs:
        for model in dict.fromkeys(str(s["model"]) for s in orch.get("steps") or []
                                   if isinstance(s, dict) and s.get("model")):
            uses.setdefault(model, []).append(("orchestrations", orch))
    if uses:
        try:
            from core.config import load_settings
            from core.model_readiness import model_problems
            problems = await model_problems(dict(load_settings()), list(uses))
        except Exception:  # noqa: BLE001
            problems = {}
        for model, problem in problems.items():
            for kind, entity in uses.get(model, []) if problem else []:
                out.append(_warning(kind, entity, f"Its model {model} can't answer on this install: "
                                                  f"{problem} Pick a model for it."))

    # Tools: an agent's tool this install doesn't have, now or in this file.
    available = await _available_tool_names() | set(tool_names)
    servers = {m.get("name") for m in await _load_mcp_servers()}
    for agent in agents:
        for tool in agent.get("tools") or []:
            if (tool == "all" or str(tool).startswith("group:") or tool in available
                    or ("__" in str(tool) and str(tool).split("__", 1)[0] in servers)):
                continue
            out.append(_warning("agents", agent, f"Its tool {tool} isn't available on this install."))

    unique: Dict[tuple, Dict[str, str]] = {}
    for w in out:
        unique.setdefault((w["kind"], w["id"], w["message"]), w)
    return list(unique.values())


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
    """Replace HTTP tool header values with empty strings (keep keys).

    Who wrote a tool's tags (`annotations_by`) stays behind: it says whom this workspace believes, and the importer
    marks every tag a file brings as the file's (TAG1).
    """
    sanitized = []
    for t in tools:
        entry = dict(t)
        entry.pop("annotations_by", None)
        if isinstance(entry.get("headers"), dict):
            entry["headers"] = {k: "" for k in entry["headers"].keys()}
        sanitized.append(entry)
    return sanitized
