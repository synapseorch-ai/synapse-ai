"""What a Python tool printed or raised is shown without a secret, wherever its test is shown (F1381; eb 11-04).

A tool's test shows its code's own output: the Tools screen's Test, `test_python_tool` (Synapse AI's, and an MCP
client's at Builder), the change's review of a fix (F1325), and "Test this fix" (F1380). Its code reads the workspace
variables it declares, and what it prints or raises may hold one. Each passes through one mask, `masked_output`: a
declared variable is said by its name wherever its value appears, a credential's shape is masked, and so is every held
secret. Output that holds none is shown as it is, so the Test stays worth pressing.
"""
from __future__ import annotations

import json
import types

import pytest

KEY = "sk_live_" + "9c1e5a" + "03bb77f2"
TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
TRACEBACK = f"Traceback (most recent call last):\n  File \"<code>\", line 3\nValueError: refused {KEY}"


@pytest.fixture
def workspace(monkeypatch):
    """A Python tool that reads SHOP_KEY, and a runner that answers what a test sets."""
    import core.execution as execution
    import core.store.resources as resources
    import core.variables as variables_mod

    box = {"stdout": "", "stderr": "", "exit_code": 0}

    class _Runner:
        async def run(self, request):
            return types.SimpleNamespace(stdout=box["stdout"], stderr=box["stderr"], exit_code=box["exit_code"],
                                         timed_out=False, ok=box["exit_code"] == 0)

    async def variables():
        return {"SHOP_KEY": KEY, "REGION": "eu"}

    async def tools():
        return [{"name": "region_totals", "tool_type": "python", "globals": ["SHOP_KEY"], "code": "print(1)"}]

    monkeypatch.setattr(execution, "get_runner", lambda: _Runner())
    monkeypatch.setattr(variables_mod, "load", variables)
    monkeypatch.setattr(resources, "load_tools", tools)
    return box


def _never_said(text: str) -> None:
    assert KEY not in text and TOKEN not in text, text


class TestTheToolsScreensTest:
    async def test_a_variables_value_and_a_token_are_masked(self, workspace):
        from core.routes.tools import PythonTestRequest, test_python_tool

        workspace.update(stdout=f"calling with {KEY}", stderr=f"{TRACEBACK}\nalso {TOKEN}", exit_code=1)
        ran = await test_python_tool(PythonTestRequest(code="print(1)", globals=["SHOP_KEY"]))
        _never_said(json.dumps(ran))
        assert "SHOP_KEY" in ran["stdout"] and "SHOP_KEY" in ran["stderr"], ran

    async def test_a_plain_print_is_shown_as_it_is(self, workspace):
        from core.routes.tools import PythonTestRequest, test_python_tool

        workspace.update(stdout='{"total": 3.23, "region": "eu"}\n', stderr="", exit_code=0)
        ran = await test_python_tool(PythonTestRequest(code="print(1)", globals=["SHOP_KEY"]))
        assert ran["stdout"] == '{"total": 3.23, "region": "eu"}\n', ran


class TestSynapseAIsAndAnMCPClientsTest:
    async def test_the_models_answer_is_masked(self, workspace):
        from core.builder_tools import execute_builder_tool_with_events

        workspace.update(stdout=f"calling with {KEY}", stderr=TRACEBACK, exit_code=1)
        raw, _ = await execute_builder_tool_with_events(
            "test_python_tool", {"name": "region_totals", "args_json": "{}"}, object())
        _never_said(raw)
        assert "SHOP_KEY" in raw, raw

    async def test_an_mcp_clients_answer_is_masked(self, workspace, client):
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller
        from core.product_tools.errors import ToolRefusal
        from core.product_tools.roles import Role

        areas.ensure_loaded()
        workspace.update(stdout=f"printed {TOKEN}", stderr=TRACEBACK, exit_code=1)
        try:
            said = json.dumps(await registry.invoke("test_python_tool", Caller(role=Role.OWNER, via="mcp",
                                                                               user_id="u1"),
                                                    {"name": "region_totals", "args_json": "{}"}), default=str)
        except ToolRefusal as refused:
            said = str(refused)
        _never_said(said)


class TestAChangesReview:
    async def test_its_fixs_printed_lines_are_masked_before_the_review(self, workspace, monkeypatch):
        """F1325's test before the yes, with no network: what each sample printed reaches the review's detail."""
        from core import python_edits

        async def ran(code, globals_, samples, network):
            return [{"args": {"rows": [5]}, "ok": True, "printed": f"total with {KEY}"}]

        monkeypatch.setattr(python_edits, "run_samples", ran)
        from core.native_builder import change_tools

        fields, why = await change_tools._code_tested(
            {"kind": "tool", "name": "region_totals"},
            {"code": "print(1)", "test_args": [{"rows": [5]}]}, {}, send_back=False)
        assert why is None, why
        _never_said(json.dumps(fields))
        assert "SHOP_KEY" in json.dumps(fields), fields


class TestTheMaskItself:
    def test_a_short_value_never_masks_ordinary_words(self):
        from core.secret_scrub import masked_output

        assert masked_output("region eu, total 3", {"REGION": "eu"}) == "region eu, total 3"

    def test_a_declared_value_is_said_by_its_name(self):
        from core.secret_scrub import masked_output

        said = masked_output(f"key {KEY}", {"SHOP_KEY": KEY})
        assert KEY not in said and "SHOP_KEY" in said
