"""
Sandbox — secure workspace + Python execution for agents.

Combines a persistent shared file vault (create / read / write / patch / list /
delete) with a Docker-based Python executor.  Vault files are auto-mounted at
/data inside the container so agents can create data, then run code against it.

The vault root comes from `core.vault._vault_root()`, per call. It used to be a
module constant built from `__file__` — `backend/tools/../data/vault` — which
stopped being the vault when blob storage became tenant-scoped, and which this
module also `mkdir`'d at import, quietly recreating `backend/data`. Nothing
raised: the Docker `-v` mount below is `.exists()`-guarded, so the container
simply came up without its `/data` mount. Per call rather than cached because
the path carries the tenant, and one process serves many.

**Every vault write, read, listing and delete goes through `get_vault()`**
(`core/vault_backend.py`), so on object storage the store is the vault and the
directory a working copy of it. This module used to keep its own S3 mirror —
copies of its writes under `vault/{path}` with no tenant in the key, read back
only by itself — beside the blob store everything else used (F57).
"""

from mcp.server import Server
from mcp.types import ToolAnnotations, Tool, TextContent

import asyncio
import json
import uuid
from pathlib import Path
from datetime import datetime, timezone

from core.file_roots import within

server = Server("sandbox")

# ── paths & limits ───────────────────────────────────────────────────────

def _vault_root() -> Path:
    """The current tenant's vault, from the blob store."""
    from core.vault import _vault_root as blob_vault_root

    return blob_vault_root()


def _vault():
    """The current tenant's vault backend — asked per call, like the root."""
    from core.vault_backend import get_vault

    return get_vault()


MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB read cap
DEFAULT_TIMEOUT = 30               # seconds
MAX_TIMEOUT = 120
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1.0"
MAX_OUTPUT = 50_000                # chars

# ── shared helpers ───────────────────────────────────────────────────────

def _ok(data: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}))]


def _safe_path(relative: str) -> Path | None:
    """Resolve *relative* inside the vault; return None if it escapes."""
    root = _vault_root()
    target = (root / relative).resolve()
    # `within`, not `startswith`: a sibling named `vault-old` starts with the
    # vault's own path (S8).
    if not within(target, [root]):
        return None
    return target


def _resolve(raw_path: str) -> Path | None:
    """Accept absolute vault path or relative."""
    p = Path(raw_path)
    if p.is_absolute():
        resolved = p.resolve()
        if not within(resolved, [_vault_root()]):
            return None
        return resolved
    return _safe_path(raw_path)


def _auto_name(extension: str = ".json") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:6]}{extension}"


EXT_MAP = {
    "json": ".json",
    "text": ".txt",
    "csv": ".csv",
    "python": ".py",
    "markdown": ".md",
}


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# ── tool definitions ─────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # -- Vault tools --
        Tool(
            name="vault_create",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
            description=(
                "Create a new file in the shared vault workspace. "
                "Returns the absolute path for passing to other tools or the Python sandbox. "
                "If no filename is given one is auto-generated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "File content. For JSON files pass a JSON string.",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Optional relative path inside the vault (e.g. 'reports/q1.json'). "
                            "Intermediate directories are created automatically. "
                            "If omitted a unique name is generated."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "text", "csv", "python", "markdown"],
                        "description": "Hint for auto-generated filename extension (default 'json').",
                        "default": "json",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="vault_read",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description="Read a file from the vault by its path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path (as returned by vault_create / vault_write) "
                            "or a relative path inside the vault."
                        ),
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="vault_write",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
            description=(
                "Write (overwrite) a file in the vault. Creates it if missing. "
                "Returns the absolute path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or vault-relative path to write to.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="vault_patch",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
            description=(
                "Patch an existing file. JSON files: deep-merges the provided object. "
                "Text files: replaces first occurrence of `find` with `replace`. "
                "Returns the absolute path and updated content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or vault-relative path.",
                    },
                    "merge": {
                        "type": "string",
                        "description": "JSON string to deep-merge (JSON files only).",
                    },
                    "find": {
                        "type": "string",
                        "description": "Text to find (text files). Used with 'replace'.",
                    },
                    "replace": {
                        "type": "string",
                        "description": "Replacement text (text files). Used with 'find'.",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="vault_list",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description="List files in the vault, optionally filtered by extension or subdirectory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Subdirectory inside the vault to list (default: root).",
                        "default": "",
                    },
                    "extension": {
                        "type": "string",
                        "description": "Filter by extension, e.g. '.json', '.csv'.",
                    },
                },
            },
        ),
        Tool(
            name="vault_delete",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
            description="Delete a file from the vault. Returns confirmation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or vault-relative path to delete.",
                    },
                },
                "required": ["path"],
            },
        ),
        # -- Python executor --
        Tool(
            name="execute_python",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
            description=(
                "Execute Python code in a secure Docker container (512 MB RAM, 1 CPU, "
                "read-only filesystem, no network by default). "
                "Pre-installed packages: pandas, pandas_ta, numpy, scipy, scikit-learn, "
                "matplotlib, seaborn, requests, httpx, beautifulsoup4, lxml, openpyxl, "
                "xlsxwriter, pyyaml, tabulate, jinja2, jsonschema, pillow, sympy. "
                "Additional packages can be installed via the 'packages' parameter. "
                "Vault files are mounted read-only at /data — e.g. vault path "
                "'reports/q1.json' is available at '/data/reports/q1.json'. "
                "Use for calculations, data processing, or testing logic safely."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute.",
                    },
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "pip packages to install before running. "
                            "Auto-enables network. Example: [\"requests\", \"pandas\"]"
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Max seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
                        "default": DEFAULT_TIMEOUT,
                    },
                    "allow_network": {
                        "type": "boolean",
                        "description": "Allow network access (default false).",
                        "default": False,
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="tool_code",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
            description=(
                "Alias for execute_python. Execute Python code in a secure Docker container. "
                "Use this to run calculations, data processing, or test logic safely."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute."},
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "pip packages to install before running.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Max seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
                        "default": DEFAULT_TIMEOUT,
                    },
                    "allow_network": {
                        "type": "boolean",
                        "description": "Allow network access (default false).",
                        "default": False,
                    },
                },
                "required": ["code"],
            },
        ),
    ]


# ── tool dispatch ────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # The tenant, the store and this tenant's settings are established by
    # `bootstrap()`, which now runs alongside the handshake rather than ahead
    # of it. A handler is the first thing that actually needs any of them.
    from core.tool_server import ready

    await ready()

    try:
        match name:
            case "vault_create":    return _handle_create(arguments)
            case "vault_read":      return _handle_read(arguments)
            case "vault_write":     return _handle_write(arguments)
            case "vault_patch":     return _handle_patch(arguments)
            case "vault_list":      return _handle_list(arguments)
            case "vault_delete":    return _handle_delete(arguments)
            case "execute_python" | "tool_code": return await _handle_execute(arguments)
            case _:               return _err(f"Unknown tool: {name}")
    except Exception as e:
        return _err(str(e))


# ── vault handlers ───────────────────────────────────────────────────────

def _handle_create(args: dict) -> list[TextContent]:
    content = args.get("content", "")
    filename = args.get("filename")
    fmt = args.get("format", "json")

    if not filename:
        filename = _auto_name(EXT_MAP.get(fmt, ".json"))

    target = _safe_path(filename)
    if target is None:
        return _err("Path escapes the vault directory")

    vault = _vault()
    if vault.ensure_local(target).exists():
        return _err(f"File already exists: {target}. Use vault_write to overwrite.")

    if target.suffix == ".json":
        try:
            content = json.dumps(json.loads(content), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    vault.write(target, content)
    return _ok({"path": str(target), "size": len(content), "created": True})


def _handle_read(args: dict) -> list[TextContent]:
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")
    _vault().ensure_local(target)
    if not target.exists():
        return _err(f"File not found: {target}")
    if target.is_dir():
        return _err("Path is a directory. Use vault_list instead.")

    size = target.stat().st_size
    if size > MAX_FILE_SIZE:
        return _err(f"File too large ({size} bytes). Max is {MAX_FILE_SIZE}.")

    return _ok({"path": str(target), "content": target.read_text(encoding="utf-8"), "size": size})


def _handle_write(args: dict) -> list[TextContent]:
    content = args.get("content", "")
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")

    if target.suffix == ".json":
        try:
            content = json.dumps(json.loads(content), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    _vault().write(target, content)
    return _ok({"path": str(target), "size": len(content), "written": True})


def _handle_patch(args: dict) -> list[TextContent]:
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")
    vault = _vault()
    vault.ensure_local(target)
    if not target.exists():
        return _err(f"File not found: {target}")

    content = target.read_text(encoding="utf-8")
    merge_data = args.get("merge")
    find_str = args.get("find")
    replace_str = args.get("replace")

    if merge_data is not None:
        try:
            existing = json.loads(content)
        except json.JSONDecodeError:
            return _err("File is not valid JSON — cannot merge")
        try:
            patch = json.loads(merge_data)
        except json.JSONDecodeError:
            return _err("merge value is not valid JSON")

        if isinstance(existing, dict) and isinstance(patch, dict):
            _deep_merge(existing, patch)
        elif isinstance(existing, list) and isinstance(patch, list):
            existing.extend(patch)
        else:
            return _err("Cannot merge: incompatible types (both must be objects or both arrays)")

        content = json.dumps(existing, indent=2)
        vault.write(target, content)
        return _ok({"path": str(target), "content": content, "patched": True})

    elif find_str is not None:
        if replace_str is None:
            replace_str = ""
        if find_str not in content:
            return _err(f"Text '{find_str[:80]}' not found in file")
        content = content.replace(find_str, replace_str, 1)
        vault.write(target, content)
        return _ok({"path": str(target), "content": content, "patched": True})

    return _err("Provide either 'merge' (for JSON) or 'find'+'replace' (for text)")


def _handle_list(args: dict) -> list[TextContent]:
    subdir = args.get("directory", "")
    ext_filter = args.get("extension")

    root = _vault_root()
    base = _safe_path(subdir) if subdir else root
    if base is None:
        return _err("Path escapes the vault directory")

    # Everything the vault holds under the directory, not only what this
    # process has materialised.
    _vault().hydrate(subdir)

    files = []
    if base.exists():
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            if ext_filter and item.suffix != ext_filter:
                continue
            files.append({
                "name": item.name,
                "path": str(item),
                "relative": str(item.relative_to(root)),
                "size": item.stat().st_size,
            })

    return _ok({"directory": str(base), "count": len(files), "files": files})


def _handle_delete(args: dict) -> list[TextContent]:
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")
    if target.is_dir():
        return _err("Cannot delete directories. Delete files individually.")

    vault = _vault()
    vault.ensure_local(target)
    if not target.exists():
        return _err(f"File not found: {target}")

    vault.forget(target)
    return _ok({"path": str(target), "deleted": True})


# ── python executor ──────────────────────────────────────────────────────


async def _handle_execute(args: dict) -> list[TextContent]:
    """Run the agent's code through the registered runner.

    **`allow_network` now does something.** It was accepted here, passed to
    `_build_docker_cmd(…, allow_network=…)`, and dropped on the floor — every
    call ran on Docker's default bridge, including the ones that asked not to.
    `core.execution.Network` is a required field on the request, so the same
    mistake cannot be made silently again; `test_execution.py` asserts a
    `Network.NONE` request actually fails to reach a URL.
    """
    from core.execution import ExecRequest, ExecUnsupported, Network, get_runner

    code: str = args.get("code", "")
    packages: list[str] | None = args.get("packages")
    timeout = min(int(args.get("timeout", DEFAULT_TIMEOUT)), MAX_TIMEOUT)
    allow_network: bool = bool(args.get("allow_network", False))

    if not code.strip():
        return _err("No code provided")

    # Installing packages needs pip to reach an index, so asking for packages is
    # asking for network. Kept as it was, now that the flag has teeth.
    if packages and not allow_network:
        allow_network = True

    try:
        result = await get_runner().run(ExecRequest(
            code=code,
            network=Network.INTERNET if allow_network else Network.NONE,
            timeout_s=timeout,
            grace_s=10,
            tmpfs_mb=512,
            memory_mb=int(MEMORY_LIMIT.rstrip("m")),
            cpus=CPU_LIMIT,
            packages=tuple(packages or ()),
            vault_path=_vault_root(),
            max_stdout=MAX_OUTPUT,
            max_stderr=MAX_OUTPUT,
        ))
    except ExecUnsupported as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001 — reported to the agent, not raised at it
        return _err(f"Sandbox error: {e}")

    if result.timed_out:
        return _err(f"Execution timed out after {timeout}s")

    return _ok({
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })


# ── main ─────────────────────────────────────────────────────────────────

async def main():
    # This process serves exactly one tenant, and it has to be told which —
    # see core/tool_server.py.
    # Serve first, bootstrap alongside. `bootstrap()` reads Postgres, and
    # putting that ahead of `stdio_server()` meant the MCP handshake waited
    # on a database — a cold serverless resume expired the 60s bound and
    # took the whole chat turn with it. Handlers wait via `ready()`.
    from core.tool_server import serve

    await serve(server)


if __name__ == "__main__":
    asyncio.run(main())
