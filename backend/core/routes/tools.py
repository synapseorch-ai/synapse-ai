"""
Custom tools and MCP server management endpoints.
"""
import json
import os
import asyncio
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.models import AddMCPServerRequest
from core.config import DATA_DIR
from core.json_store import JsonStore
import core.mcp_oauth_state as oauth_state

router = APIRouter()

_custom_tools_store = JsonStore(os.path.join(DATA_DIR, "custom_tools.json"), cache_ttl=2.0)


def load_custom_tools():
    return _custom_tools_store.load()


def save_custom_tools(tools):
    _custom_tools_store.save(tools)


@router.get("/api/tools/custom")
async def get_custom_tools():
    return load_custom_tools()


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
    all_tools = [SPAWN_SUBTASK_STATIC_SCHEMA]

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
        custom_tools = load_custom_tools()
        for t in custom_tools:
            tool_type = t.get("tool_type", "http")
            all_tools.append({
                "name": t.get("name"),
                "label": t.get("generalName", t.get("name")),
                "description": t.get("description", ""),
                "source": "custom_http" if tool_type != "python" else "custom_python",
                "type": tool_type,
                "schema": t.get("inputSchema") or t.get("schema")
            })
    except Exception as e:
        print(f"Error listing custom tools: {e}")

    return {"tools": all_tools}


@router.post("/api/tools/custom")
async def create_custom_tool(tool: dict):
    tools = load_custom_tools()
    if any(t['name'] == tool['name'] for t in tools):
        tools = [t if t['name'] != tool['name'] else tool for t in tools]
    else:
        tools.append(tool)
    save_custom_tools(tools)
    return {"status": "success", "tool": tool}


@router.delete("/api/tools/custom/{tool_name}")
async def delete_custom_tool(tool_name: str):
    tools = load_custom_tools()
    tools = [t for t in tools if t['name'] != tool_name]
    save_custom_tools(tools)
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
VAULT_ROOT = os.path.join(DATA_DIR, "vault")
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
    """Execute a Python tool snippet in the Docker sandbox and return stdout/stderr."""
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="No code provided")

    # Check Docker is available
    if not shutil.which("docker"):
        raise HTTPException(status_code=503, detail="Docker is not installed or not in PATH")

    # Inject _args into the code
    args_json = json.dumps(req.args or {})
    escaped = args_json.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    injected_code = f'import json\n_args = json.loads("""' + escaped + '""")\n\n' + req.code

    timeout = min(int(req.timeout or 30), 60)
    tmp_dir = tempfile.mkdtemp(prefix="pytool_test_")
    script_path = os.path.join(tmp_dir, "script.py")

    try:
        with open(script_path, "w") as f:
            f.write(injected_code)

        cmd = [
            "docker", "run", "--rm",
            "--memory", MEMORY_LIMIT,
            "--cpus", CPU_LIMIT,
            "--pids-limit", "64",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "--tmpfs", "/root:rw,size=256m",
            "-v", f"{script_path}:/sandbox/script.py:ro",
        ]
        if os.path.isdir(VAULT_ROOT):
            cmd += ["-v", f"{VAULT_ROOT}:/data:ro"]
        cmd += [DOCKER_IMAGE, "python", "/sandbox/script.py"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"exit_code": -1, "stdout": "", "stderr": f"Execution timed out after {timeout}s"}

        return {
            "exit_code": proc.returncode,
            "stdout": stdout_b.decode("utf-8", errors="replace")[:20000],
            "stderr": stderr_b.decode("utf-8", errors="replace")[:5000],
        }
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Docker not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sandbox error: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
            _server.tool_router[f"{name}__{tool.name}"] = (agent_key, tool.name)


async def _maybe_sync_mcp_to_pg() -> None:
    """Sync all MCP configs to Postgres if scale_auto_sync is enabled."""
    try:
        from core.config import load_settings
        if not load_settings().get("scale_auto_sync", False):
            return
        import core.server as _server
        sf = getattr(_server.app.state, "pg_session_factory", None)
        if not sf:
            return
        from core.scale.sync import sync_mcp_servers_to_pg
        async with sf() as session:
            await sync_mcp_servers_to_pg(session)
    except Exception as e:
        print(f"[tools] MCP auto-sync error: {e}", flush=True)


async def _delete_mcp_from_pg(name: str) -> None:
    """Delete an MCP server entry from Postgres."""
    try:
        import core.server as _server
        sf = getattr(_server.app.state, "pg_session_factory", None)
        if not sf:
            return
        from sqlalchemy import delete
        from core.scale.models_db import MCPServerDB
        async with sf() as session:
            await session.execute(delete(MCPServerDB).where(MCPServerDB.name == name))
            await session.commit()
    except Exception as e:
        print(f"[tools] MCP PG delete error: {e}", flush=True)


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    import core.server as _server
    if not _server.mcp_manager:
        return []
    return _server.mcp_manager.servers_config


@router.post("/api/mcp/servers")
async def add_mcp_server(req: AddMCPServerRequest):
    import core.server as _server
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

        # Auto-sync to Postgres if scale_auto_sync is enabled
        await _maybe_sync_mcp_to_pg()

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

        # Remove from Postgres immediately (regardless of auto_sync setting)
        await _delete_mcp_from_pg(name)

        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
