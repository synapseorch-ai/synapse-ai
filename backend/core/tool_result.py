"""A built-in tool server's failure, said the MCP way (F1208, CAP3).

The product's own tool servers (`tools/sandbox.py`, `core/vault_tools.py`, `tools/web_scraper.py`) answered a
failure with content only: `{"error": …}` from their `_err`, and `{"exit_code": 1, …}` for a script that failed.
MCP's `isError` stayed false, so a Tool step (which fails on `isError`, F90) reported the call "completed", and its
run with it (CAP3's stage 0: an `execute_python` step whose script died with a traceback).

`failed(text)` is a server's failure: still a list of content, so every caller that reads `result[0].text` keeps
working, and `as_result` turns it into `CallToolResult(isError=True)` where the server answers MCP. An agent reads
the text, not the flag, so it is still handed the failure as its answer (F90's rule).

`built_in_failure` reads the same failure from the content, for a Tool step on a built-in server that predates the
flag. Only for the product's own servers, whose shape is known: a third-party server's JSON may hold an "error" key
as data (a ticket's, a payment's), and only its `isError` says its call failed.
"""
from __future__ import annotations

import json

from mcp.types import CallToolResult, TextContent


class Failed(list):
    """A tool's content that reports a failure."""


def failed(text: str) -> Failed:
    return Failed([TextContent(type="text", text=text)])


def is_failed(content) -> bool:
    return isinstance(content, Failed)


def as_result(content):
    """What a server's `call_tool` returns: a failure as `CallToolResult(isError=True)`, anything else unchanged."""
    if isinstance(content, Failed):
        return CallToolResult(content=list(content), isError=True)
    return content


def built_in_failure(text: str) -> str | None:
    """Why a built-in server's answer is a failure, or None: a truthy top-level "error", or an "exit_code" that is
    not 0. Never for a third-party server's answer (see the module's note)."""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return str(data["error"])[:500]
    code = data.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        detail = str(data.get("stderr") or data.get("stdout") or "").strip()
        return f"The script exited with code {code}: {detail[-500:] or '(no output)'}"
    return None


def mcp_text(result) -> str:
    """What an MCP tool's result says, as one text, the way every caller reads it (F1344, cap-03): no content is ""
    (a tool that returned [] sends none), one part its text, several the list the tool returned when each part is
    JSON (MCP sends a list's items as parts of their own), else their texts one per line. A part with no text (an
    image, a resource) is left out; never an index or attribute error the agent would read as an answer."""
    texts = [t for c in (getattr(result, "content", None) or []) if isinstance(t := getattr(c, "text", None), str)]
    if len(texts) <= 1:
        return texts[0] if texts else ""
    try:
        return json.dumps([json.loads(t) for t in texts], ensure_ascii=False)
    except ValueError:
        return "\n".join(texts)

