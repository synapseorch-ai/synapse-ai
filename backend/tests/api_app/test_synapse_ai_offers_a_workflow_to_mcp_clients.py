"""Synapse AI can offer a workflow to MCP clients, or stop offering it, as the workflow's own switch does (F1696).

Each workflow has its own "Offer to MCP clients" switch (Workflow settings): on, Claude and other apps connected over
MCP see it as a tool of its own. Synapse AI could not set it: the change planner's `update_workflow` had no such
field and `set_orchestration_meta` left it out of its schema and of the fields it sets, so asked to offer a workflow
it said the only way was Settings › MCP server, which connects a client and offers nothing.

Now `update_workflow` takes `offer_to_mcp` (true or false), its card says "Offer "X" to MCP clients" or "Stop offering
"X" to MCP clients", `set_orchestration_meta` sets it, and the "?" panel's MCP server help names the workflow's own
switch.

Near misses: a value that is not true or false is refused; the other workflow settings are worded as before.
"""
from __future__ import annotations

import json
from pathlib import Path

from _fakes import seed as S
from core.builder_tools import BUILDER_TOOL_SCHEMAS, execute_builder_tool
from core.native_builder import change_ops as C
from core.native_builder import change_tools as T

ROOT = Path(__file__).resolve().parents[3]
TARGET = {"kind": "workflow", "name": "Weekly report"}


class _Server:
    agent_sessions: dict = {}


class TestTheChangeOp:
    def test_update_workflow_takes_offer_to_mcp(self):
        assert C.issues("update_workflow", TARGET, {"offer_to_mcp": True}) == []
        assert C.issues("update_workflow", TARGET, {"offer_to_mcp": False}) == []

    def test_a_value_that_is_not_true_or_false_is_refused(self):
        said = C.issues("update_workflow", TARGET, {"offer_to_mcp": "yes"})
        assert any("`offer_to_mcp` is true or false" in s for s in said), said

    def test_the_planner_is_told_the_field(self):
        assert "offer_to_mcp" in T.SUBMIT["function"]["description"]


class TestTheCardsWords:
    def test_offer_and_stop_offering(self):
        op = C.OPS["update_workflow"]
        assert T._words(op, TARGET, {"offer_to_mcp": True}, "") == 'Offer "Weekly report" to MCP clients'
        assert T._words(op, TARGET, {"offer_to_mcp": False}, "") == 'Stop offering "Weekly report" to MCP clients'

    def test_other_settings_are_worded_as_before(self):
        op = C.OPS["update_workflow"]
        assert T._words(op, TARGET, {"timeout_minutes": 30}, "") == \
            'Change the settings of the workflow "Weekly report" — timeout_minutes: 30'


class TestSetOrchestrationMeta:
    def test_its_schema_has_the_switch(self):
        meta = next(t for t in BUILDER_TOOL_SCHEMAS if t["function"]["name"] == "set_orchestration_meta")
        assert meta["function"]["parameters"]["properties"]["offer_to_mcp"]["type"] == "boolean"

    async def test_it_sets_the_switch(self, client):
        made = await client.post("/api/orchestrations", json=S.make_orchestration(id="orch_f1696", name="Weekly"))
        assert made.status_code == 200, made.text
        out = json.loads(await execute_builder_tool("set_orchestration_meta",
                                                    {"orch_id": "orch_f1696", "offer_to_mcp": True}, _Server()))
        assert out.get("fields") == ["offer_to_mcp"], out
        assert (await client.get("/api/orchestrations/orch_f1696")).json()["offer_to_mcp"] is True


class TestTheHelp:
    def test_the_mcp_server_help_names_the_workflow_s_own_switch(self):
        help_ts = (ROOT / "frontend" / "src" / "lib" / "help.ts").read_text(encoding="utf-8")
        topics = json.loads((ROOT / "backend" / "core" / "native_builder" / "help_topics.json").read_text())
        assert "Workflow settings › Offer to MCP clients" in help_ts
        assert any("Workflow settings › Offer to MCP clients" in s for s in topics["mcp_server"]["steps"])
