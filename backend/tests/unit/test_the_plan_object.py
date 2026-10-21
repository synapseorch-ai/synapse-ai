"""Synapse AI's typed objects read what models really wrote, and code alone fills in the facts (SAI2-P1, F774).

The fixtures are P0's schema-fill probe: 133 plans eight models wrote for 15 build requests
(`tests/fixtures/sai2_plans/`, Naveen's address replaced). Each reads as a Plan, and the structure check says of
each what the probe's check said — its notes word for word. The three it now refuses are the class P0 found after
its first run, an inner workflow no step runs, which is why the check exists.
"""
import json
import types
from pathlib import Path

import pytest

from core.native_builder import objects as O

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sai2_plans"
ROWS = [json.loads(line) for line in (FIXTURES / "probe_plans.jsonl").read_text().splitlines() if line.strip()]
_INV = json.loads((FIXTURES / "inventory.json").read_text())
INVENTORY = O.Inventory(agents=_INV["agents"], workflows=_INV["workflows"], tools=set(_INV["tools"]))

#: P0's finding (p0c): these defined an inner workflow and ran its work some other way.
INNER_NEVER_RUN = {("p0", "gemini-3-flash-preview", "bld-08"), ("p0", "cli.codex", "bld-08"),
                   ("p0c-nested", "claude-haiku-4-5", "bld-08")}


def _key(row):
    return row["run"], row["model"], row["persona"]


class TestWhatModelsWrote:
    def test_there_are_the_probes_plans(self):
        assert len(ROWS) == 133
        assert len({r["model"] for r in ROWS}) == 8

    @pytest.mark.parametrize("row", ROWS, ids=lambda r: "-".join(_key(r)))
    def test_each_reads_and_checks_as_the_probe_said(self, row):
        plan, errors = O.read(row["plan"])
        assert errors == [] and plan is not None
        problems, notes = O.check_structure(plan, INVENTORY)
        if _key(row) in INNER_NEVER_RUN:
            assert len(problems) == 1 and "is never run" in problems[0]
        else:
            assert problems == []
        assert notes == sorted(set(row["notes"]))

    def test_the_nested_build_runs_its_inner_workflow_through_a_workflow_step(self):
        nested = [r for r in ROWS if r["run"] == "p0c-nested" and _key(r) not in INNER_NEVER_RUN]
        assert nested
        for row in nested:
            plan, _ = O.read(row["plan"])
            assert any(s.type == "workflow" and s.workflow == w.title
                       for w in plan.inner_workflows for s in plan.steps)


def _plan(**over):
    raw = {"kind": "create", "title": "Weekly report", "summary": "Every Monday, the numbers to ops.",
           "steps": [{"id": "draft", "type": "agent", "name": "Draft", "does": "Writes it", "agent": "Writer",
                      "prompt": "Write the report.", "next": "ask"},
                     {"id": "ask", "type": "human", "name": "Approve", "does": "A person approves",
                      "prompt": "Send it?", "next": "send"},
                     {"id": "send", "type": "tool", "name": "Send", "does": "Sends it", "tool": "send_mail",
                      "next": "end"}],
           "agents": [{"name": "Writer", "purpose": "Writes reports", "tools": ["read_sheet"]}]}
    raw.update(over)
    return raw


class TestTheCheck:
    def test_a_good_plan_has_no_errors_and_names_what_is_missing(self):
        plan, _ = O.read(_plan())
        assert O.check_structure(plan, O.Inventory(tools={"read_sheet"})) == ([], ["missing tool: send_mail"])

    @pytest.mark.parametrize("change,says", [
        ({"steps": [{"id": "a", "type": "agent", "name": "A", "does": "x", "next": "b"}]}, "names its agent"),
        ({"steps": [{"id": "a", "type": "print", "name": "A", "does": "x", "next": "nowhere"}]}, "not a step id"),
        ({"steps": [{"id": "a", "type": "workflow", "name": "A", "does": "x"}]}, "names the workflow"),
        ({"steps": [{"id": "a", "type": "workflow", "name": "A", "does": "x", "workflow": "Gone"}]},
         "neither in `inner_workflows`"),
        ({"steps": [{"id": "a", "type": "if_else", "name": "A", "does": "x", "branches": {"true": "end"}}]},
         "true and false"),
        ({"steps": [{"id": "a", "type": "loop", "name": "A", "does": "x"}]}, "body steps"),
        ({"steps": [{"id": "a", "type": "human", "name": "A", "does": "x"}]}, "says what it asks"),
        ({"steps": []}, "at least one step"),
        ({"schedule": {"cron": "every monday", "says": "Mondays"}}, "five fields"),
        ({"inner_workflows": [{"title": "Inner", "does": "x", "steps": [
            {"id": "i", "type": "print", "name": "I", "does": "x"}]}]}, "is never run"),
    ])
    def test_each_error_is_one_line_back_to_the_model(self, change, says):
        plan, _ = O.read(_plan(**change))
        errors, _ = O.check_structure(plan, O.Inventory(agents={}, tools={"read_sheet", "send_mail"}))
        assert any(says in e for e in errors), errors

    def test_a_shape_the_schema_refuses_is_said_as_errors(self):
        plan, errors = O.read({"kind": "build", "title": "x"})
        assert plan is None and any(e.startswith("kind:") for e in errors) and any("summary" in e for e in errors)


class TestCodeAloneFillsTheFacts:
    def test_a_code_field_a_model_wrote_is_dropped(self):
        plan = O.Plan.from_draft(_plan(approvals={"acts_without_asking": ["send_mail"]}, missing=[],
                                       tools=[{"name": "send_mail", "status": "present"}], room=None))
        assert plan.approvals == O.Approvals() and plan.tools == [] and plan.missing == []

    def test_the_planners_schema_has_none_of_them(self):
        props = O.plan_schema()["properties"]
        assert {"tools", "approvals", "missing", "connections_needed", "room"}.isdisjoint(props)
        assert "workflow" in json.dumps(O.plan_schema())

    @pytest.fixture
    def workspace(self, monkeypatch):
        import core.routes.tools as routes_tools
        import core.tools as tools_mod
        from mcp.types import ToolAnnotations

        from core.tools import VirtualTool

        declared = {
            "read_sheet": VirtualTool("read_sheet", "reads", {}, ToolAnnotations(readOnlyHint=True)),
            "send_mail": VirtualTool("send_mail", "sends", {}, ToolAnnotations(readOnlyHint=False)),
        }

        async def one(server_module, name, agent, custom_tools):
            return declared.get(name)

        async def no_custom():
            return []

        monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
        monkeypatch.setattr(routes_tools, "load_custom_tools", no_custom)
        return types.SimpleNamespace(agent_sessions={}, tool_router={})

    async def test_present_found_and_missing(self, workspace):
        plan = O.Plan.from_draft(_plan(agents=[{"name": "Writer", "purpose": "x", "tools": ["read_sheet",
                                                                                         "mailchimp_send"]}],
                                       steps=_plan()["steps"] + [{"id": "post", "type": "tool", "name": "Post",
                                                                  "does": "x", "tool": "slack_post"}]))
        found = {"mailchimp_send": O.ToolStatus(name="mailchimp_send", status="found", provider="Mailchimp",
                                                via="mcp_server")}
        filled = await O.fill(plan, workspace, found=found)
        assert {t.name: t.status for t in filled.tools} == {
            "send_mail": "present", "slack_post": "missing", "read_sheet": "present", "mailchimp_send": "found"}
        assert filled.missing == ["slack_post"]
        assert filled.connections_needed == ["Mailchimp"]
        assert plan.tools == [], "fill works on a copy"

    async def test_what_acts_asks_first_unless_the_person_moved_it(self, workspace):
        filled = await O.fill(O.Plan.from_draft(_plan()), workspace)
        assert filled.approvals == O.Approvals(asks_first=["send_mail"])
        filled.approvals = O.Approvals(acts_without_asking=["send_mail"])
        again = await O.fill(filled, workspace)
        assert again.approvals == O.Approvals(acts_without_asking=["send_mail"])

    async def test_the_plan_room_is_the_hosts(self, workspace):
        from core import builder_tools

        asked = []

        async def room(kind, adding):
            asked.append((kind, adding))
            return "Your plan has room for 2 workflows; you have 2." if kind == "orchestration" else None

        builder_tools.set_room_check(room)
        try:
            filled = await O.fill(O.Plan.from_draft(_plan()), workspace)
        finally:
            builder_tools.set_room_check(None)
        assert filled.room == "Your plan has room for 2 workflows; you have 2."
        assert asked[0] == ("orchestration", 1)
        assert (await O.fill(O.Plan.from_draft(_plan()), workspace)).room is None


class TestARequestAChangeATurn:
    def test_a_request_has_four_labels(self):
        assert O.Request.model_validate({"kind": "research", "providers": ["Mailchimp"]}).providers == ["Mailchimp"]
        with pytest.raises(Exception):
            O.Request.model_validate({"kind": "cantdo"})

    def test_a_changes_ops_are_a_closed_list_and_acts_are_told_from_writes(self):
        change = O.Change.model_validate({"ops": [
            {"op": "schedule_off", "target": {"kind": "schedule", "name": "Morning digest"}},
            {"op": "set_schedule", "target": {"kind": "schedule", "id": "sch_1"}, "fields": {"cron": "0 9 * * 1"}},
        ]})
        assert [op.is_act for op in change.ops] == [True, False]
        assert change.has_writes
        with pytest.raises(Exception):
            O.ChangeOp.model_validate({"op": "drop_database", "target": {"kind": "workflow"}})

    def test_buttons_only_has_no_writes(self):
        change = O.Change.model_validate({"ops": [{"op": "run_workflow", "target": {"kind": "workflow", "id": "o"}}]})
        assert not change.has_writes

    def test_a_turn_is_codes(self):
        assert O.Turn(route="pick", picked="orch_2").picked == "orch_2"
