"""
Bash native tool — executes shell commands on the host system.

OS is detected at import time and injected into the tool description so the LLM
always knows what shell syntax to use. Execution is restricted to allowed
directories (linked repos + vault + user-configured bash_allowed_dirs).
"""

import asyncio
import json
import locale
import os
import platform
import signal
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server import Server

from core.config import BASH_TIMEOUT_DEFAULT, BASH_TIMEOUT_MAX


# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

_SYSTEM = platform.system()  # "Linux" | "Darwin" | "Windows"
_IS_WSL = (
    "microsoft" in platform.version().lower()
    or "microsoft" in platform.release().lower()
)
_OS_LABEL = "WSL2 (Linux)" if _IS_WSL else _SYSTEM

_NATIVE_WINDOWS = _SYSTEM == "Windows" and not _IS_WSL

# Shell command prefix — chosen once at startup
if _NATIVE_WINDOWS:
    _SHELL_PREFIX = ["cmd", "/c"]
else:
    _SHELL_PREFIX = ["/bin/bash", "-c"]

# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


async def _get_allowed_dirs() -> list[str]:
    """Return all directories the bash tool is allowed to run commands in.

    `core/file_roots.py`'s list, which every file tool and the Filesystem server
    use too: the linked repos, the vault (always), then Settings → Allowed
    Directories — read from the store on every call (F241).

    This process is a stdio MCP server, not the backend. It used to read Allowed
    Directories through `load_settings()`, which here is the snapshot the
    bootstrap took and never refreshes: a folder saved while the backend ran was
    refused — "Add it via Settings…", the setting just saved — until a restart.
    F213 (`8f06063`) fixed the same thing for the file tools.
    """
    from core.file_roots import allowed_roots
    from core.vault import _vault_root

    # The vault is always a place bash may run; make sure it exists to run in.
    _vault_root().mkdir(parents=True, exist_ok=True)
    return await allowed_roots()


def _is_allowed(path: str, allowed: list[str]) -> bool:
    """Check that `path` is inside (or equal to) one of the allowed dirs."""
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return False
    for d in allowed:
        try:
            base = os.path.realpath(d)
        except Exception:
            continue
        if resolved == base or resolved.startswith(base + os.sep):
            return True
    return False


# ---------------------------------------------------------------------------
# Running a command
# ---------------------------------------------------------------------------
#
# One bash server serves every chat, workflow step and sub-agent in the
# workspace, and the MCP server runs each call in a task of its own. So a
# command must never block this process: it used to run under a blocking
# `subprocess.run`, and every other `run_bash` — and the ping each caller sends
# first — waited behind it (F246). A caller whose ping went unanswered for 5 s
# skipped its call as "session unresponsive".
#
# The command leads a process group of its own, so a timeout or a cancelled
# call kills everything it started. `subprocess.run` killed only the shell: a
# `sleep` the shell had started ran on after the error was returned, and a
# child still holding the output pipes kept the call waiting for it.


def _own_process_group() -> dict:
    if _NATIVE_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _kill_command(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and whatever it started, and reap it.

    The group is killed even when the shell has already exited: a child it
    left behind can still be running, holding the output pipes open.
    """
    try:
        if _NATIVE_WINDOWS:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            # With `start_new_session` the shell's pid is its group's id.
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def _text(data: bytes | None) -> str:
    """What `text=True` gave, without its strictness: a byte that does not
    decode is replaced, rather than turning the whole result into an error."""
    if not data:
        return ""
    return data.decode(locale.getpreferredencoding(False), errors="replace").replace("\r\n", "\n")


async def _run_command(shell_cmd: list[str], cwd: str, timeout: float):
    """`(exit_code, stdout, stderr)`, or None when the command timed out.

    Killed with everything it started on a timeout and on a cancelled call
    alike; the cancel is re-raised once the command is dead.
    """
    proc = await asyncio.create_subprocess_exec(
        *shell_cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_own_process_group(),
    )
    try:
        with anyio.move_on_after(timeout) as deadline:
            stdout, stderr = await proc.communicate()
    except BaseException:
        # Shielded: a cancelled scope cancels every await in it again, and the
        # command must be dead before the cancel goes on.
        with anyio.CancelScope(shield=True):
            await _kill_command(proc)
        raise
    if deadline.cancelled_caught:
        with anyio.CancelScope(shield=True):
            await _kill_command(proc)
        return None
    return proc.returncode, _text(stdout), _text(stderr)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

app = Server("bash-server")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    allowed = await _get_allowed_dirs()
    dir_hint = ", ".join(allowed) if allowed else "(none configured yet)"
    return [
        types.Tool(
            name="run_bash",
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
            description=(
                f"Run a shell command on the host system (OS: {_OS_LABEL}). "
                f"The command executes in the specified working directory. "
                f"Always use shell syntax appropriate for {_OS_LABEL}. "
                f"Execution is restricted to allowed directories: {dir_hint}. "
                "You can pass a `cwd` to choose which directory to run in. "
                "If no `cwd` is given, the first available allowed directory is used."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Working directory for the command. "
                            "Must be within one of the allowed directories."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "description": f"Timeout in seconds (default {BASH_TIMEOUT_DEFAULT}, max {BASH_TIMEOUT_MAX}).",
                    },
                },
                "required": ["command"],
            },
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    # The tenant, the store and this tenant's settings are established by
    # `bootstrap()`, which now runs alongside the handshake rather than ahead
    # of it. A handler is the first thing that actually needs any of them.
    from core.tool_server import ready

    await ready()

    if name != "run_bash":
        return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    command: str = arguments.get("command", "").strip()
    cwd_arg: str | None = arguments.get("cwd")
    timeout: float = float(arguments.get("timeout") or BASH_TIMEOUT_DEFAULT)
    # Callers wait this long plus a margin (`core.config.mcp_call_timeout`, F300).
    timeout = min(max(timeout, 1), BASH_TIMEOUT_MAX)

    if not command:
        return [types.TextContent(type="text", text=json.dumps({"error": "No command provided."}))]

    allowed_dirs = await _get_allowed_dirs()

    # Resolve working directory
    if cwd_arg:
        cwd = os.path.realpath(os.path.expanduser(cwd_arg))
        if not _is_allowed(cwd, allowed_dirs):
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({
                        "error": (
                            f"Directory '{cwd}' is not in the allowed list. "
                            "Add it via Settings → General → Allowed Directories, "
                            "or use a linked repo/vault path."
                        ),
                        "allowed_dirs": allowed_dirs,
                    }),
                )
            ]
        if not os.path.isdir(cwd):
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"error": f"Directory does not exist: {cwd}"}),
                )
            ]
    else:
        # Default to first available allowed dir. _get_allowed_dirs always
        # includes the vault, so the list is never actually empty.
        if not allowed_dirs:
            return [types.TextContent(
                type="text",
                text=json.dumps({"error": "No directory is configured for the bash tool."}),
            )]
        cwd = allowed_dirs[0]

    # Build the shell invocation
    shell_cmd = _SHELL_PREFIX + [command]

    try:
        result = await _run_command(shell_cmd, cwd, timeout)
        if result is None:
            output = {
                "error": f"Command timed out after {timeout}s.",
                "exit_code": -1,
                "cwd": cwd,
                "os": _OS_LABEL,
            }
        else:
            exit_code, stdout, stderr = result
            output = {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "cwd": cwd,
                "os": _OS_LABEL,
                "allowed_dirs": allowed_dirs,
            }
    except PermissionError as e:
        output = {
            "error": f"Permission denied: {e}",
            "exit_code": -1,
            "cwd": cwd,
            "os": _OS_LABEL,
        }
    except Exception as e:
        output = {
            "error": str(e),
            "exit_code": -1,
            "cwd": cwd,
            "os": _OS_LABEL,
        }

    return [types.TextContent(type="text", text=json.dumps(output))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    # This process serves exactly one tenant, and it has to be told which —
    # see core/tool_server.py.
    # Serve first, bootstrap alongside. `bootstrap()` reads Postgres, and
    # putting that ahead of `stdio_server()` meant the MCP handshake waited
    # on a database — a cold serverless resume expired the 60s bound and
    # took the whole chat turn with it. Handlers wait via `ready()`.
    from core.tool_server import serve

    await serve(app)


if __name__ == "__main__":
    asyncio.run(main())
