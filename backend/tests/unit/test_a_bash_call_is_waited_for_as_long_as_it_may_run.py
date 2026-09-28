"""
A caller waits for `run_bash` as long as the command may run (F300).

`run_bash` tells the model it may set a timeout of up to 120 s, but every MCP
call was waited for `MCP_TOOL_CALL_TIMEOUT` (60 s) whatever the tool. Measured
on a scratch install: a chat's `sleep 70; touch marker` with `timeout: 100` came
back at 61 s as "Timed out while waiting for response to ClientRequest. Waited
60.0 seconds.", and the marker appeared at +75 s. The command ran to its end
with nobody reading what it said.

A caller now waits for a `run_bash` call its own timeout (the tool's clamp,
1–120 s, default 30) plus 5 s for the reply. The command is killed at its own
timeout (F246), so the caller always hears how it ended. Every other tool keeps
`MCP_TOOL_CALL_TIMEOUT`.
"""
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


class _Bash:
    """An MCP session serving `run_bash`, noting how long each caller would wait."""

    def __init__(self):
        self.waits: list[float] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        self.waits.append(read_timeout_seconds.total_seconds())
        out = {"stdout": "ok\n", "stderr": "", "exit_code": 0}
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(out))], isError=False)


def _server(session):
    return types.SimpleNamespace(agent_sessions={"bash": session}, memory_store=None,
                                 tool_router={"run_bash": ("bash", "run_bash")})


_RUN_BASH = {"type": "function", "function": {
    "name": "run_bash", "description": "Run a shell command.",
    "parameters": {"type": "object", "properties": {
        "command": {"type": "string"}, "timeout": {"type": "number"}}}}}


class TestTheWait:
    @pytest.mark.parametrize("arguments, wait", [
        ({"command": "make", "timeout": 100}, 105.0),
        ({"command": "make", "timeout": 120}, 125.0),
        ({"command": "make", "timeout": 500}, 125.0),   # the tool clamps to 120
        ({"command": "make", "timeout": "90"}, 95.0),
        ({"command": "make"}, 60.0),                    # 30 s default + 5 < 60
        ({"command": "make", "timeout": "soon"}, 60.0),
        ({"command": "make", "timeout": 0}, 60.0),
    ])
    def test_run_bash_is_waited_for_its_own_timeout(self, arguments, wait):
        from core.config import mcp_call_timeout
        assert mcp_call_timeout("run_bash", arguments) == wait

    def test_every_other_tool_keeps_the_default(self):
        from core.config import MCP_TOOL_CALL_TIMEOUT, mcp_call_timeout
        assert mcp_call_timeout("list_tables", {"timeout": 100}) == MCP_TOOL_CALL_TIMEOUT
        assert mcp_call_timeout("run_bash", None) == MCP_TOOL_CALL_TIMEOUT

    def test_the_tool_clamps_as_the_wait_does(self):
        """One pair of numbers: what `run_bash` accepts is what its callers wait for."""
        from core import config
        from tools import bash
        assert (bash.BASH_TIMEOUT_DEFAULT, bash.BASH_TIMEOUT_MAX) == (
            config.BASH_TIMEOUT_DEFAULT, config.BASH_TIMEOUT_MAX) == (30, 120)


async def test_a_chat_waits_for_a_long_bash_command(fake_llm):
    from core.react_engine import run_agent_step

    bash = _Bash()
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script([tool_call("run_bash", command="make test", timeout=100), "Built."])
    events = [ev async for ev in run_agent_step(
        message="build it", agent_id=agent["id"], session_id="s_f300",
        server_module=_server(bash), agent_override=agent, tools_override=[_RUN_BASH], max_turns=3)]

    assert next(e for e in reversed(events) if e.get("type") == "final")["response"] == "Built."
    assert bash.waits == [105.0], bash.waits


async def test_a_tool_step_waits_for_a_long_bash_command(fake_llm, monkeypatch):
    import core.tools as tools_mod
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    async def resolve_one_tool(server_module, name, agent, custom):
        return types.SimpleNamespace(name=name, description="Run a shell command.",
                                     inputSchema=_RUN_BASH["function"]["parameters"])

    monkeypatch.setattr(tools_mod, "resolve_one_tool", resolve_one_tool)
    bash = _Bash()
    fake_llm.script([tool_call("run_bash", command="make test", timeout=100)])
    orch = S.make_orchestration(entry_step_id="call", steps=[
        {"id": "call", "name": "Build", "type": "tool", "forced_tool": "run_bash",
         "prompt_template": "build it", "output_key": "result", "model": "claude-x",
         "next_step_id": None}])
    events = [e async for e in OrchestrationEngine(
        Orchestration.model_validate(orch), _server(bash)).run("go", run_id="run_f300")]

    done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
    assert done["status"] == "completed", events
    assert bash.waits == [105.0], bash.waits
