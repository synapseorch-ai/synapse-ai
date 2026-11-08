"""The AI builder makes a Python tool and proves it, as a person does in the tool editor (F1198, BLD1; CAP3's F-a).

The builder had `create_http_tool` and nothing for code, so "a tool that totals sales.csv by region" could only be an
HTTP call to somewhere. Now:

- `create_python_tool` saves a Python tool through the editor's own checks (`core.python_tool`: code, that parses)
  and the host's rule (cloud's plan gate), tagged as a write (its code may do anything) unless a claim is stricter;
- it never names a variable for the code to read: a person adds those in the editor (Naveen 10-29), and a replace
  keeps them;
- `test_python_tool` runs a saved Python tool once through the Test button's own route (the same runner, limits,
  variables and refusals);
- both are opt-in: an agent that names them has them, an "all" agent does not.
"""
from __future__ import annotations

import json
import types

import pytest

from core.execution import ExecResult, ExecUnsupported, register_runner

CODE = "import json\nprint(json.dumps({'total': sum(_args.get('values', []))}))\n"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def call(tool, args):
    from core.builder_tools import execute_builder_tool
    return json.loads(await execute_builder_tool(tool, args, _server()))


async def _tool(name):
    from core.store.resources import load_tools
    return next((t for t in await load_tools() if t.get("name") == name), None)


def _make(**kw):
    return {"name": "region_totals", "description": "Totals a list of numbers", "code": CODE,
            "input_schema_json": json.dumps({"type": "object", "properties": {"values": {"type": "array"}}}), **kw}


class _Runner:
    def __init__(self, result=None, raises=None):
        self.requests, self.result, self.raises = [], result, raises

    async def run(self, request):
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return self.result or ExecResult(exit_code=0, stdout='{"total": 6}\n', stderr="")


@pytest.fixture
def runner():
    r = _Runner()
    register_runner(r)
    yield r
    register_runner(None)


class TestMakingOne:
    async def test_it_is_saved_as_the_editor_saves_one(self):
        result = await call("create_python_tool", _make())
        assert result["status"] == "created" and result["reads_variables"] == []
        tool = await _tool("region_totals")
        assert tool["tool_type"] == "python" and tool["code"] == CODE
        assert tool["inputSchema"]["properties"] == {"values": {"type": "array"}}
        assert tool["globals"] == [] and "url" not in tool

    async def test_it_starts_as_a_write_never_as_a_read(self):
        await call("create_python_tool", _make())
        hints = (await _tool("region_totals"))["annotations"]
        assert hints["readOnlyHint"] is False and hints["openWorldHint"] is True
        await call("create_python_tool", _make(name="deleter", destructive=True))
        assert (await _tool("deleter"))["annotations"]["destructiveHint"] is True

    @pytest.mark.parametrize("code,words", [
        ("", "Python code cannot be empty."),
        ("   \n", "Python code cannot be empty."),
        ("def broken(:\n    pass\n", "does not parse: line 1"),
    ])
    async def test_what_the_editor_refuses_is_refused(self, code, words):
        result = await call("create_python_tool", _make(code=code))
        assert words in result["error"]
        assert await _tool("region_totals") is None

    async def test_it_names_no_variable(self):
        result = await call("create_python_tool", _make(globals=["STRIPE_KEY"]))
        assert "a person adds it" in result["error"]
        assert await _tool("region_totals") is None

    async def test_a_replace_keeps_the_variables_a_person_added(self):
        from core.store.resources import save_tool
        await call("create_python_tool", _make())
        await save_tool({**await _tool("region_totals"), "globals": ["SHEET_KEY"]})
        result = await call("create_python_tool", _make(code=CODE + "# v2\n"))
        assert result["reads_variables"] == ["SHEET_KEY"]
        tool = await _tool("region_totals")
        assert tool["globals"] == ["SHEET_KEY"] and tool["code"].endswith("# v2\n")

    async def test_an_http_tools_name_is_not_taken(self):
        from core.store.resources import save_tool
        await save_tool({"name": "region_totals", "tool_type": "http", "method": "GET", "url": "https://api.example.com/x"})
        result = await call("create_python_tool", _make())
        assert "is an HTTP tool here" in result["error"]
        assert (await _tool("region_totals"))["tool_type"] == "http"

    async def test_the_hosts_rule_is_asked(self):
        from core import python_tool
        asked = []

        async def plan(tool):
            asked.append(tool["name"])
            return "Custom Python tools are on the Team and Business plans."

        python_tool.set_check(plan)
        try:
            result = await call("create_python_tool", _make())
        finally:
            python_tool.set_check(None)
        assert result == {"error": "Custom Python tools are on the Team and Business plans."}
        assert asked == ["region_totals"] and await _tool("region_totals") is None


class TestTestingOne:
    async def test_it_runs_through_the_test_buttons_path(self, runner):
        from core.store.resources import save_tool
        await call("create_python_tool", _make())
        await save_tool({**await _tool("region_totals"), "globals": ["SHEET_KEY"]})
        result = await call("test_python_tool", {"name": "region_totals", "args_json": '{"values": [1, 2, 3]}'})
        assert result == {"exit_code": 0, "stdout": '{"total": 6}\n', "stderr": ""}
        (request,) = runner.requests
        assert CODE in request.code and '"values": [1, 2, 3]' in request.code
        assert request.timeout_s == 30

    async def test_no_sandbox_is_said_in_the_routes_words(self):
        register_runner(_Runner(raises=ExecUnsupported("Docker is not available. Cannot execute Python code.")))
        try:
            await call("create_python_tool", _make())
            result = await call("test_python_tool", {"name": "region_totals"})
        finally:
            register_runner(None)
        assert result == {"error": "Docker is not available. Cannot execute Python code."}

    async def test_only_a_saved_python_tool_runs(self, runner):
        result = await call("test_python_tool", {"name": "nothing_here"})
        assert "No Python tool named" in result["error"] and runner.requests == []

    async def test_its_output_is_read_from_the_end(self, runner):
        runner.result = ExecResult(exit_code=1, stdout="", stderr="x" * 9000 + "ValueError: last line")
        await call("create_python_tool", _make())
        result = await call("test_python_tool", {"name": "region_totals"})
        assert result["exit_code"] == 1 and result["stderr"].endswith("ValueError: last line")
        assert len(result["stderr"]) == 4000
        # A failed run is an error, to fix: never a step's finish (E2), and isError over MCP.
        assert result["error"].startswith("The test failed (exit code 1): ") and result["error"].endswith("last line")


class TestWhoHasThem:
    async def _offered(self, tools):
        from core.tools import aggregate_all_tools

        class _S:
            agent_sessions: dict = {}
        offered = (await aggregate_all_tools(_S(), {"tools": tools}, []))[0]
        return {getattr(t, "name", "") for t in offered}

    async def test_an_all_agent_does_not_and_one_that_names_them_does(self):
        assert not {"create_python_tool", "test_python_tool"} & await self._offered(["all"])
        assert {"create_python_tool", "test_python_tool"} <= await self._offered(["create_python_tool", "test_python_tool"])

    def test_they_need_a_builder(self):
        from core.builder_access import WRITES
        assert {"create_python_tool", "test_python_tool"} <= WRITES
