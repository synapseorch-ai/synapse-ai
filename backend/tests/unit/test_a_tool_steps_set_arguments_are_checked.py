"""A Tool step's set arguments are checked before a run finds them wrong (E1, SAI2-P1, D300).

Set on the step, the arguments meet the tool with no model to repair them: a name the tool does not take, a required
one left out, or a state key nothing writes would fail every run. The validator says so at save, the way it says a
forced tool nothing provides will fail (F489).
"""
import types

import pytest

from core.builder_tools import _validate_orchestration
from core.tools import VirtualTool

SCHEMA = {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["to"]}


def _orch(arguments, *, schema=None):
    return {
        "id": "orch_v", "name": "V", "entry_step_id": "draft", "state_schema": schema or {},
        "steps": [
            {"id": "draft", "name": "Draft", "type": "transform", "transform_code": "result = 1",
             "output_key": "draft", "next_step_id": "send"},
            {"id": "send", "name": "Send", "type": "tool", "forced_tool": "send_mail", "tool_arguments": arguments,
             "output_key": "sent",
             "next_step_id": "end"},
            {"id": "end", "name": "End", "type": "end"},
        ],
    }


class TestTheReferences:
    def test_a_key_an_earlier_step_writes_is_fine(self):
        assert _validate_orchestration(_orch({"to": "{{state.draft.to}}", "body": "{state.user_input}"}))["ok"]

    def test_a_key_nothing_writes_is_an_issue(self):
        verdict = _validate_orchestration(_orch({"to": "{{state.recipient}}"}))
        assert not verdict["ok"]
        assert any("'send'" in i and "recipient" in i for i in verdict["issues"])

    def test_a_key_the_schema_declares_counts(self):
        assert _validate_orchestration(_orch({"to": "{{state.recipient}}"},
                                             schema={"recipient": {"type": "str"}}))["ok"]

    def test_references_inside_nested_values_are_checked(self):
        verdict = _validate_orchestration(_orch({"to": "a", "body": ["x {{state.nowhere}}"]}))
        assert any("nowhere" in i for i in verdict["issues"])

    def test_no_set_arguments_is_unchanged(self):
        assert _validate_orchestration(_orch(None))["ok"]


class TestOnlyAToolStepTakesThem:
    def test_set_arguments_on_another_step_type_are_refused(self):
        from pydantic import ValidationError

        from core.models_orchestration import StepConfig

        with pytest.raises(ValidationError, match="tool_arguments"):
            StepConfig(id="a", name="A", type="agent", tool_arguments={"x": 1})
        assert StepConfig(id="t", name="T", type="tool", forced_tool="x", tool_arguments={"x": 1}).tool_arguments


class TestTheToolsOwnSchema:
    @pytest.fixture
    def tools(self, monkeypatch):
        import core.routes.tools as routes_tools
        import core.tools as tools_mod

        declared = {"send_mail": VirtualTool("send_mail", "sends", SCHEMA)}

        async def one(server_module, name, agent, custom_tools):
            return declared.get(name)

        async def no_custom():
            return []

        monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
        monkeypatch.setattr(routes_tools, "load_custom_tools", no_custom)
        return types.SimpleNamespace(agent_sessions={}, tool_router={})

    async def test_arguments_the_tool_takes_are_fine(self, tools):
        from core import builder_guard

        assert await builder_guard.argument_issues([_orch({"to": "a", "body": "b"})], tools) == []

    async def test_a_name_the_tool_does_not_take_is_an_issue(self, tools):
        from core import builder_guard

        issues = await builder_guard.argument_issues([_orch({"to": "a", "subject": "b"})], tools)
        assert len(issues) == 1 and "subject" in issues[0] and "send_mail" in issues[0]
        assert "to, body" in issues[0] or "body, to" in issues[0]

    async def test_a_required_argument_left_out_is_an_issue(self, tools):
        from core import builder_guard

        issues = await builder_guard.argument_issues([_orch({"body": "b"})], tools)
        assert len(issues) == 1 and "'to'" in issues[0]

    async def test_a_tool_nothing_provides_is_left_to_f489(self, tools):
        from core import builder_guard

        orch = _orch({"anything": 1})
        orch["steps"][1]["forced_tool"] = "gone"
        assert await builder_guard.argument_issues([orch], tools) == []
