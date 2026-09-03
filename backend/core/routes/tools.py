"""
Custom tools and MCP server management endpoints.
"""
import json
import os
import asyncio
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.models import AddMCPServerRequest
from core.openapi_import import parse_openapi_spec
import core.mcp_oauth_state as oauth_state

router = APIRouter()

async def load_custom_tools():
    from core.store.resources import load_tools
    return await load_tools()


async def save_custom_tools(tools):
    """Replace this tenant's custom tools with `tools`. Bulk only — see below."""
    from core.store.resources import replace_tools
    await replace_tools(tools)


@router.get("/api/tools/custom")
async def get_custom_tools():
    return await load_custom_tools()


_NATIVE_SERVER_LABELS: dict[str, str] = {
    "code_vault_search": "Code Search",
    "time": "Time & Date",
    "sql": "SQL Database",
    "personal_details": "Personal Details",
    "collect_data": "Collect Data",
    "pdf_parser": "PDF Parser",
    "xlsx_parser": "Excel / XLSX",
    "vault_sandbox": "Vault Sandbox",
    "web_scraper": "Web Scraper",
    "bash": "Bash",
    "Filesystem": "Filesystem",
}


@router.get("/api/tools/available")
async def get_available_tools():
    """List all available tools from all sources (Native Agents, External MCP, Custom HTTP)"""
    import core.server as _server

    # Native engine tools appear first so they show above MCP and custom tools
    from core.spawn_subtask import SPAWN_SUBTASK_STATIC_SCHEMA
    from core.task_list import TASK_LIST_STATIC_SCHEMA
    all_tools = [TASK_LIST_STATIC_SCHEMA, SPAWN_SUBTASK_STATIC_SCHEMA]

    # 1. Active MCP Sessions (Native + External)
    for name, session in list(_server.agent_sessions.items()):
        try:
            is_external = name.startswith("ext_mcp_")
            server_name = name[len("ext_mcp_"):] if is_external else name
            tool_type = "mcp_external" if is_external else "mcp_native"

            # Look up human-friendly label from stored config
            if is_external and _server.mcp_manager:
                cfg = _server.mcp_manager.get_server_config(server_name)
                display_label = (cfg.get("label") or server_name) if cfg else server_name
            else:
                display_label = _NATIVE_SERVER_LABELS.get(server_name, server_name)

            result = await session.list_tools()
            for t in result.tools:
                all_tools.append({
                    "name": f"{server_name}__{t.name}" if is_external else t.name,
                    "description": t.description,
                    "source": server_name,
                    "source_label": display_label,
                    "type": tool_type,
                    "schema": t.inputSchema
                })
        except Exception as e:
            print(f"Error listing tools for agent '{name}': {e}")

    # 2. Custom HTTP + Python Tools
    try:
        custom_tools = await load_custom_tools()
        group_names = {g["id"]: g.get("name", g["id"]) for g in await _load_groups()}
        for t in custom_tools:
            tool_type = t.get("tool_type", "http")
            group = t.get("group") if t.get("group") in group_names else None
            all_tools.append({
                "name": t.get("name"),
                "label": t.get("generalName", t.get("name")),
                "description": t.get("description", ""),
                "source": "custom_http" if tool_type != "python" else "custom_python",
                "type": tool_type,
                "schema": t.get("inputSchema") or t.get("schema"),
                # The picker shows one row per group; an agent may take the
                # whole group as `group:<id>` (core/tools.py).
                "group": group,
                "group_label": group_names.get(group) if group else None,
            })
    except Exception as e:
        print(f"Error listing custom tools: {e}")

    return {"tools": all_tools}


async def _upsert_custom_tools(new_tools: list[dict]) -> None:
    """Upsert tools by name, leaving every other tool alone.

    Was read-list / mutate / write-list, which loses a concurrent edit and
    rewrites every row to change one.
    """
    from core.store.resources import save_tools
    await save_tools(new_tools)


@router.post("/api/tools/custom")
async def create_custom_tool(tool: dict):
    await _upsert_custom_tools([tool])
    return {"status": "success", "tool": tool}


@router.post("/api/tools/custom/bulk")
async def create_custom_tools_bulk(payload: dict):
    """Upsert many tools at once. Body: {"tools": [ {tool}, ... ]}."""
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        raise HTTPException(status_code=400, detail="Body must include a non-empty 'tools' list.")
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            raise HTTPException(status_code=400, detail="Each tool must be an object with a 'name'.")
    await _upsert_custom_tools(tools)
    return {"status": "success", "imported": len(tools)}


# ── Tool groups ───────────────────────────────────────────────────────────────
#
# A group is a small document — name, description — and a tool carries its
# group's id in `group`. An agent may list `group:<id>` in `tools` and gets
# every tool in it (core/tools.py). The OpenAPI import makes one group per
# import, so fifty endpoints are one row in the picker rather than fifty.

TOOL_GROUPS = "tool_groups"


class ToolGroupBody(BaseModel):
    name: str
    description: str = ""


def _group_id(name: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_") or "group"
    return f"grp_{slug}"[:80]


async def _load_groups() -> list[dict]:
    from core.store import collections
    return [g for g in await collections.load(TOOL_GROUPS) if isinstance(g, dict) and g.get("id")]


async def _save_groups(groups: list[dict]) -> None:
    # `collections.save` replaces the whole collection with the list it is
    # given, so every write here goes through the full list.
    from core.store import collections
    await collections.save(TOOL_GROUPS, groups)


async def _upsert_group(group: dict) -> dict:
    groups = [g for g in await _load_groups() if g["id"] != group["id"]]
    await _save_groups([*groups, group])
    return group


@router.get("/api/tools/groups")
async def list_tool_groups():
    return await _load_groups()


@router.post("/api/tools/groups")
async def create_tool_group(body: ToolGroupBody):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="A group needs a name.")
    return await _upsert_group(
        {"id": _group_id(body.name), "name": body.name.strip(), "description": body.description.strip()}
    )


@router.put("/api/tools/groups/{group_id}")
async def update_tool_group(group_id: str, body: ToolGroupBody):
    existing = next((g for g in await _load_groups() if g["id"] == group_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="No such group.")
    return await _upsert_group(
        {**existing, "name": body.name.strip() or existing["name"], "description": body.description.strip()}
    )


@router.delete("/api/tools/groups/{group_id}")
async def delete_tool_group(group_id: str):
    """Delete the group; its tools stay, ungrouped."""
    from core.store.resources import load_tools, save_tools
    await _save_groups([g for g in await _load_groups() if g["id"] != group_id])
    orphaned = [{**t, "group": None} for t in await load_tools() if t.get("group") == group_id]
    if orphaned:
        await save_tools(orphaned)
    return {"status": "deleted", "ungrouped": len(orphaned)}


class OpenAPIImportRequest(BaseModel):
    spec: str                          # raw JSON or YAML text of the spec
    base_url: Optional[str] = None     # override the server base URL
    headers: Optional[dict] = None     # static headers applied to every tool (e.g. auth)
    name_prefix: str = ""              # prepended to every generated tool name
    save: bool = False                 # False → preview only; True → upsert all generated tools
    group: Optional[str] = None        # group name for this import; default: the spec's title


def _parse_spec_text(text: str) -> dict:
    """Parse spec text as JSON, falling back to YAML (if PyYAML is available)."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty spec.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # optional dependency; specs are often YAML
    except ImportError:
        raise HTTPException(
            status_code=400,
            detail="Spec is not valid JSON and PyYAML is not installed to parse YAML. Paste JSON instead.",
        )
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse spec as JSON or YAML: {e}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Spec did not parse to an object.")
    return data


@router.post("/api/tools/import/openapi")
async def import_openapi(req: OpenAPIImportRequest):
    """Convert an OpenAPI 3.x / Swagger 2.0 spec into HTTP custom tools.

    With save=False (default) returns the generated tools as a preview so the user
    can pick a subset; with save=True upserts all of them.
    """
    spec = _parse_spec_text(req.spec)
    try:
        generated = parse_openapi_spec(
            spec, base_url=req.base_url, headers=req.headers, name_prefix=req.name_prefix,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # One group per import: the spec's title unless the caller named one.
    group_name = (req.group or "").strip() or str((spec.get("info") or {}).get("title") or "").strip() or (req.name_prefix or "Imported API")
    group = {"id": _group_id(group_name), "name": group_name, "description": str((spec.get("info") or {}).get("description") or "").strip()[:500]}
    for t in generated:
        t["group"] = group["id"]

    if req.save:
        await _upsert_group(group)
        await _upsert_custom_tools(generated)
    return {"tools": generated, "count": len(generated), "saved": req.save, "group": group}


@router.delete("/api/tools/custom/{tool_name}")
async def delete_custom_tool(tool_name: str):
    from core.store.resources import delete_tool
    await delete_tool(tool_name)
    return {"status": "success"}


# ── Python Tool Endpoints ──────────────────────────────────────────────────

SANDBOX_PACKAGES = [
    "pandas", "pandas_ta", "numpy", "scipy", "scikit-learn",
    "matplotlib", "seaborn", "requests", "httpx", "beautifulsoup4",
    "lxml", "openpyxl", "xlsxwriter", "pyyaml", "tabulate",
    "jinja2", "jsonschema", "pillow", "sympy",
]

DOCKER_IMAGE = "sandbox-python:latest"
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1.0"
def _vault_root() -> str:
    """The current tenant's vault root.

    Used as a Docker bind mount below. Previously this was DATA_DIR/vault —
    i.e. *every* tenant's vaulted tool output, mounted read-only into every
    tenant's sandbox. Scoping it to the caller is the fix D12 describes.
    """
    from core.vault import _vault_root as _root
    return str(_root())
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@router.get("/api/tools/docker/status")
async def get_docker_status():
    """Check if Docker is installed, running, and if the sandbox image exists."""
    installed = shutil.which("docker") is not None
    running = False
    image_exists = False
    if installed:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            running = proc.returncode == 0
        except Exception:
            pass
        if running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "images", DOCKER_IMAGE, "--format", "{{.Repository}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                image_exists = bool(stdout_b.strip())
            except Exception:
                pass
    return {"installed": installed, "running": running, "image_exists": image_exists}


@router.post("/api/tools/docker/build")
async def build_docker_sandbox():
    """Build the sandbox-python Docker image from backend/tools/sandbox.Dockerfile."""
    if not shutil.which("docker"):
        raise HTTPException(status_code=503, detail="Docker is not installed or not in PATH")
    dockerfile = os.path.join(_BACKEND_DIR, "tools", "sandbox.Dockerfile")
    build_ctx = os.path.join(_BACKEND_DIR, "tools")
    if not os.path.isfile(dockerfile):
        raise HTTPException(status_code=404, detail="Sandbox Dockerfile not found")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", DOCKER_IMAGE, "-f", dockerfile, build_ctx,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail="Docker build timed out after 10 minutes")
        output = stdout_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Build failed:\n{output[-3000:]}")
        return {"success": True, "output": output[-3000:]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Build error: {e}")


class PythonTestRequest(BaseModel):
    code: str
    args: Optional[dict] = None
    timeout: Optional[int] = 30


@router.get("/api/tools/python/packages")
async def get_python_packages():
    """Returns list of pre-installed packages available in the Python sandbox."""
    return {"packages": SANDBOX_PACKAGES}


@router.post("/api/tools/python/test")
async def test_python_tool(req: PythonTestRequest):
    """Execute a Python tool snippet through the runner and return stdout/stderr.

    **The sixth `docker run` site**, and the one D8's count of five missed. It
    matters more than its size: this is the Test button, so leaving it on Docker
    while the run path went through `core.execution` would mean a tool that
    passes its test and then behaves differently in a run — on a deployment
    where the two runtimes are not even the same kind of thing.
    """
    from core.execution import ExecRequest, ExecUnsupported, Network, get_runner, wrap_args

    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="No code provided")

    timeout = min(int(req.timeout or 30), 60)

    try:
        result = await get_runner().run(ExecRequest(
            code=wrap_args(req.code, req.args or {}),
            network=Network.INTERNET,
            timeout_s=timeout,
            memory_mb=int(MEMORY_LIMIT.rstrip("m")),
            cpus=CPU_LIMIT,
            vault_path=Path(_vault_root()),
        ))
    except ExecUnsupported as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sandbox error: {e}") from e

    if result.timed_out:
        return {"exit_code": -1, "stdout": "", "stderr": result.stderr}

    return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}


# --- External MCP Server Management ---

async def _register_session(name: str):
    """Register a newly connected MCP session into the global tool router."""
    import core.server as _server
    session = _server.mcp_manager.sessions.get(name)
    if session:
        agent_key = f"ext_mcp_{name}"
        _server.agent_sessions[agent_key] = session
        tools = await session.list_tools()
        for tool in tools.tools:
            _server.tool_router.register(
                name, tool.name, session_key=agent_key, alias=False
            )


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    import core.server as _server
    if not _server.mcp_manager:
        return []
    return _server.mcp_manager.servers_config


@router.post("/api/mcp/servers")
async def add_mcp_server(req: AddMCPServerRequest):
    import core.server as _server
    from core.mcp_client import stdio_mcp_allowed

    # stdio MCP servers launch arbitrary local commands. Gate that capability up
    # front (before touching the manager) so it is enforced in scale mode / when
    # explicitly disabled, independent of auth. See GHSA-3j67-x3j8-r32x.
    if req.server_type == "stdio" and not stdio_mcp_allowed():
        raise HTTPException(
            status_code=403,
            detail="stdio MCP server registration is disabled on this deployment.",
        )
    if not _server.mcp_manager:
        raise HTTPException(status_code=500, detail="MCP Manager not initialized")
    try:
        result = await _server.mcp_manager.add_server(
            name=req.name,
            label=req.label,
            server_type=req.server_type,
            command=req.command,
            args=req.args,
            env=req.env,
            url=req.url,
            token=req.token,
        )
        status   = result["status"]   # "connected" | "disconnected" | "oauth_pending"
        connected = result["connected"]

        if connected:
            await _register_session(req.name)

        if status == "oauth_pending":
            return {
                "status": "oauth_pending",
                "config": result["config"],
                "connected": False,
                "auth_url": result.get("auth_url"),
                "message": "OAuth required — opening browser. Return here once authorised.",
            }
        return {
            "status": "success" if connected else "saved",
            "config": result["config"],
            "connected": connected,
            "message": "Server connected and saved." if connected else "Config saved. Use Retry to reconnect.",
        }
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/mcp/servers/{name}/reconnect")
async def reconnect_mcp_server(name: str):
    import core.server as _server
    if not _server.mcp_manager:
        raise HTTPException(status_code=500, detail="MCP Manager not initialized")
    try:
        connected = await _server.mcp_manager.reconnect_server(name)
        if connected:
            await _register_session(name)
            return {"status": "success", "connected": True, "message": "Reconnected successfully."}

        # For OAuth-based remote servers (no stored PAT), attempt a fresh OAuth flow
        config = _server.mcp_manager.get_server_config(name)
        if config and config.get("server_type") == "remote" and not config.get("token"):
            auth_url_future: asyncio.Future = asyncio.get_event_loop().create_future()
            asyncio.create_task(_server.mcp_manager.start_oauth_connect(config, auth_url_future))
            try:
                auth_url = await asyncio.wait_for(asyncio.shield(auth_url_future), timeout=10)
                if auth_url:
                    return {
                        "status": "oauth_required",
                        "connected": False,
                        "needs_oauth": True,
                        "auth_url": auth_url,
                        "message": "Re-authentication required — complete OAuth in the popup.",
                    }
            except (asyncio.TimeoutError, Exception):
                pass

        return {"status": "failed", "connected": False, "needs_oauth": False, "message": "Could not reconnect."}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/mcp/oauth/callback", response_class=HTMLResponse)
async def mcp_oauth_callback(code: str = None, state: str = None, error: str = None):
    """OAuth redirect URI.  Unblocks the waiting background connection task
    and returns a page that notifies the opener via postMessage then closes."""

    if error or not code or not state:
        err_msg = error or "Missing OAuth parameters"
        html = f"""
        <html><head><title>MCP Auth Failed</title></head><body>
        <p style="font-family:sans-serif;color:#e55">OAuth failed: {err_msg}</p>
        <script>
          if (window.opener) {{
            window.opener.postMessage({{ type:'MCP_OAUTH_COMPLETE', success:false, error:{json.dumps(err_msg)} }}, '*');
            setTimeout(() => window.close(), 2000);
          }}
        </script>
        </body></html>"""
        return HTMLResponse(html, status_code=400)

    found = oauth_state.complete_callback(state, code)
    name  = (oauth_state.get(state) or {}).get("name", "server") if not found else \
            next((v["name"] for k, v in {state: oauth_state.get(state)} .items() if v), "server")

    # Retrieve name before pop happened
    entry = oauth_state.get(state)
    server_name = entry["name"] if entry else "server"
    if not found:
        # state already popped by callback_handler — get name from config
        import core.server as _server
        server_name = next(
            (s["name"] for s in (_server.mcp_manager.servers_config if _server.mcp_manager else [])
             if s.get("status") in ("disconnected", "oauth_pending")),
            "server"
        )

    oauth_state.complete_callback(state, code)   # idempotent if already called

    html = f"""
    <html>
    <head><title>MCP Connected</title>
    <style>body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;
    height:100vh;margin:0;background:#0a0a0a;color:#fff}}
    .box{{text-align:center}}.check{{font-size:3rem;color:#22c55e}}</style>
    </head><body>
    <div class="box">
      <div class="check">&#10003;</div>
      <h2>Connected to {server_name}!</h2>
      <p style="color:#888">This tab will close automatically…</p>
    </div>
    <script>
      const name = {json.dumps(server_name)};
      if (window.opener) {{
        window.opener.postMessage({{ type:'MCP_OAUTH_COMPLETE', success:true, name }}, '*');
        setTimeout(() => window.close(), 1500);
      }}
    </script>
    </body></html>"""
    return HTMLResponse(html)


@router.delete("/api/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    import core.server as _server
    if not _server.mcp_manager:
        raise HTTPException(status_code=500, detail="MCP Manager not initialized")
    try:
        await _server.mcp_manager.remove_server(name)
        agent_key = f"ext_mcp_{name}"
        if agent_key in _server.agent_sessions:
            del _server.agent_sessions[agent_key]
        keys_to_del = [k for k, (ak, _) in _server.tool_router.items() if ak == agent_key]
        for k in keys_to_del:
            del _server.tool_router[k]

        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
