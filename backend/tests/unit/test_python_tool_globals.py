"""A Python tool receives the globals it declares, and nothing a model writes is
rendered with them (F28, D104).

Both Python branches — a TOOL step and an agent — used to run the model-written
arguments through `{{ global.* }}` rendering. A prompt-injected page could then
ask for `{{ global.ANY_KEY }}` and have the plaintext handed to customer code
running with internet access. Arguments are values now (D97). A tool that needs
a variable names it in `globals`, and only those reach the code, as `_globals`.
"""

import pytest

from core import variables
from core.execution import ExecRequest, ExecResult, register_runner, wrap_args
from core.store.resources import save_tools
from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_react_engine_deep import _drive


class _Recording:
    def __init__(self):
        self.requests: list[ExecRequest] = []

    async def run(self, request: ExecRequest) -> ExecResult:
        self.requests.append(request)
        return ExecResult(exit_code=0, stdout='{"ok": true}', stderr="")


@pytest.fixture
def runner():
    recording = _Recording()
    register_runner(recording)
    yield recording
    register_runner(None)


def _preamble(code: str) -> dict:
    """What the script's preamble binds, without running the tool's own code."""
    namespace: dict = {}
    exec(code.split("\n\n", 1)[0], namespace)  # noqa: S102 — our own preamble
    return namespace


TOOL = {
    "name": "lookup_account",
    "description": "Look an account up",
    "tool_type": "python",
    "code": "print('{}')",
    "globals": ["API_KEY"],
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
}

SCHEMA = {"type": "function", "function": {
    "name": "lookup_account", "description": "Look an account up",
    "parameters": TOOL["inputSchema"],
}}


class TestThePreamble:
    def test_declared_globals_arrive_as_their_own_dict(self):
        bound = _preamble(wrap_args("pass", {"query": "x"}, {"API_KEY": "k"}))
        assert bound["_args"] == {"query": "x"}
        assert bound["_globals"] == {"API_KEY": "k"}

    def test_no_declaration_is_an_empty_dict_not_a_name_error(self):
        assert _preamble(wrap_args("pass", {}))["_globals"] == {}


class TestATOOLStep:
    async def test_an_argument_is_a_value_and_only_declared_globals_reach_the_code(self, runner):
        from core.orchestration.steps import ToolStepExecutor

        await variables.save({"API_KEY": "the-key", "PAYROLL_TOKEN": "never-this"})

        await ToolStepExecutor()._execute_python_tool(
            TOOL, {"query": "{{ global.PAYROLL_TOKEN }}"}
        )

        bound = _preamble(runner.requests[0].code)
        assert bound["_args"] == {"query": "{{ global.PAYROLL_TOKEN }}"}
        assert bound["_globals"] == {"API_KEY": "the-key"}
        assert "never-this" not in runner.requests[0].code


class TestAnAgent:
    async def test_an_argument_is_a_value_and_only_declared_globals_reach_the_code(
        self, fake_llm, runner
    ):
        await variables.save({"API_KEY": "the-key", "PAYROLL_TOKEN": "never-this"})
        await save_tools([TOOL])
        agent = S.make_agent(tools=["lookup_account"], skip_default_tools=True)

        await _drive(
            agent, fake_llm,
            [tool_call("lookup_account", query="{{ global.PAYROLL_TOKEN }}"), "done"],
            tools_override=[SCHEMA], tool_executor=None,
        )

        assert len(runner.requests) == 1
        bound = _preamble(runner.requests[0].code)
        assert bound["_args"] == {"query": "{{ global.PAYROLL_TOKEN }}"}
        assert bound["_globals"] == {"API_KEY": "the-key"}
        assert "never-this" not in runner.requests[0].code
