"""
Sandbox — secure workspace + Python execution for agents.

Combines a persistent shared file vault (create / read / write / patch / list /
delete) with a Docker-based Python executor.  Vault files are auto-mounted at
/data inside the container so agents can create data, then run code against it.
"""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timezone

server = Server("sandbox")

# ── paths & limits ───────────────────────────────────────────────────────

VAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "vault"
VAULT_ROOT.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB read cap
DEFAULT_TIMEOUT = 30               # seconds
MAX_TIMEOUT = 120
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1.0"
DOCKER_IMAGE = "sandbox-python:latest"
MAX_OUTPUT = 50_000                # chars

# ── shared helpers ───────────────────────────────────────────────────────

def _ok(data: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}))]


def _safe_path(relative: str) -> Path | None:
    """Resolve *relative* inside VAULT_ROOT; return None if it escapes."""
    target = (VAULT_ROOT / relative).resolve()
    if not str(target).startswith(str(VAULT_ROOT)):
        return None
    return target


def _resolve(raw_path: str) -> Path | None:
    """Accept absolute vault path or relative."""
    p = Path(raw_path)
    if p.is_absolute():
        resolved = p.resolve()
        if not str(resolved).startswith(str(VAULT_ROOT)):
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


# ── S3 helpers (scale mode only; all are fire-and-forget / best-effort) ──────

def _s3_rel(target: Path) -> str | None:
    """Vault-relative path for constructing S3 keys (e.g. 'reports/q1.json')."""
    try:
        return str(target.resolve().relative_to(VAULT_ROOT.resolve()))
    except ValueError:
        return None


def _s3_upload(target: Path, content: str) -> None:
    """Upload content to S3 at vault/{rel}. Never raises."""
    try:
        from core.s3_storage import get_s3
        s3 = get_s3()
        if s3:
            rel = _s3_rel(target)
            if rel:
                s3.upload_text(f"vault/{rel}", content)
    except Exception:
        pass


def _s3_ensure_local(target: Path) -> bool:
    """If target is missing locally, download from S3 and write it. Returns True if now available."""
    if target.exists():
        return True
    try:
        from core.s3_storage import get_s3
        s3 = get_s3()
        if not s3:
            return False
        rel = _s3_rel(target)
        if not rel:
            return False
        content = s3.download_text(f"vault/{rel}")
        if content is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def _s3_delete(target: Path) -> None:
    """Delete from S3. Never raises."""
    try:
        from core.s3_storage import get_s3
        s3 = get_s3()
        if s3:
            rel = _s3_rel(target)
            if rel:
                s3.delete(f"vault/{rel}")
    except Exception:
        pass


def _s3_list_rels(subdir_rel: str) -> list[str]:
    """Return vault-relative paths of all objects in S3 under the given subdirectory."""
    try:
        from core.s3_storage import get_s3
        s3 = get_s3()
        if not s3:
            return []
        prefix = f"vault/{subdir_rel}/" if subdir_rel else "vault/"
        keys = s3.list_keys(prefix) or []
        # list_keys returns rel-keys that still include the "vault/" prefix
        return [k[len("vault/"):] for k in keys if k.startswith("vault/") and not k.endswith("/")]
    except Exception:
        return []


# ── tool definitions ─────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # -- Vault tools --
        Tool(
            name="vault_create",
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

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return _err(f"File already exists: {target}. Use vault_write to overwrite.")

    if target.suffix == ".json":
        try:
            content = json.dumps(json.loads(content), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    target.write_text(content, encoding="utf-8")
    _s3_upload(target, content)
    return _ok({"path": str(target), "size": len(content), "created": True})


def _handle_read(args: dict) -> list[TextContent]:
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")
    if not target.exists():
        _s3_ensure_local(target)
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

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.suffix == ".json":
        try:
            content = json.dumps(json.loads(content), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

    target.write_text(content, encoding="utf-8")
    _s3_upload(target, content)
    return _ok({"path": str(target), "size": len(content), "written": True})


def _handle_patch(args: dict) -> list[TextContent]:
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")
    if not target.exists():
        _s3_ensure_local(target)
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
        target.write_text(content, encoding="utf-8")
        _s3_upload(target, content)
        return _ok({"path": str(target), "content": content, "patched": True})

    elif find_str is not None:
        if replace_str is None:
            replace_str = ""
        if find_str not in content:
            return _err(f"Text '{find_str[:80]}' not found in file")
        content = content.replace(find_str, replace_str, 1)
        target.write_text(content, encoding="utf-8")
        _s3_upload(target, content)
        return _ok({"path": str(target), "content": content, "patched": True})

    return _err("Provide either 'merge' (for JSON) or 'find'+'replace' (for text)")


def _handle_list(args: dict) -> list[TextContent]:
    subdir = args.get("directory", "")
    ext_filter = args.get("extension")

    base = _safe_path(subdir) if subdir else VAULT_ROOT
    if base is None:
        return _err("Path escapes the vault directory")

    files = []
    seen_rels: set[str] = set()

    if base.exists():
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            if ext_filter and item.suffix != ext_filter:
                continue
            rel = str(item.relative_to(VAULT_ROOT))
            seen_rels.add(rel)
            files.append({
                "name": item.name,
                "path": str(item),
                "relative": rel,
                "size": item.stat().st_size,
            })

    # Merge S3-only files (present in S3 but not on this worker's local disk)
    for rel in _s3_list_rels(subdir):
        if ext_filter and not rel.endswith(ext_filter):
            continue
        if rel in seen_rels:
            continue
        s3_local = VAULT_ROOT / rel
        files.append({
            "name": s3_local.name,
            "path": str(s3_local),
            "relative": rel,
            "size": 0,
        })

    return _ok({"directory": str(base), "count": len(files), "files": files})


def _handle_delete(args: dict) -> list[TextContent]:
    target = _resolve(args.get("path", ""))
    if target is None:
        return _err("Invalid path or path escapes the vault")
    if target.is_dir():
        return _err("Cannot delete directories. Delete files individually.")

    if not target.exists():
        # File may exist only in S3 (written by another worker)
        rel = _s3_rel(target)
        if not rel:
            return _err(f"File not found: {target}")
        try:
            from core.s3_storage import get_s3
            s3 = get_s3()
            if s3 and s3.download_text(f"vault/{rel}") is not None:
                s3.delete(f"vault/{rel}")
                return _ok({"path": str(target), "deleted": True})
        except Exception:
            pass
        return _err(f"File not found: {target}")

    target.unlink()
    _s3_delete(target)
    return _ok({"path": str(target), "deleted": True})


# ── python executor ──────────────────────────────────────────────────────

def _build_docker_cmd(
    script_path: str,
    *,
    timeout: int,
    packages: list[str] | None = None,
    allow_network: bool = False,
) -> list[str]:
    cmd = [
        "docker", "run",
        "--rm",
        "--memory", MEMORY_LIMIT,
        "--cpus", CPU_LIMIT,
        "--pids-limit", "64",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=512m",
        "--tmpfs", "/root:rw,size=512m",
    ]

    cmd += ["-v", f"{script_path}:/sandbox/script.py:ro"]

    if VAULT_ROOT.exists():
        cmd += ["-v", f"{VAULT_ROOT}:/data:ro"]

    cmd.append(DOCKER_IMAGE)

    inner_parts: list[str] = []
    if packages:
        safe_pkgs = " ".join(
            p for p in packages if all(c.isalnum() or c in "-_.[]=<>!" for c in p)
        )
        if safe_pkgs:
            inner_parts.append(
                f"pip install --quiet --disable-pip-version-check "
                f"--root-user-action=ignore --no-warn-script-location "
                f"{safe_pkgs}"
            )

    inner_parts.append("python /sandbox/script.py")
    cmd += ["sh", "-c", " && ".join(inner_parts)]
    return cmd


async def _handle_execute(args: dict) -> list[TextContent]:
    code: str = args.get("code", "")
    packages: list[str] | None = args.get("packages")
    timeout = min(int(args.get("timeout", DEFAULT_TIMEOUT)), MAX_TIMEOUT)
    allow_network: bool = bool(args.get("allow_network", False))

    if not code.strip():
        return _err("No code provided")

    if packages and not allow_network:
        allow_network = True

    tmp_dir = tempfile.mkdtemp(prefix="pysandbox_")
    script_path = os.path.join(tmp_dir, "script.py")

    try:
        with open(script_path, "w") as f:
            f.write(code)

        docker_cmd = _build_docker_cmd(
            script_path,
            timeout=timeout,
            packages=packages,
            allow_network=allow_network,
        )

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 10
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _err(f"Execution timed out after {timeout}s")

        stdout = stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT]
        stderr = stderr_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT]

        result: dict = {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

        return _ok(result)

    except FileNotFoundError:
        return _err("Docker is not installed or not in PATH")
    except Exception as e:
        return _err(f"Sandbox error: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── main ─────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
