"""What every door that saves a Python custom tool checks (F1198, BLD1; CAP3's F-a).

The tool editor's save (`POST /api/tools/custom`, and `/bulk`) and the builder's `create_python_tool` ask the same
questions here, so a model can't save what a person couldn't. The editor's save took any document: a tool with no
code, or code that does not parse, was saved and failed every call ("Python tool has no code defined.", a traceback).

Checked on save only. A tool saved before keeps loading and running as it always did.

The host may add its own rule (`set_check`): cloud's plan gate for custom Python tools (`code_execution`), in its
route's words, for the doors that do not pass through that route.
"""
from __future__ import annotations

import ast
from typing import Any

#: The host's rule: async `check(tool) -> reason | None`. None: OSS, none.
_check: Any = None


def set_check(check: Any) -> None:
    """Install the host's rule for saving a Python tool. ``None`` removes it."""
    global _check
    _check = check


def problems(tool: dict) -> list[str]:
    """What is wrong with `tool` as a Python tool, in the editor's words; empty when nothing is."""
    code = tool.get("code")
    if not isinstance(code, str) or not code.strip():
        return ["Python code cannot be empty."]
    out = []
    try:
        ast.parse(code)
    except SyntaxError as err:
        out.append(f"Its code does not parse: line {err.lineno}: {err.msg}.")
    schema = tool.get("inputSchema")
    if schema is not None and not isinstance(schema, dict):
        out.append("Its input schema must be a JSON Schema object.")
    names = tool.get("globals")
    if names is not None and not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
        out.append("The variables it reads are a list of variable names.")
    return out


async def refusal(tool: dict) -> str | None:
    """Why `tool` may not be saved, or None: its problems, then the host's rule."""
    if found := problems(tool):
        return " ".join(found)
    return await _check(tool) if _check is not None else None
