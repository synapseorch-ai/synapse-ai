"""F1469 (CodeQL 9928): the agent loop's DEBUG lines say which tool answered and how long the answer was, never
what it said. A tool's output can carry a key it was handed (`{{global.KEY}}`) or one it read, and a log line is
kept and shipped where a chat turn's masking (F854) does not reach. Nine lines in react_engine printed the output,
or a 200-500 character preview of it."""
from __future__ import annotations

import ast
import json
import logging
import types
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2] / "core" / "react_engine.py"
#: The names react_engine holds a tool's output (or a piece of it) in.
OUTPUT_NAMES = {"preview", "raw_output", "_mem_preview", "stderr", "stdout"}
SAID = "sk-live-4f9a2c71e8b3d605"


def _prints_output(value: ast.FormattedValue) -> bool:
    expr = value.value
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "len":
        return False
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and node.id in OUTPUT_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in OUTPUT_NAMES:
            return True
    return False


def test_no_debug_line_in_the_agent_loop_prints_a_tools_output():
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    printing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "debug"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "_log"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr) and any(
                    isinstance(v, ast.FormattedValue) and _prints_output(v) for v in arg.values):
                printing.append(node.lineno)
    assert printing == [], f"react_engine.py lines whose DEBUG line prints a tool's output: {printing}"


async def test_a_builder_tools_answer_is_logged_by_its_length(fake_llm, caplog):
    from _fakes import seed as S
    from core.react_engine import run_agent_step

    schema = {"type": "function", "function": {"name": "connect_app", "description": "c", "parameters": {
        "type": "object", "properties": {}}}}
    answer = json.dumps({"connected": True, "token": SAID})

    async def executor(name, args):
        return answer

    fake_llm.script([json.dumps({"tool": "connect_app", "arguments": {}}), "Connected."])
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    caplog.set_level(logging.DEBUG, logger="core.react_engine")
    events = [e async for e in run_agent_step(
        message="Connect it.", agent_id=agent["id"], session_id="s_f1469",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[schema], tool_executor=executor, max_turns=3)]
    assert any(e.get("type") == "tool_result" and e.get("tool_name") == "connect_app" for e in events), events
    logged = [r.getMessage() for r in caplog.records if r.name == "core.react_engine"]
    assert not [m for m in logged if SAID in m], [m for m in logged if SAID in m]
    assert any(f"Executor result (connect_app): {len(answer)} chars" in m for m in logged), logged
