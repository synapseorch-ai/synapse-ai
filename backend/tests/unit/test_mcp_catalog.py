"""What Synapse serves over MCP, and how each tool is marked (D125).

The allowlist is the policy: deletes stay in the app, where a person approves them,
and a builder tool added later is not served until someone decides it should be.
"""
from __future__ import annotations

import pytest

from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from core.builder_tools import BUILDER_TOOL_NAMES, KNOWLEDGE_BUILDER_TOOLS
from core.mcp_server import catalog, runs, server, workflows


@pytest.fixture
async def on(monkeypatch):
    """Knowledge on, so every allowlisted tool is listed."""
    use_saved_settings(monkeypatch)
    await enable_knowledge()


@pytest.fixture
def off(monkeypatch):
    use_saved_settings(monkeypatch)  # the shipped default


class TestTheAllowlist:
    def test_exactly_the_agreed_builder_tools_are_served(self):
        assert catalog.SERVED_BUILDER_TOOLS == {
            "list_agents", "get_agent", "list_orchestrations", "get_orchestration",
            "validate_orchestration", "list_tool_servers", "list_server_tools", "list_all_tools",
            "get_tools_detail", "list_custom_tools", "list_skills", "get_skill", "list_schedules",
            "list_repos", "list_db_configs", "list_knowledge_bases",
            "create_agent", "create_agents", "update_agent", "create_orchestration",
            "create_orchestration_skeleton", "add_steps", "add_single_step", "add_multiple_steps",
            "update_step", "remove_step", "update_orchestration", "set_orchestration_meta",
            "create_http_tool", "import_openapi", "create_skill", "update_skill", "attach_skills",
            "create_schedule",
            # F1451 (Naveen 11-07): on and off as two tools, turning on asking first; the on/off switch is the app's.
            "turn_on_schedule", "pause_schedule",
            # F694 (MCP2, D292): builder tools nothing had decided to hold back.
            "deploy_orchestration", "update_schedule",
            # D292 (Naveen): deletes, behind a confirmation token.
            "delete_agent", "delete_orchestration", "delete_schedule", "delete_custom_tool",
            # F1198 (BLD1, Naveen 10-29): a Python tool, previewed with its code and made with a token; its test
            # run, through the editor's Test route.
            "create_python_tool", "test_python_tool",
            # F1312b (Naveen 11-04): an HTTP tool's address changed in place, never its headers; destructive, so the
            # client asks first.
            "update_http_tool",
            # F1325 (Naveen 11-04): a Python tool's code changed in place, previewed with its new code, made with a
            # token. Its fix's test on a person's press, test_code_change, is the app's own (below).
            "update_python_tool",
        }

    @pytest.mark.parametrize("name", ["finish_build", "list_variables", "test_code_change"])
    def test_the_rest_are_not_served(self, name):
        assert name in BUILDER_TOOL_NAMES  # still a builder tool…
        assert name not in catalog.SERVED_BUILDER_TOOLS  # …not an MCP one
        assert name not in {t.name for t in catalog.builder_tools()}

    def test_every_served_name_is_a_real_builder_tool(self, on):
        assert catalog.SERVED_BUILDER_TOOLS <= BUILDER_TOOL_NAMES
        assert [t.name for t in catalog.builder_tools()] == list(
            catalog.READ_TOOLS + catalog.BUILD_TOOLS + catalog.DELETE_TOOLS)

    def test_while_knowledge_is_off_the_rest_are_listed_in_order(self, off):
        assert [t.name for t in catalog.builder_tools()] == [
            n for n in catalog.READ_TOOLS + catalog.BUILD_TOOLS + catalog.DELETE_TOOLS
            if n not in KNOWLEDGE_BUILDER_TOOLS
        ]

    def test_run_tools_do_not_shadow_builder_tools(self):
        assert not (runs.RUN_TOOL_NAMES & BUILDER_TOOL_NAMES)


@pytest.mark.usefixtures("on")
class TestAnnotations:
    """MCP clients assume a tool is destructive unless it says it is not."""

    def test_every_tool_states_both_hints(self):
        for tool in server._fixed_tools():
            assert tool.annotations is not None, tool.name
            assert tool.annotations.readOnlyHint is not None, tool.name
            assert tool.annotations.destructiveHint is not None, tool.name

    def test_every_tool_has_a_title(self):
        """F1546 (DIR1): Claude's directory flags a tool without one. Said twice, as the spec has it: the tool's own
        `title` and its annotations' (a client reads one or the other)."""
        for tool in [*server._fixed_tools(), *catalog.builder_tools()]:
            assert tool.title and tool.title.strip(), tool.name
            assert tool.annotations.title == tool.title, tool.name
        titles = {t.name: t.title for t in server._fixed_tools()}
        assert titles["create_http_tool"] == "Create HTTP tool"
        assert titles["get_tools_detail"] == "Get tool details"

    def test_a_description_sends_a_client_only_to_tools_it_is_offered(self):
        """F1546 (DIR1): a directory's reviewer calls what a description names, and a description must say what the
        server does. `update_schedule` told a client to pause or resume with `set_schedule_enabled`, the app's own
        card's, which MCP never serves (F1451 gave it turn_on_schedule and pause_schedule)."""
        import re

        from core.product_tools import registry

        known = BUILDER_TOOL_NAMES | {t.name for t in registry.all_tools()}
        served = {t.name for t in server._fixed_tools()}
        for tool in server._fixed_tools():
            named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", tool.description)) & known
            assert named <= served, f"{tool.name} names {sorted(named - served)}, which MCP does not serve"

    def test_reads_are_read_only(self):
        marks = {t.name: t.annotations for t in catalog.builder_tools()}
        for name in catalog.READ_TOOLS:
            assert marks[name].readOnlyHint and not marks[name].destructiveHint

    @pytest.mark.parametrize("name", ["update_orchestration", "remove_step", "create_http_tool", "create_skill",
                                      "update_http_tool", "update_python_tool"])
    def test_overwriters_are_destructive(self, name):
        mark = {t.name: t.annotations for t in catalog.builder_tools()}[name]
        assert mark.destructiveHint and not mark.readOnlyHint

    def test_additive_builds_are_not_destructive(self):
        marks = {t.name: t.annotations for t in catalog.builder_tools()}
        for name in set(catalog.BUILD_TOOLS) - catalog.OVERWRITING_TOOLS:
            assert marks[name].destructiveHint is False, name



@pytest.mark.usefixtures("on")
class TestScopes:
    """What a caller must have been granted to see and call each tool (D127).

    An embedder may grant less than everything: cloud's OAuth tokens carry `read`,
    `run` and `build`. A key, and every self-hosted caller, is granted all three.
    """

    def test_every_served_tool_has_one_scope(self):
        for tool in server._fixed_tools():
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

    @pytest.mark.parametrize("name", ["finish_build", "list_variables", "no_such_tool"])
    def test_what_is_not_served_has_no_scope(self, name):
        assert catalog.scope_of(name) is None

    def test_a_read_scope_only_reads(self):
        """A token granted `read` alone must see nothing that changes anything."""
        marks = {t.name: t.annotations for t in server._fixed_tools()}
        assert "search_knowledge" in marks
        for name, mark in marks.items():
            if catalog.scope_of(name) == "read":
                assert mark.readOnlyHint, name

class TestKnowledgeBases:
    """`list_knowledge_bases` is served only while Knowledge is on (R1c, D173): a client writing a Retrieve or
    Ingest step needs the bases' ids, and while Knowledge is off the tool exists nowhere."""

    def test_it_is_listed_while_knowledge_is_on(self, on):
        tool = {t.name: t for t in catalog.builder_tools()}["list_knowledge_bases"]
        assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
        assert catalog.scope_of("list_knowledge_bases") == "read"

    def test_it_is_not_listed_while_knowledge_is_off(self, off):
        assert "list_knowledge_bases" not in {t.name for t in catalog.builder_tools()}

    def test_every_knowledge_builder_tool_is_served(self):
        assert KNOWLEDGE_BUILDER_TOOLS <= catalog.SERVED_BUILDER_TOOLS


class TestKnowledgeSearch:
    """`search_knowledge` over MCP (P1a, D176): a read, listed only while Knowledge is on, with the agent
    tool's flat schema. Not a builder tool, so it is not on the builder allowlist."""

    def test_it_is_listed_while_knowledge_is_on(self, on):
        from core.knowledge.agent_contract import SEARCH_SCHEMA

        tool = {t.name: t for t in server._fixed_tools()}["search_knowledge"]
        assert tool.inputSchema == SEARCH_SCHEMA
        marks = tool.annotations
        assert marks.readOnlyHint and marks.destructiveHint is False and marks.openWorldHint is False
        assert "read_knowledge" not in tool.description

    def test_it_is_not_listed_while_knowledge_is_off(self, off):
        assert "search_knowledge" not in {t.name for t in server._fixed_tools()}

    def test_it_is_a_read_and_reserved(self):
        assert catalog.scope_of("search_knowledge") == "read"
        assert "search_knowledge" in server._reserved()
        assert "search_knowledge" not in catalog.SERVED_BUILDER_TOOLS
        assert "read_knowledge" not in server._reserved() and catalog.scope_of("read_knowledge") is None

    def test_the_calling_key_is_named_whatever_the_host(self):
        import uuid
        from types import SimpleNamespace

        from core.mcp_server.knowledge import key_id

        key = uuid.uuid4()
        assert key_id({"id": "key_1"}) == "key_1"  # OSS: the key's record
        assert key_id(SimpleNamespace(api_key_id=key)) == str(key)  # cloud: its principal
        assert key_id(SimpleNamespace(api_key_id=None)) is None  # an OAuth grant
        assert key_id(None) is None


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
        [line] = out["steps"]
        assert {k: line[k] for k in ("step_id", "step_name", "step_type", "status")} == {
            "step_id": "a", "step_name": "A", "step_type": "llm", "status": "completed"}
        assert len(out["shared_state"]["o"]) < 4100

    def test_each_step_says_the_start_of_what_it_wrote_and_how_long_it_took(self):
        """"Did it finish, and what did each step do" is one read (ASSIST1, D291)."""
        view = {"status": "failed", "run_id": "r", "error": "Step 'Send' failed: 401",
                "step_history": [
                    {"step_id": "a", "step_name": "Draft", "step_type": "agent", "status": "completed",
                     "output_key": "draft", "duration_seconds": 4.2, "tasks": 3},
                    {"step_id": "b", "step_name": "Send", "step_type": "tool", "status": "failed",
                     "output_key": "sent", "error": "401 Unauthorized"},
                    "not an entry"],
                "shared_state": {"draft": "z" * 2000, "sent": "partial"}}
        draft, send = runs._for_model(view)["steps"]
        assert draft["output"].startswith("z" * 500) and draft["output"].endswith("[1500 more characters]")
        assert (draft["duration_seconds"], draft["tasks"]) == (4.2, 3)
        assert send["error"] == "401 Unauthorized" and "output" not in send
