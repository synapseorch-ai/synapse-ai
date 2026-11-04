"""
Custom tools and MCP server management endpoints.
"""
import json
import os
import re
import asyncio
import shutil
from pathlib import Path
from html import escape
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.models import AddMCPServerRequest
from core.openapi_import import parse_openapi_spec
import core.mcp_oauth_state as oauth_state
from core.tools import declaration_tokens

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
    from core.store.resources import row_stamps

    # Each with its row's stamp, which the editor's save says it read (F891).
    stamps = await row_stamps("tools")
    return [{**t, "updated_at": stamps.get(t.get("name"))} for t in await load_custom_tools()]


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
    from core.tool_tags import listed_hints
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
                name = f"{server_name}__{t.name}" if is_external else t.name
                all_tools.append({
                    "name": name,
                    "description": t.description,
                    "source": server_name,
                    "source_label": display_label,
                    "type": tool_type,
                    "schema": t.inputSchema,
                    # What it adds to every call an agent holding it makes (F483).
                    "tokens": declaration_tokens(name, t.description, t.inputSchema),
                    # What it says it does, and whether that is believed (TAG1): an outside server's is a claim.
                    **({"annotations": hints} if (hints := listed_hints(t)) is not None else {}),
                    "trusted": not is_external,
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
                "tokens": declaration_tokens(t.get("name") or "", t.get("description"),
                                             t.get("inputSchema") or t.get("schema")),
                # The picker shows one row per group; an agent may take the
                # whole group as `group:<id>` (core/tools.py).
                "group": group,
                "group_label": group_names.get(group) if group else None,
                # What it says it does (TAG1): a person's tags are believed, the builder's and a file's are a claim.
                **({"annotations": hints} if (hints := listed_hints(t)) is not None else {}),
                "trusted": not t.get("annotations_by"),
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
    from core.tools import hinted
    # What the person creates or saves declares what it does (F488); a tool nobody saves keeps its old behaviour.
    # Saved by the person, the builder's hints are theirs now: they saw them in the editor.
    for t in new_tools:
        t.pop("annotations_by", None)
    await save_tools([hinted(t) for t in new_tools])


@router.post("/api/tools/custom")
async def create_custom_tool(tool: dict, request: Request = None):  # type: ignore[assignment] — called directly too
    from core.routes.save_check import SAVE_LOCK, if_match, refuse_if_stale
    from core.store.resources import row_stamps

    # The row's stamp is handed out with the tool, never stored in it (F891).
    tool.pop("updated_at", None)
    await _refuse_python_problems([tool])
    name = str(tool.get("name") or "")
    # F891: the editor says what it read, while the name is the one it opened (a rename is a new tool, as before).
    seen = if_match(request)
    async with SAVE_LOCK:
        if seen is not None:
            current = (await row_stamps("tools", [name])).get(name)
            refuse_if_stale(seen, current, exists=current is not None, noun="tool")
        note = await _keep_lock(tool)
        await _upsert_custom_tools([tool])
    return {"status": "success", "tool": {**tool, "updated_at": (await row_stamps("tools", [name])).get(name)},
            **({"note": note} if note else {})}


async def _refuse_python_problems(tools: list[dict]) -> None:
    """A Python tool saved as the builder's are (F1198, `core.python_tool`): code, that parses. On save only."""
    from core import python_tool

    for t in tools:
        if t.get("tool_type") == "python" and (why := await python_tool.refusal(t)):
            raise HTTPException(status_code=422, detail=f"`{t.get('name')}`: {why}" if len(tools) > 1 else why)


async def _keep_lock(tool: dict) -> str | None:
    """A tool made from a read API keeps its lock across a person's save (F944, A8): an editor that does not send it
    never drops it; a URL the person moves off it is theirs, so the lock follows their URL, and the save says so."""
    from core.http_tool import lock_of, under_lock
    from core.store.resources import load_tools

    before = next((t for t in await load_tools() if t.get("name") == tool.get("name")), None)
    # Only the stored lock counts: what an editor or an import sends is never taken (SAI2's review, blocker 1).
    tool.pop("base_lock", None)
    lock = str((before or {}).get("base_lock") or "")
    if not lock:
        return None
    url = str(tool.get("url") or "")
    if under_lock(url, lock):
        tool["base_lock"] = lock
        return None
    tool["base_lock"] = lock_of(url)
    return f"This tool now keeps to {tool['base_lock']}, the address you gave it, instead of {lock}."


@router.post("/api/tools/custom/bulk")
async def create_custom_tools_bulk(payload: dict):
    """Upsert many tools at once. Body: {"tools": [ {tool}, ... ]}."""
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        raise HTTPException(status_code=400, detail="Body must include a non-empty 'tools' list.")
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            raise HTTPException(status_code=400, detail="Each tool must be an object with a 'name'.")
    await _refuse_python_problems(tools)
    # An import is the person's act, but its file may be someone else's: a lock it moves is said on its result.
    notes = [f"`{t['name']}`: {note}" for t in tools if (note := await _keep_lock(t))]
    await _upsert_custom_tools(tools)
    return {"status": "success", "imported": len(tools), **({"notes": notes} if notes else {})}


#: An account part of a made tool's host (Mailchimp's `us6`): one DNS label (F945).
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@router.post("/api/tools/connect")
async def connect_made_tools(body: dict):
    """The Connect card of a build's made tools saves here (F945; A8): for each tool, the account part of its host is
    filled only with one DNS label and the provider's domain is checked again on the result, and the key goes into
    its header with its scheme, through the tools' own save. The key is never sent back."""
    from core.store.resources import load_tools

    names = [str(n) for n in body.get("tools") or []]
    header, scheme, key = str(body.get("header") or ""), str(body.get("scheme") or ""), str(body.get("key") or "")
    params = {str(k).lower(): str(v).strip().lower() for k, v in (body.get("host_params") or {}).items()}
    if not key or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise HTTPException(status_code=422, detail="The key is empty or holds a line break: paste it again.")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header) or scheme not in ("Bearer", "Token", ""):
        raise HTTPException(status_code=422, detail="That header or scheme is not one this card can save.")
    async with _save_lock():
        # Read and written under one lock, so an edit in between is never lost (SAI2's review, 8).
        current = {t.get("name"): t for t in await load_tools()}
        updated = _connected_tools(names, current, header, scheme, key, params)
        from core.store.resources import save_tools

        # Saved as they are, `annotations_by` with them: the builder's tags stay a claim (SAI2's review, 8).
        await save_tools(updated)
    return {"status": "connected", "tools": names, "message": f"Saved the {header} for {body.get('provider') or names[0]}."}


def _connected_tools(names: list[str], current: dict, header: str, scheme: str, key: str,
                     params: dict[str, str]) -> list[dict]:
    from core.builder_research import _registrable_domain
    from core.http_tool import _UNCONNECTED

    updated = []
    for name in names:
        tool = current.get(name)
        lock = str((tool or {}).get("base_lock") or "")
        if not lock:
            raise HTTPException(status_code=422, detail=f"`{name}` is not a tool a build made, so it is not "
                                                        "connected here: set its key on the Tools screen.")
        new_lock, new_url = lock, str(tool.get("url") or "")
        for part in _UNCONNECTED.findall(lock):
            label = part.strip("_")
            value = params.get(label, "")
            if not _LABEL.match(value):
                raise HTTPException(status_code=422, detail=f"`{label}` is one word of letters, digits and dashes, "
                                                            "as the card says where to find it.")
            new_lock, new_url = new_lock.replace(part, value), new_url.replace(part, value)
        before = _registrable_domain(urlsplit(lock).hostname.split(".", 1)[-1] if _UNCONNECTED.search(lock)
                                     else urlsplit(lock).hostname or "")
        if _registrable_domain(urlsplit(new_lock).hostname or "") != before:
            raise HTTPException(status_code=422, detail=f"`{name}` would no longer be on {before}: not saved.")
        updated.append({**tool, "url": new_url, "base_lock": new_lock,
                        "headers": {**(tool.get("headers") or {}), header: f"{scheme} {key}".strip()}})
    return updated


def _save_lock():
    from core.routes.save_check import SAVE_LOCK

    return SAVE_LOCK


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
    # The preview shows what each operation will declare (TAG1): a tool already saved under the name keeps the
    # person's tags while its method is the same; the rest start from their method.
    from core.store.resources import load_tools
    from core.tool_tags import carried
    from core.tools import hinted
    before = {t.get("name"): t for t in await load_tools()}
    generated = [hinted(carried(t, before.get(t["name"]))) for t in generated]

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
    #: The globals the tool declares, so the Test button hands the code the same
    #: `_globals` a run will (F28).
    globals: Optional[list[str]] = None
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
    from core import variables
    from core.execution import (
        ExecRequest,
        ExecUnsupported,
        Network,
        declared_globals,
        get_runner,
        wrap_args,
    )

    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="No code provided")

    timeout = min(int(req.timeout or 30), 60)

    try:
        result = await get_runner().run(ExecRequest(
            code=wrap_args(
                req.code,
                req.args or {},
                declared_globals({"globals": req.globals}, await variables.load())
                if req.globals
                else {},
            ),
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


#: What the old Slack, Figma and GitHub presets put in the token field (F103):
#: environment variable *names*, not tokens, so naming one back is safe.
_PLACEHOLDER_TOKENS = frozenset({
    "SLACK_CLIENT_ID", "FIGMA_PERSONAL_ACCESS_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN",
})


def _public_server(config: dict) -> dict:
    """A server's config as the browser may see it: no secrets (F135).

    The stored config held the bearer token in clear and a local server's
    environment values (its API keys), and both went to the browser as they
    were. The screen needs to know whether a token is set and, when it is a
    placeholder an older preset filled in, which one (F103) — never the value.
    Environment keys stay, values are blanked. A copy: the manager keeps its own.
    """
    view = {k: v for k, v in config.items() if k != "token"}
    token = (config.get("token") or "").strip()
    view["token_set"] = bool(token)
    view["token_placeholder"] = token if token in _PLACEHOLDER_TOKENS else None
    if isinstance(config.get("env"), dict):
        view["env"] = {key: "" for key in config["env"]}
    if view.get("last_error"):  # why it did not connect, never a credential (F1306)
        from core.mcp_client import said_safely
        view["last_error"] = said_safely(str(view["last_error"]), config)
    return view


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    import core.server as _server
    if not _server.mcp_manager:
        return []
    return [_public_server(c) for c in _server.mcp_manager.servers_config]


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
                "config": _public_server(result["config"]),
                "connected": False,
                "auth_url": result.get("auth_url"),
                "message": "OAuth required — opening browser. Return here once authorised.",
            }
        if connected:
            message = "Server connected and saved."
        elif status == "connecting":
            # Still going after the add's wait; it connects in the background (U61).
            message = "Saved — still connecting. It will show as connected when it does."
        elif result.get("error"):
            message = f"Saved, but it did not connect: {result['error']}. Retry once it is reachable."
        else:
            message = "Config saved. Use Retry to reconnect."
        return {
            "status": "success" if connected else "saved",
            "config": _public_server(result["config"]),
            "connected": connected,
            "message": message,
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
            from core.background import spawn_background
            spawn_background(_server.mcp_manager.start_oauth_connect(config, auth_url_future),
                             name="mcp-oauth-connect")
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

        # Why, in the words the add and the server list say (F1306, `mcp_client.connect_failure`).
        why = _server.mcp_manager.last_error(name)
        return {"status": "failed", "connected": False, "needs_oauth": False,
                "message": f"Could not reconnect: {why}." if why else "Could not reconnect."}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _script_literal(value: str) -> str:
    """`value` as a JS string literal that is safe inside an inline <script>.

    `json.dumps` quotes a string but leaves `</script>` in it, which ends the
    element wherever it appears — so every `<` becomes `\\u003c` (S7).
    """
    return json.dumps(value).replace("<", "\\u003c")


@router.get("/api/mcp/oauth/callback", response_class=HTMLResponse)
async def mcp_oauth_callback(code: str = None, state: str = None, error: str = None):
    """OAuth redirect URI.  Unblocks the waiting background connection task
    and returns a page that notifies the opener via postMessage then closes."""

    if error or not code or not state:
        err_msg = error or "Missing OAuth parameters"
        html = f"""
        <html><head><title>MCP Auth Failed</title></head><body>
        <p style="font-family:sans-serif;color:#e55">OAuth failed: {escape(err_msg)}</p>
        <script>
          if (window.opener) {{
            window.opener.postMessage({{ type:'MCP_OAUTH_COMPLETE', success:false, error:{_script_literal(err_msg)} }}, '*');
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
      <h2>Connected to {escape(server_name)}!</h2>
      <p style="color:#888">This tab will close automatically…</p>
    </div>
    <script>
      const name = {_script_literal(server_name)};
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
