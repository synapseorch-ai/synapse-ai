"""F1475: the agent loop's DEBUG line for a tool call names each argument and its size, never its value.

`📥 Args:` printed the call's arguments, up to 1,000 characters of them. A workflow renders `{{ global.KEY }}` into a
tool's arguments on purpose, and a model can put a key it read there, so the line carried the secret into the log,
where a chat turn's masking (F854) does not reach. The sibling of F1469, which did the same for a tool's answer."""
from __future__ import annotations

import ast
import json
import logging
import types
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2] / "core" / "react_engine.py"
#: The names react_engine holds a tool call's arguments in.
ARG_NAMES = {"tool_args", "args", "arguments", "_args"}
SAID = "sk-live-8c1d4e7f2a9b3605"


def _logs_arguments(value: ast.FormattedValue) -> bool:
    """Whether a formatted value prints a whole arguments dict: a bare name, or one handed to anything but
    `_arg_sizes`. One argument read out by its key (`tool_args['filename']`, a path the engine made) is not."""
    def walk(node: ast.AST, inside_sizes: bool) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_arg_sizes":
            inside_sizes = True
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ARG_NAMES:
            return walk(node.slice, inside_sizes)
        if isinstance(node, ast.Name) and node.id in ARG_NAMES and not inside_sizes:
            return True
        return any(walk(child, inside_sizes) for child in ast.iter_child_nodes(node))

    return walk(value.value, False)


def test_no_log_line_in_the_agent_loop_prints_a_calls_arguments():
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    printing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("debug", "info", "warning", "error")
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "_log"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr) and any(
                    isinstance(v, ast.FormattedValue) and _logs_arguments(v) for v in arg.values):
                printing.append(node.lineno)
    assert printing == [], f"react_engine.py lines whose log line prints a tool call's arguments: {printing}"


async def test_a_calls_arguments_are_logged_by_name_and_size(fake_llm, caplog):
    from _fakes import seed as S
    from core.react_engine import run_agent_step

    schema = {"type": "function", "function": {"name": "post_update", "description": "p", "parameters": {
        "type": "object", "properties": {"token": {"type": "string"}, "text": {"type": "string"}}}}}

    async def executor(name, args):
        return "posted"

    fake_llm.script([json.dumps({"tool": "post_update", "arguments": {"token": SAID, "text": "hello"}}), "Posted."])
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    caplog.set_level(logging.DEBUG, logger="core.react_engine")
    _ = [e async for e in run_agent_step(
        message="Post it.", agent_id=agent["id"], session_id="s_f1475",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[schema], tool_executor=executor, max_turns=3)]
    logged = [r.getMessage() for r in caplog.records if r.name == "core.react_engine"]
    args_line = next((m for m in logged if m.startswith("📥 Args:")), None)
    assert args_line is not None and SAID not in args_line, args_line
    assert args_line == f"📥 Args: token ({len(json.dumps(SAID))} chars), text ({len(json.dumps('hello'))} chars)", \
        args_line
