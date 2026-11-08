"""F1198 over MCP (BLD1; Naveen 10-29): a Python tool is made by an outside client the way a delete is done.

`create_python_tool` is served to a builder, in the build scope, marked destructive, and done only on a second call:
the first previews the tool with its code whole (or refuses now, in the editor's words) and gives a token; the
second, with that token and the same arguments, saves it and is audited. `test_python_tool` runs a saved one through
the editor's Test route. Neither is offered to a viewer or an approver.
"""
from __future__ import annotations

import json

import pytest

from api_app.test_mcp_server import _body, mcp_app, mcp_session  # noqa: F401  mcp_app is a fixture
from core.execution import ExecResult, register_runner
from core.mcp_server import server
from core.product_tools import hooks
from core.product_tools.caller import Caller
from core.product_tools.roles import Role

CODE = "print('{\"ok\": true}')\n"
ARGS = {"name": "says_ok", "description": "Says ok", "code": CODE}


@pytest.fixture
def audited():
    events: list[hooks.AuditEvent] = []

    async def record(event):
        events.append(event)

    hooks.set_audit(record)
    yield events
    hooks.set_audit(None)


async def _tool(name):
    from core.store.resources import load_tools
    return next((t for t in await load_tools() if t.get("name") == name), None)


def _offered(role: Role) -> dict:
    return {t.name: t for t in server._fixed_tools(Caller(role=role, via="mcp", user_id="u1"))}


class TestWhoIsOfferedThem:
    def test_a_builder_is_and_they_are_marked(self):
        tools = _offered(Role.BUILDER)
        make, test = tools["create_python_tool"], tools["test_python_tool"]
        assert make.annotations.destructiveHint is True and make.annotations.readOnlyHint is False
        assert "confirm_token" in make.inputSchema["properties"]
        assert test.annotations.openWorldHint is True and "confirm_token" not in test.inputSchema["properties"]

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
    def test_no_one_less_is(self, role):
        assert not {"create_python_tool", "test_python_tool"} & set(_offered(role))


class TestMakingOne:
    async def test_previewed_with_its_code_then_made_and_audited(self, mcp_app, api_key, audited):
        async with mcp_session(mcp_app, api_key) as s:
            first = _body(await s.call_tool("create_python_tool", ARGS))
            assert first["status"] == "needs_confirmation"
            assert first["preview"]["will_create"]["code"] == CODE
            assert first["preview"]["replaces"] is False and first["preview"]["reads_variables"] == []
            assert await _tool("says_ok") is None and audited == []
            done = await s.call_tool("create_python_tool", {**ARGS, "confirm_token": first["confirm_token"]})
        assert not done.isError, done.content[0].text
        assert (await _tool("says_ok"))["code"] == CODE
        assert [(e.action, e.target_id) for e in audited] == [("tool.created", "says_ok")]

    async def test_a_token_for_other_code_makes_nothing(self, mcp_app, api_key):
        async with mcp_session(mcp_app, api_key) as s:
            token = _body(await s.call_tool("create_python_tool", ARGS))["confirm_token"]
            res = await s.call_tool("create_python_tool", {**ARGS, "code": "import os\n", "confirm_token": token})
        assert res.isError and "different call" in res.content[0].text
        assert await _tool("says_ok") is None

    async def test_code_that_does_not_parse_is_refused_at_the_preview(self, mcp_app, api_key):
        async with mcp_session(mcp_app, api_key) as s:
            res = await s.call_tool("create_python_tool", {**ARGS, "code": "def f(:\n"})
        assert res.isError and "does not parse" in res.content[0].text
        assert "confirm_token" not in res.content[0].text


class TestTestingOne:
    async def test_it_runs_through_the_test_route_and_is_audited(self, mcp_app, api_key, audited):
        from core.store.resources import save_tool

        class Runner:
            async def run(self, request):
                return ExecResult(exit_code=0, stdout='{"ok": true}\n', stderr="")

        await save_tool({"name": "says_ok", "tool_type": "python", "code": CODE, "inputSchema": {}, "globals": []})
        register_runner(Runner())
        try:
            async with mcp_session(mcp_app, api_key) as s:
                res = await s.call_tool("test_python_tool", {"name": "says_ok"})
        finally:
            register_runner(None)
        assert not res.isError, res.content[0].text
        assert json.loads(res.content[0].text)["stdout"] == '{"ok": true}\n'
        assert [e.action for e in audited] == ["tool.tested"]
