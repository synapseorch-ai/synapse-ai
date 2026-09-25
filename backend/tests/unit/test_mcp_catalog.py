"""What Synapse serves over MCP, and how each tool is marked (D125).

The allowlist is the policy: deletes stay in the app, where a person approves them,
and a builder tool added later is not served until someone decides it should be.
"""
from __future__ import annotations

import pytest

from core.builder_tools import BUILDER_TOOL_NAMES
from core.mcp_server import catalog, runs, workflows


class TestTheAllowlist:
    def test_exactly_the_agreed_builder_tools_are_served(self):
        assert catalog.SERVED_BUILDER_TOOLS == {
            "list_agents", "get_agent", "list_orchestrations", "get_orchestration",
            "validate_orchestration", "list_tool_servers", "list_server_tools", "list_all_tools",
            "get_tools_detail", "list_custom_tools", "list_skills", "get_skill", "list_schedules",
            "list_repos", "list_db_configs",
            "create_agent", "create_agents", "update_agent", "create_orchestration",
            "create_orchestration_skeleton", "add_steps", "add_single_step", "add_multiple_steps",
            "update_step", "remove_step", "update_orchestration", "set_orchestration_meta",
            "create_http_tool", "import_openapi", "create_skill", "update_skill", "attach_skills",
            "create_schedule", "set_schedule_enabled",
        }

    @pytest.mark.parametrize("name", [
        "delete_agent", "delete_orchestration", "delete_custom_tool", "delete_schedule",
        "finish_build", "list_variables",
    ])
    def test_deletes_and_the_rest_are_not_served(self, name):
        assert name in BUILDER_TOOL_NAMES  # still a builder tool…
        assert name not in catalog.SERVED_BUILDER_TOOLS  # …not an MCP one
        assert name not in {t.name for t in catalog.builder_tools()}

    def test_every_served_name_is_a_real_builder_tool(self):
        assert catalog.SERVED_BUILDER_TOOLS <= BUILDER_TOOL_NAMES
        assert [t.name for t in catalog.builder_tools()] == list(catalog.READ_TOOLS + catalog.BUILD_TOOLS)

    def test_run_tools_do_not_shadow_builder_tools(self):
        assert not (runs.RUN_TOOL_NAMES & BUILDER_TOOL_NAMES)


class TestAnnotations:
    """MCP clients assume a tool is destructive unless it says it is not."""

    def test_every_tool_states_both_hints(self):
        for tool in catalog.builder_tools() + list(runs.RUN_TOOLS):
            assert tool.annotations is not None, tool.name
            assert tool.annotations.readOnlyHint is not None, tool.name
            assert tool.annotations.destructiveHint is not None, tool.name

    def test_reads_are_read_only(self):
        marks = {t.name: t.annotations for t in catalog.builder_tools()}
        for name in catalog.READ_TOOLS:
            assert marks[name].readOnlyHint and not marks[name].destructiveHint

    @pytest.mark.parametrize("name", ["update_orchestration", "remove_step", "create_http_tool", "create_skill"])
    def test_overwriters_are_destructive(self, name):
        mark = {t.name: t.annotations for t in catalog.builder_tools()}[name]
        assert mark.destructiveHint and not mark.readOnlyHint

    def test_additive_builds_are_not_destructive(self):
        marks = {t.name: t.annotations for t in catalog.builder_tools()}
        for name in set(catalog.BUILD_TOOLS) - catalog.OVERWRITING_TOOLS:
            assert marks[name].destructiveHint is False, name



class TestScopes:
    """What a caller must have been granted to see and call each tool (D127).

    An embedder may grant less than everything: cloud's OAuth tokens carry `read`,
    `run` and `build`. A key, and every self-hosted caller, is granted all three.
    """

    def test_every_served_tool_has_one_scope(self):
        for tool in catalog.builder_tools() + list(runs.RUN_TOOLS):
            assert catalog.scope_of(tool.name) in catalog.SCOPES, tool.name

    def test_reading_is_read(self):
        for name in catalog.READ_TOOLS:
            assert catalog.scope_of(name) == "read", name
        assert catalog.scope_of("get_run") == "read"
        assert catalog.scope_of("list_runs") == "read"

    def test_building_is_build(self):
        for name in catalog.BUILD_TOOLS:
            assert catalog.scope_of(name) == "build", name

    @pytest.mark.parametrize("name", ["run_orchestration", "answer_run", "cancel_run", "chat_with_agent"])
    def test_starting_answering_cancelling_and_chatting_spend_credits(self, name):
        assert catalog.scope_of(name) == "run"

    def test_a_workflow_tool_runs(self):
        assert catalog.scope_of(workflows.PREFIX + "weekly_client_summary") == "run"

    @pytest.mark.parametrize("name", ["delete_agent", "finish_build", "no_such_tool"])
    def test_what_is_not_served_has_no_scope(self, name):
        assert catalog.scope_of(name) is None

    def test_a_read_scope_only_reads(self):
        """A token granted `read` alone must see nothing that changes anything."""
        marks = {t.name: t.annotations for t in catalog.builder_tools() + list(runs.RUN_TOOLS)}
        for name, mark in marks.items():
            if catalog.scope_of(name) == "read":
                assert mark.readOnlyHint, name

def _orch(oid, name, **extra):
    return {"id": oid, "name": name, "description": extra.pop("description", ""), "steps": [], **extra}


class TestWorkflowTools:
    def test_a_workflow_is_named_after_itself(self):
        named = workflows.name_workflows([_orch("orch_1", "Weekly client summary!")], set())
        assert list(named) == ["run_weekly_client_summary"]

    def test_names_fit_mcp_and_collide_safely(self):
        orchs = [
            _orch("orch_aaaa1111", "Report", created_at="1"),
            _orch("orch_bbbb2222", "report", created_at="2"),
            _orch("orch_cccc3333", "x" * 200, created_at="3"),
            _orch("orch_dddd4444", "Orchestration", created_at="4"),
        ]
        named = workflows.name_workflows(orchs, reserved={"run_orchestration"})
        names = list(named)
        assert names[0] == "run_report"
        assert names[1].startswith("run_report_") and names[1] != names[0]
        assert all(len(n) <= 64 for n in names)
        assert "run_orchestration" not in names  # a fixed tool keeps its name
        assert len(set(names)) == 4
        import re
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) for n in names)

    def test_a_nameless_workflow_falls_back_to_its_id(self):
        assert list(workflows.name_workflows([_orch("orch_z9", "")], set())) == ["run_orch_z9"]

    def test_declared_inputs_are_described_keys_no_step_writes(self):
        orch = _orch("orch_1", "Brief", state_schema={
            "client": {"type": "string", "description": "Which client"},
            "summary": {"type": "string", "description": "The result"},
            "internal": {"type": "string"},
            "message": {"type": "string", "description": "shadows the tool's own argument"},
        }, steps=[{"id": "s", "output_key": "summary"}])
        tool = workflows.as_tool("run_brief", orch)
        props = tool.inputSchema["properties"]
        assert set(props) == {"message", "client", "wait_seconds"}
        assert props["client"]["type"] == "string"
        assert tool.inputSchema["required"] == ["message"]
        assert workflows.split_args(orch, {"message": "go", "client": "Acme", "summary": "x"}) == (
            "go", {"client": "Acme"},
        )

    def test_a_workflow_tool_is_described_in_its_own_words(self):
        tool = workflows.as_tool("run_brief", _orch("orch_1", "Brief", description="Drafts the brief."))
        assert tool.description.startswith("Drafts the brief.")
        assert tool.annotations.destructiveHint is False


class TestResults:
    def test_a_long_result_is_cut_with_a_note(self):
        from core.mcp_server.server import RESULT_CHARS, _text_result

        text = _text_result("x" * (RESULT_CHARS + 10)).content[0].text
        assert len(text) < RESULT_CHARS + 100
        assert text.endswith("[cut: 10 more characters]")

    def test_a_run_reads_one_line_per_step(self):
        view = {"status": "completed", "run_id": "r", "response": "ok",
                "step_history": [{"step_id": "a", "step_name": "A", "step_type": "llm", "status": "completed",
                                  "started_at": "t", "output_key": "o"}],
                "shared_state": {"o": "y" * 5000}}
        out = runs._for_model(view)
        assert out["steps"] == [{"step_id": "a", "step_name": "A", "step_type": "llm", "status": "completed"}]
        assert len(out["shared_state"]["o"]) < 4100
