"""The builder's own tools — the ones the AI builder calls to build things.

These went untested for a long time and it cost: two of them returned an error
string on every call (an un-awaited loader), every write rewrote the whole
tenant, and `create_agent` minted ids from a millisecond clock. The tests below
pin the behaviour those bugs violated, so the builder that now runs
workspace-wide cannot quietly lose a row again.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool


class _FakeServerModule:
    """`server_module` as the dispatcher uses it: a bag of MCP sessions."""

    def __init__(self, sessions: dict | None = None):
        self.agent_sessions = sessions or {}


@pytest.fixture
def server():
    return _FakeServerModule()


async def call(tool: str, args: dict, server) -> dict:
    """Invoke a builder tool the way the engine does, and decode its result."""
    return json.loads(await execute_builder_tool(tool, args, server))


# ── single-row writes ────────────────────────────────────────────────────────

class TestWritesTouchOneRow:
    """Every mutator used to read the list, mutate it, and write it all back.

    That loses a concurrent edit, and on cloud it also rewrote every row on
    every tool call and resurrected soft-deleted orchestrations.
    """

    async def test_creating_an_agent_leaves_its_siblings_alone(self, server):
        keeper = await S.seed_agents([S.make_agent(id="agent_keeper", name="Keeper")])
        await call("create_agent", {
            "name": "New", "description": "d", "type": "conversational",
            "tools": [], "system_prompt": "p",
        }, server)

        from core.routes.agents import load_user_agents
        ids = {a["id"] for a in await load_user_agents()}
        assert "agent_keeper" in ids
        assert len(ids) == 2

    async def test_updating_one_agent_does_not_rewrite_another(self, server):
        await S.seed_agents([
            S.make_agent(id="agent_a", name="A"),
            S.make_agent(id="agent_b", name="B"),
        ])
        result = await call("update_agent", {"agent_id": "agent_a", "fields": {"name": "A2"}}, server)
        assert result["status"] == "updated"

        from core.routes.agents import load_user_agents
        by_id = {a["id"]: a for a in await load_user_agents()}
        assert by_id["agent_a"]["name"] == "A2"
        assert by_id["agent_b"]["name"] == "B"

    async def test_adding_a_step_does_not_disturb_another_orchestration(self, server):
        await S.seed_orchestrations([
            S.make_orchestration(id="orch_target", name="Target"),
            S.make_orchestration(id="orch_bystander", name="Bystander"),
        ])
        result = await call("add_single_step", {
            "orch_id": "orch_target", "step_id": "step_new", "name": "New", "type": "end",
        }, server)
        assert result["status"] == "step_added"

        from core.routes.orchestrations import load_orchestrations
        by_id = {o["id"]: o for o in await load_orchestrations()}
        assert len(by_id["orch_target"]["steps"]) == 2
        assert len(by_id["orch_bystander"]["steps"]) == 1


class TestIdsAreUnique:
    async def test_agents_created_in_a_tight_loop_all_survive(self, server):
        """The old id was `agent_{ms}`: two calls in one millisecond collided,
        and because the write was a full replace, the second silently ate the
        first."""
        for i in range(25):
            result = await call("create_agent", {
                "name": f"Agent {i}", "description": "d", "type": "conversational",
                "tools": [], "system_prompt": "p",
            }, server)
            assert result["status"] == "created"

        from core.routes.agents import load_user_agents
        assert len({a["id"] for a in await load_user_agents()}) == 25

    async def test_a_new_orchestration_never_reuses_a_live_id(self, server, monkeypatch):
        await S.seed_orchestrations([S.make_orchestration(id="orch_taken")])
        # Force the first draw to collide, so the retry loop is what saves us.
        draws = iter(["orch_taken", "orch_fresh"])
        monkeypatch.setattr("core.builder_tools._random_id", lambda *a, **k: next(draws))

        result = await call("create_orchestration", {
            "name": "New", "entry_step_id": "step_1",
            "steps_json": json.dumps([{"id": "step_1", "name": "End", "type": "end"}]),
        }, server)
        assert result["orchestration"]["id"] == "orch_fresh"

        from core.routes.orchestrations import load_orchestrations
        assert len(await load_orchestrations()) == 2


# ── the builder's own machinery is not workspace content ─────────────────────

class TestSystemRowsAreHidden:
    """The builder is itself a seeded orchestration with sub-agents. It must
    not offer its own parts to the model as things to edit or delete."""

    async def test_list_agents_omits_system_agents(self, server):
        await S.seed_agents([
            S.make_agent(id="agent_mine", name="Mine"),
            S.make_agent(id="agent_native_builder_analyst", name="Analyst", system="native_builder"),
        ])
        listed = await call("list_agents", {}, server)
        assert [a["id"] for a in listed] == ["agent_mine"]

    async def test_list_orchestrations_omits_system_and_retired_rows(self, server):
        await S.seed_orchestrations([
            S.make_orchestration(id="orch_mine", name="Mine"),
            S.make_orchestration(id="orch_native_builder", system="native_builder"),
            S.make_orchestration(id="orch_deleted", is_active=False),
        ])
        listed = await call("list_orchestrations", {}, server)
        assert [o["id"] for o in listed] == ["orch_mine"]

    async def test_a_system_agent_cannot_be_fetched_or_edited(self, server):
        await S.seed_agents([S.make_agent(id="agent_native_builder", system="native_builder")])
        assert "error" in await call("get_agent", {"agent_id": "agent_native_builder"}, server)
        edit = await call("update_agent", {"agent_id": "agent_native_builder", "fields": {"name": "x"}}, server)
        assert "error" in edit

    async def test_a_system_orchestration_cannot_be_edited(self, server):
        await S.seed_orchestrations([S.make_orchestration(id="orch_native_builder", system="native_builder")])
        result = await call("add_single_step", {
            "orch_id": "orch_native_builder", "step_id": "step_x", "name": "X", "type": "end",
        }, server)
        assert "error" in result


# ── the plan gate ────────────────────────────────────────────────────────────

class TestTransformNeedsACodeRuntime:
    """Cloud's cheaper plans have no sandbox. Refusing at authoring time beats
    saving an orchestration that fails halfway through its first run."""

    @pytest.fixture
    def no_code_execution(self, monkeypatch):
        from core import config
        monkeypatch.setattr(
            config, "load_settings",
            lambda: {**config._DEFAULTS, "code_execution_enabled": False},
        )

    async def test_a_transform_step_is_refused_when_the_plan_has_no_runtime(
        self, server, no_code_execution
    ):
        await S.seed_orchestrations([S.make_orchestration(id="orch_t")])
        result = await call("add_single_step", {
            "orch_id": "orch_t", "step_id": "step_t", "name": "Reshape",
            "type": "transform", "transform_code": "result = state.x",
        }, server)
        assert "error" in result
        assert "step_t" in result["error"]

        from core.routes.orchestrations import load_orchestrations
        assert len((await load_orchestrations())[0]["steps"]) == 1

    async def test_other_step_types_still_save(self, server, no_code_execution):
        await S.seed_orchestrations([S.make_orchestration(id="orch_t")])
        result = await call("add_single_step", {
            "orch_id": "orch_t", "step_id": "step_ok", "name": "Say", "type": "print",
            "print_content": "hi",
        }, server)
        assert result["status"] == "step_added"

    async def test_open_source_installs_run_their_own_sandbox(self, server):
        """No setting, no gate: `code_execution_enabled` is a cloud injection."""
        await S.seed_orchestrations([S.make_orchestration(id="orch_t")])
        result = await call("add_single_step", {
            "orch_id": "orch_t", "step_id": "step_t", "name": "Reshape",
            "type": "transform", "transform_code": "result = 1",
        }, server)
        assert result["status"] == "step_added"


# ── tool discovery ───────────────────────────────────────────────────────────

class TestToolDiscoveryWorksAtAll:
    """Both of these called an async loader without awaiting it and swallowed
    the TypeError, so they returned `{"error": ...}` on every single call —
    while four of the five seeded builder agents declared `get_tools_detail`."""

    async def test_get_tools_detail_returns_schemas(self, server):
        await S.seed_agents([S.make_agent(id="agent_mine", tools=["all"])])
        result = await call("get_tools_detail", {"tool_names": ["list_agents"]}, server)
        assert "error" not in result
        assert result["list_agents"]["name"] == "list_agents"
        assert result["list_agents"]["description"]

    async def test_get_tools_detail_names_what_it_could_not_find(self, server):
        await S.seed_agents([S.make_agent(id="agent_mine", tools=["all"])])
        result = await call("get_tools_detail", {"tool_names": ["no_such_tool"]}, server)
        assert result["_not_found"] == ["no_such_tool"]

    async def test_unscoped_list_all_tools_returns_tools(self, server):
        await S.seed_agents([S.make_agent(id="agent_mine", tools=["all"])])
        result = await call("list_all_tools", {"limit": 200}, server)
        assert isinstance(result, list) and result
        assert "list_agents" in {t["name"] for t in result}

    async def test_discovery_ignores_the_builders_own_agents(self, server):
        """The active agent it borrows tool access from must be the customer's,
        not one of the builder's own sub-agents."""
        await S.seed_agents([
            S.make_agent(id="agent_native_builder", tools=[], system="native_builder"),
            S.make_agent(id="agent_mine", tools=["all"]),
        ])
        result = await call("list_all_tools", {"limit": 200}, server)
        assert isinstance(result, list) and result


# ── the wider tool set (D2) ──────────────────────────────────────────────────

class TestCustomTools:
    async def test_creating_an_http_tool_stores_it(self, server):
        result = await call("create_http_tool", {
            "name": "create_ticket",
            "description": "Open a support ticket",
            "url": "https://api.example.com/tickets/{{ project }}",
            "method": "POST",
            "headers_json": json.dumps({"Authorization": "Bearer {{ global.TOKEN }}"}),
            "input_schema_json": json.dumps({
                "type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"],
            }),
        }, server)
        assert result == {"status": "created", "id": "create_ticket", "name": "create_ticket"}

        from core.routes.tools import load_custom_tools
        tool = (await load_custom_tools())[0]
        assert tool["method"] == "POST"
        assert tool["headers"]["Authorization"] == "Bearer {{ global.TOKEN }}"
        assert tool["inputSchema"]["required"] == ["project"]

    async def test_a_body_template_is_the_body_the_tool_sends(self, server):
        """F2: the builder stored `body_template` and the executor reads `body`, so
        the template was dropped and the bare arguments went out instead."""
        await call("create_http_tool", {
            "name": "open_ticket",
            "description": "Open a support ticket",
            "url": "https://api.example.com/tickets",
            "method": "POST",
            "body_template": '{"ticket": {"summary": "{{ title }}"}}',
        }, server)

        from core import http_tool
        from core.routes.tools import load_custom_tools
        tool = (await load_custom_tools())[0]
        request = http_tool.build_request(tool, {"title": "Printer on fire"}, {})
        assert request["json"] == {"ticket": {"summary": "Printer on fire"}}

    def test_a_tool_saved_with_the_old_key_still_sends_its_template(self):
        """Builder-made tools saved before F2's fix hold `body_template`."""
        from core import http_tool
        tool = {"method": "POST", "url": "https://api.example.com/t", "body_template": '{"x": "{{ a }}"}'}
        assert http_tool.build_request(tool, {"a": "1"}, {})["json"] == {"x": "1"}

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",  # the cloud metadata endpoint
        "http://127.0.0.1:8000/api/settings",        # our own API
        "http://10.0.0.5/internal",                  # private space
        "file:///etc/passwd",                        # not even HTTP
    ])
    async def test_a_url_pointing_inward_is_refused(self, server, url):
        """The shape a prompt-injected builder writes when it goes looking for
        credentials. This is the only check these URLs get — the HTTP executor
        does not guard egress at run time."""
        result = await call("create_http_tool", {"name": "peek", "description": "d", "url": url}, server)
        assert "error" in result

        from core.routes.tools import load_custom_tools
        assert await load_custom_tools() == []

    async def test_a_host_that_does_not_resolve_here_is_still_allowed(self, server):
        """An internal API, or one not deployed yet. Refusing would be a guess
        about someone else's network, and the tools screen would allow it."""
        result = await call("create_http_tool", {
            "name": "internal_api", "description": "d",
            "url": "https://tickets.internal.example.invalid/v1/create",
        }, server)
        assert result["status"] == "created"

    async def test_a_tool_name_must_be_callable(self, server):
        result = await call("create_http_tool", {
            "name": "not a tool name!", "description": "d", "url": "https://api.example.com/x",
        }, server)
        assert "error" in result

    async def test_importing_a_spec_creates_one_tool_per_operation(self, server):
        spec = json.dumps({
            "openapi": "3.0.0",
            "info": {"title": "Widgets", "description": "The widget API"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/widgets": {"get": {"operationId": "listWidgets", "summary": "List widgets"}},
                "/widgets/{id}": {
                    "get": {
                        "operationId": "getWidget", "summary": "Get one",
                        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    }
                },
            },
        })
        result = await call("import_openapi", {"spec_text": spec}, server)
        assert result["count"] == 2
        assert set(result["tool_names"]) == {"listwidgets", "getwidget"}

        from core.routes.tools import load_custom_tools
        tools = {t["name"]: t for t in await load_custom_tools()}
        # One group per import, and the path parameter arrives as {{ }}.
        assert tools["getwidget"]["group"] == result["id"]
        assert tools["getwidget"]["url"].endswith("{{id}}")

    async def test_a_spec_that_is_not_a_spec_is_reported_not_raised(self, server):
        result = await call("import_openapi", {"spec_text": "not a spec"}, server)
        assert "error" in result


class TestSkills:
    async def test_create_then_read_back(self, server):
        created = await call("create_skill", {
            "name": "Quarterly Report Format",
            "description": "How the quarterly report is laid out",
            "body": "# Format\n\nOne section per region.",
        }, server)
        assert created == {"status": "created", "id": "quarterly-report-format", "name": "quarterly-report-format"}

        fetched = await call("get_skill", {"name": "quarterly-report-format"}, server)
        assert fetched["body"].startswith("# Format")

    async def test_update_keeps_the_body_when_only_the_description_changes(self, server):
        await call("create_skill", {"name": "tone", "description": "old", "body": "Be brief."}, server)
        result = await call("update_skill", {"name": "tone", "description": "new"}, server)
        assert result["status"] == "updated"

        fetched = await call("get_skill", {"name": "tone"}, server)
        assert fetched["description"] == "new"
        assert fetched["body"] == "Be brief."

    async def test_updating_a_skill_that_does_not_exist_says_so(self, server):
        assert "error" in await call("update_skill", {"name": "ghost", "description": "d"}, server)

    async def test_attaching_skills_to_an_agent(self, server):
        await S.seed_agents([S.make_agent(id="agent_writer")])
        await call("create_skill", {"name": "tone", "description": "d", "body": "b"}, server)

        result = await call("attach_skills", {"agent_id": "agent_writer", "names": ["tone"]}, server)
        assert result["skills"] == ["tone"]

        from core.store.resources import get_agent
        assert (await get_agent("agent_writer"))["skills"] == ["tone"]

    async def test_attaching_a_skill_that_does_not_exist_is_refused(self, server):
        await S.seed_agents([S.make_agent(id="agent_writer")])
        result = await call("attach_skills", {"agent_id": "agent_writer", "names": ["nope"]}, server)
        assert "error" in result

        from core.store.resources import get_agent
        assert "skills" not in (await get_agent("agent_writer"))


class TestSchedules:
    async def test_creating_a_schedule_computes_its_next_run(self, server):
        await S.seed_orchestrations([S.make_orchestration(id="orch_brief", name="Market brief")])
        result = await call("create_schedule", {
            "name": "Weekday brief", "target_type": "orchestration", "target_id": "orch_brief",
            "prompt": "Run the brief", "schedule_type": "interval",
            "interval_value": 1, "interval_unit": "days",
        }, server)
        assert result["status"] == "created"
        assert result["next_run_at"]

        from core.store import schedules as schedule_store
        assert len(await schedule_store.load()) == 1

    async def test_a_schedule_cannot_point_at_nothing(self, server):
        """A schedule whose target does not exist fires into the void forever."""
        result = await call("create_schedule", {
            "name": "Ghost", "target_type": "orchestration", "target_id": "orch_missing",
            "prompt": "go", "schedule_type": "interval", "interval_value": 1, "interval_unit": "hours",
        }, server)
        assert "error" in result

        from core.store import schedules as schedule_store
        assert await schedule_store.load() == []

    async def test_a_schedule_cannot_target_the_builder_itself(self, server):
        await S.seed_orchestrations([S.make_orchestration(id="orch_native_builder", system="native_builder")])
        result = await call("create_schedule", {
            "name": "Loop", "target_type": "orchestration", "target_id": "orch_native_builder",
            "prompt": "go", "schedule_type": "interval", "interval_value": 1, "interval_unit": "hours",
        }, server)
        assert "error" in result

    async def test_pausing_and_listing(self, server):
        await S.seed_agents([S.make_agent(id="agent_x")])
        created = await call("create_schedule", {
            "name": "Hourly", "target_type": "agent", "target_id": "agent_x", "prompt": "go",
            "schedule_type": "interval", "interval_value": 1, "interval_unit": "hours",
        }, server)
        await call("set_schedule_enabled", {"schedule_id": created["id"], "enabled": False}, server)

        listed = await call("list_schedules", {}, server)
        assert listed[0]["enabled"] is False


class TestVariablesArePreviewedNotDisclosed:
    async def test_values_are_truncated_and_the_reference_is_given(self, server):
        from core import variables
        await variables.save({"API_TOKEN": "sk-" + "x" * 200})

        listed = await call("list_variables", {}, server)
        assert listed == [{
            "key": "API_TOKEN",
            "preview": ("sk-" + "x" * 200)[:40],
            "reference": "{{ global.API_TOKEN }}",
        }]
        assert len(listed[0]["preview"]) == 40


class TestDeletesRefuseWithoutConfirmation:
    """Two layers guard a delete: `confirm=true`, and the fact that these tools
    are declared only on the operator agent, which runs after a human approves
    a plan naming the deletion."""

    @pytest.mark.parametrize("tool,args", [
        ("delete_agent", {"agent_id": "agent_doomed"}),
        ("delete_orchestration", {"orch_id": "orch_doomed"}),
        ("delete_custom_tool", {"name": "tool_doomed"}),
        ("delete_schedule", {"schedule_id": "sched_doomed"}),
    ])
    async def test_refused_without_confirm(self, server, tool, args):
        result = await call(tool, args, server)
        assert "error" in result
        assert "confirm" in result["error"]

    @pytest.mark.parametrize("tool,args", [
        ("delete_agent", {"agent_id": "agent_doomed", "confirm": False}),
        ("delete_orchestration", {"orch_id": "orch_doomed", "confirm": "yes"}),
    ])
    async def test_confirm_must_be_true_not_merely_present(self, server, tool, args):
        assert "error" in await call(tool, args, server)

    async def test_deleting_an_agent_names_the_steps_it_orphans(self, server):
        await S.seed_agents([S.make_agent(id="agent_doomed", name="Doomed")])
        await S.seed_orchestrations([S.make_orchestration(id="orch_uses_it", steps=[
            {"id": "step_1", "name": "Work", "type": "agent", "agent_id": "agent_doomed"},
        ])])
        result = await call("delete_agent", {"agent_id": "agent_doomed", "confirm": True}, server)
        assert result["status"] == "deleted"
        assert result["orphaned_steps"] == [{"orchestration_id": "orch_uses_it", "step_id": "step_1"}]

        from core.store.resources import get_agent
        assert await get_agent("agent_doomed") is None

    async def test_the_builders_own_parts_cannot_be_deleted(self, server):
        await S.seed_agents([S.make_agent(id="agent_native_builder", system="native_builder")])
        await S.seed_orchestrations([S.make_orchestration(id="orch_native_builder", system="native_builder")])

        assert "error" in await call("delete_agent", {"agent_id": "agent_native_builder", "confirm": True}, server)
        assert "error" in await call("delete_orchestration", {"orch_id": "orch_native_builder", "confirm": True}, server)

        from core.store.resources import get_agent, get_orchestration
        assert await get_agent("agent_native_builder") is not None
        assert await get_orchestration("orch_native_builder") is not None

    async def test_deleting_an_orchestration_that_is_not_there(self, server):
        assert "error" in await call("delete_orchestration", {"orch_id": "orch_ghost", "confirm": True}, server)


class TestRunInspection:
    async def test_recent_runs_exclude_the_builders_own(self, server):
        """The builder's conversations are runs too; they are not the
        workspace's run history."""
        await S.seed_orchestrations([
            S.make_orchestration(id="orch_mine"),
            S.make_orchestration(id="orch_native_builder", system="native_builder"),
        ])
        listed = await call("list_recent_runs", {}, server)
        assert isinstance(listed, list)
        assert all(r["orchestration_id"] != "orch_native_builder" for r in listed)

    async def test_a_run_with_no_checkpoint_says_so(self, server):
        assert "error" in await call("get_run_summary", {"run_id": "run_missing"}, server)


# ── what the panel reads instead of guessing (D3) ────────────────────────────

class TestResourceSavedEvents:
    """The panel used to recover "an orchestration was saved" with a regex over
    a 500-character truncation of the tool result. It failed on anything big
    enough to be worth building, and three fallbacks behind it were dead code.
    The dispatcher now says what it changed."""

    async def test_creating_an_agent_announces_it_in_full(self, server):
        from core.builder_tools import execute_builder_tool_with_events

        _output, events = await execute_builder_tool_with_events("create_agent", {
            "name": "Researcher", "description": "d", "type": "conversational",
            "tools": [], "system_prompt": "p",
        }, server)

        assert len(events) == 1
        event = events[0]
        assert event["type"] == "resource_saved"
        assert (event["kind"], event["action"], event["name"]) == ("agent", "created", "Researcher")
        assert event["id"].startswith("agent_")
        # The whole resource, not a truncation of it.
        assert event["resource"]["system_prompt"] == "p"

    async def test_the_result_string_stays_lean_while_the_event_carries_the_resource(self, server):
        """A big orchestration is exactly the case the old regex could not
        survive: the model's copy is truncated, the event's is not."""
        from core.builder_tools import execute_builder_tool_with_events

        steps = [
            {"id": f"step_{i}", "name": f"Step {i}" * 20, "type": "print",
             "print_content": "x" * 200, "next_step_id": f"step_{i + 1}"}
            for i in range(12)
        ] + [{"id": "step_12", "name": "End", "type": "end"}]

        output, events = await execute_builder_tool_with_events("create_orchestration", {
            "name": "Big", "entry_step_id": "step_0", "steps_json": json.dumps(steps),
        }, server)

        assert len(output) > 500  # the caller truncates this to 500 for the model
        assert len(events) == 1
        assert events[0]["kind"] == "orchestration"
        assert len(events[0]["resource"]["steps"]) == 13

    async def test_creating_several_agents_announces_each(self, server):
        from core.builder_tools import execute_builder_tool_with_events

        _output, events = await execute_builder_tool_with_events("create_agents", {
            "agents": [
                {"name": "One", "description": "d", "type": "conversational", "tools": [], "system_prompt": "p"},
                {"name": "Two", "description": "d", "type": "conversational", "tools": [], "system_prompt": "p"},
            ],
        }, server)
        assert [e["name"] for e in events] == ["One", "Two"]
        assert {e["action"] for e in events} == {"created"}

    async def test_an_openapi_import_announces_once_not_per_operation(self, server):
        """A sixty-endpoint spec must not put sixty banners in the panel."""
        from core.builder_tools import execute_builder_tool_with_events

        spec = json.dumps({
            "openapi": "3.0.0", "info": {"title": "Widgets"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {f"/w{i}": {"get": {"operationId": f"op{i}"}} for i in range(8)},
        })
        _output, events = await execute_builder_tool_with_events("import_openapi", {"spec_text": spec}, server)

        assert len(events) == 1
        assert events[0]["resource"]["count"] == 8

    async def test_a_delete_is_announced_too(self, server):
        from core.builder_tools import execute_builder_tool_with_events

        await S.seed_orchestrations([S.make_orchestration(id="orch_doomed", name="Doomed")])
        _output, events = await execute_builder_tool_with_events(
            "delete_orchestration", {"orch_id": "orch_doomed", "confirm": True}, server
        )
        assert (events[0]["kind"], events[0]["action"], events[0]["id"]) == (
            "orchestration", "deleted", "orch_doomed",
        )

    async def test_a_read_changes_nothing_and_says_nothing(self, server):
        from core.builder_tools import execute_builder_tool_with_events

        _output, events = await execute_builder_tool_with_events("list_agents", {}, server)
        assert events == []

    async def test_a_refused_write_announces_nothing(self, server):
        from core.builder_tools import execute_builder_tool_with_events

        await S.seed_orchestrations([S.make_orchestration(id="orch_doomed")])
        _output, events = await execute_builder_tool_with_events(
            "delete_orchestration", {"orch_id": "orch_doomed"}, server  # no confirm
        )
        assert events == []


class TestBothDispatchSitesYieldIt:
    """A builder tool reaches the engine two ways — an agent calling it mid-ReAct,
    and a tool step forcing it. Both have to pass the event on, or the panel
    goes quiet on whichever path it happens to take."""

    async def test_an_agent_calling_a_builder_tool_yields_it(self, fake_llm):
        import core.server as server
        from core.react_engine import run_agent_step

        fake_llm.set_default(json.dumps({
            "tool": "create_agent",
            "arguments": {"name": "R", "description": "d", "type": "conversational",
                          "tools": [], "system_prompt": "p"},
        }))
        agent = S.make_agent(id="agent_builder_ish", tools=["create_agent"], skip_default_tools=True)
        await S.seed_agents([agent])

        events = []
        async for event in run_agent_step(
            message="make an agent", agent_id="agent_builder_ish",
            session_id="s1", server_module=server, max_turns=2,
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert "resource_saved" in types, types
        assert types.index("tool_result") < types.index("resource_saved")

    async def test_a_tool_step_forcing_a_builder_tool_yields_it(self, fake_llm):
        import core.server as server
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        fake_llm.set_default(json.dumps({
            "tool": "create_agent",
            "arguments": {"name": "R", "description": "d", "type": "conversational",
                          "tools": [], "system_prompt": "p"},
        }))
        orch = Orchestration.model_validate(S.make_orchestration(
            entry_step_id="step_write",
            steps=[
                {"id": "step_write", "name": "Write", "type": "tool",
                 "forced_tool": "create_agent", "prompt_template": "go",
                 "output_key": "out", "next_step_id": "step_end"},
                {"id": "step_end", "name": "End", "type": "end"},
            ],
        ))
        events = [e async for e in OrchestrationEngine(orch, server).run("go", run_id="run_ts")]

        saved = [e for e in events if e["type"] == "resource_saved"]
        assert saved, [e["type"] for e in events]
        # A step's events carry where they happened, like every other step event.
        assert saved[0]["step_name"] == "Write"
        assert saved[0]["orch_step_id"] == "step_write"


class TestAnUnsafeExpressionIsRefusedAtAuthoringTime:
    """`if_condition` is a parameter this model writes directly.

    That is the whole of S3's live path: a chat message, a prompt injection, an
    expression, and code running in-process. `core.expressions` closes it at run
    time; refusing at the write is what turns an injected expression into a
    corrected one rather than a stored one the owner never sees.
    """

    async def test_a_dunder_walk_is_refused_and_nothing_is_saved(self, server):
        result = await call("create_orchestration", {
            "name": "Sneaky", "description": "d", "entry_step_id": "s1",
            "steps_json": json.dumps([
                {"id": "s1", "name": "Branch", "type": "if_else",
                 "if_condition": "().__class__.__bases__[0].__subclasses__()",
                 "if_true_step_id": None, "if_false_step_id": None},
            ]),
        }, server)
        assert "error" in result
        assert "will not run" in result["error"]
        assert "if_condition" in result["error"]

        from core.routes.orchestrations import load_orchestrations
        assert not [o for o in await load_orchestrations() if o.get("name") == "Sneaky"]

    async def test_every_expression_field_is_checked(self, server):
        for field, value in (
            ("if_condition", "state.x.__class__"),
            ("switch_expression", "f'{state.x}'"),
            ("loop_over", "[x for x in state.items]"),
            ("loop_while_condition", "(lambda: 1)()"),
            ("loop_break_condition", "__import__('os')"),
        ):
            step = {"id": "s1", "name": "S", "type": "if_else", field: value}
            result = await call("create_orchestration", {
                "name": f"Bad {field}", "description": "d", "entry_step_id": "s1",
                "steps_json": json.dumps([step]),
            }, server)
            assert "error" in result and field in result["error"], field

    async def test_an_ordinary_condition_still_saves(self, server):
        result = await call("create_orchestration", {
            "name": "Fine", "description": "d", "entry_step_id": "s1",
            "steps_json": json.dumps([
                {"id": "s1", "name": "Branch", "type": "if_else",
                 "if_condition": "str(state.verdict).strip().lower() == 'approve'",
                 "if_true_step_id": None, "if_false_step_id": None},
            ]),
        }, server)
        assert "error" not in result, result

    async def test_a_typo_is_reported_by_the_validator_not_the_gate(self, server):
        # The gate is deliberately narrow: the engine survives a broken
        # expression with a warning, so failing the write on one would be the
        # worse trade. The validator is where an author hears about it.
        created = await call("create_orchestration", {
            "name": "Typo", "description": "d", "entry_step_id": "s1",
            "steps_json": json.dumps([
                {"id": "s1", "name": "Branch", "type": "if_else",
                 "if_condition": "state.a.b.c ===",
                 "if_true_step_id": None, "if_false_step_id": None},
            ]),
        }, server)
        assert "error" not in created, created

        orch_id = created["orchestration"]["id"]
        report = await call("validate_orchestration", {"orch_id": orch_id}, server)
        assert any("Expression problem" in i and "if_condition" in i
                   for i in report.get("issues", [])), report


class TestStateSchemaValidation:
    """`state_schema` was the one part of an orchestration nothing ever looked at.

    A key could carry no type, or a type nobody recognised, and it saved, loaded
    and ran — the only symptom being a blank dropdown in the editor and, at run
    time, an engine that had nothing to coerce against.
    """

    ALLOWED = "Use one of: str, int, float, bool, list, dict."

    @staticmethod
    def _issues(state_schema):
        from core.builder_tools import _validate_orchestration
        return _validate_orchestration({
            "id": "o", "entry_step_id": "s1", "state_schema": state_schema,
            "steps": [
                {"id": "s1", "name": "Say", "type": "print", "print_content": "x",
                 "output_key": "note", "next_step_id": "end1"},
                {"id": "end1", "name": "Done", "type": "end"},
            ],
        })["issues"]

    def test_a_canonical_schema_raises_nothing(self):
        assert self._issues({
            "note": {"type": "str", "default": "", "description": "A note"},
            "rows": {"type": "list", "default": [], "description": "Rows"},
            "n": {"type": "int", "default": 0, "description": "A count"},
        }) == []

    def test_the_editor_spellings_still_pass(self):
        # Orchestrations saved before the vocabularies were reconciled hold
        # these. They are aliases, not errors — nothing needs migrating.
        assert self._issues({
            "a": {"type": "string", "default": ""},
            "b": {"type": "number", "default": 0},
            "c": {"type": "boolean", "default": False},
        }) == []

    def test_a_key_with_no_type_is_an_issue(self):
        # What the AI builder actually emitted for `loop_item` / `loop_index`.
        issues = self._issues({"loop_item": {"description": "The current query."}})
        assert len(issues) == 1
        assert "loop_item" in issues[0]
        assert self.ALLOWED in issues[0]

    def test_a_type_nobody_recognises_is_an_issue(self):
        issues = self._issues({"n": {"type": "int64", "default": 0}})
        assert len(issues) == 1
        assert "int64" in issues[0] and "n" in issues[0]
        assert self.ALLOWED in issues[0]

    def test_an_entry_that_is_not_an_object_is_an_issue(self):
        issues = self._issues({"rows": "list"})
        assert len(issues) == 1
        assert "rows" in issues[0]

    def test_a_default_that_contradicts_its_type_is_an_issue(self):
        issues = self._issues({"rows": {"type": "list", "default": "[]"}})
        assert len(issues) == 1
        assert "rows" in issues[0] and "list" in issues[0]

    def test_a_missing_default_is_fine(self):
        # `default` is optional — `_init_state` simply skips a key without one.
        assert self._issues({"rows": {"type": "list", "description": "Rows"}}) == []

    def test_every_message_carries_the_fix(self):
        # Both savers cap themselves at two or three repair attempts, so a
        # message that does not name the allowed set burns the budget and the
        # saver reports DONE anyway.
        issues = self._issues({
            "a": {"description": "no type"},
            "b": {"type": "frobnicate"},
        })
        assert len(issues) == 2
        assert all(self.ALLOWED in issue for issue in issues)


class TestStateSchemaCanonicalisation:
    """Every save runs `Orchestration.model_validate`, so legacy rows self-heal."""

    @staticmethod
    def _schema(raw):
        from core.models_orchestration import Orchestration
        return Orchestration(id="o", name="O", state_schema=raw).state_schema

    def test_an_editor_spelling_is_stored_canonically(self):
        assert self._schema({"a": {"type": "string", "default": ""}})["a"]["type"] == "str"
        assert self._schema({"a": {"type": "boolean", "default": False}})["a"]["type"] == "bool"
        assert self._schema({"a": {"type": "number", "default": 0}})["a"]["type"] == "float"

    def test_an_unreadable_type_is_left_exactly_as_it_was(self):
        # So `_validate_orchestration` can still name it, and so the editor can
        # show the person what is actually stored rather than a guess.
        assert self._schema({"a": {"type": "frobnicate"}})["a"]["type"] == "frobnicate"

    def test_the_rest_of_the_entry_is_untouched(self):
        entry = self._schema({"a": {"type": "array", "default": [1], "description": "d"}})["a"]
        assert entry == {"type": "list", "default": [1], "description": "d"}

    def test_a_non_object_entry_survives_validation(self):
        # Malformed, and `_validate_orchestration` says so — but loading an
        # orchestration must not be what fails.
        assert self._schema({"a": "list"})["a"] == "list"


class TestParallelConvergence:
    """A parallel step with no `next_step_id` runs the rest of the graph twice.

    The engine builds a single-entry branch by following `next_step_id` until it
    reaches the parallel's own `next_step_id` — the join — or a step with no
    next (`ParallelStepExecutor.resolve_branch_chain`). With no join the chain
    never stops: Naveen's Test Harness ran Summarizer → Compiler → Review →
    Finalizer as *branch one*, so the second branch's Entity Extractor never ran
    and the End step inside the branch failed the run. The canvas cannot show
    this — a branch's chain is drawn as ordinary next edges — so the builder
    has to get it right when it writes the orchestration.
    """

    JOIN = "needs next_step_id set to the merge step"

    @staticmethod
    def _issues(par_next, merge_next="rev1", extra=None, strip_merge_inputs=False):
        from core.builder_tools import _validate_orchestration
        steps = [
            {"id": "par1", "name": "Analyse", "type": "parallel",
             "parallel_branches": [["sum1"], ["ent1"]], "next_step_id": par_next},
            {"id": "sum1", "name": "Summarise", "type": "llm", "output_key": "summary",
             "next_step_id": "mrg1"},
            {"id": "ent1", "name": "Entities", "type": "llm", "output_key": "entities",
             "next_step_id": "mrg1"},
            {"id": "mrg1", "name": "Compile", "type": "merge", "merge_strategy": "dict",
             "output_key": "report", "next_step_id": merge_next,
             **({} if strip_merge_inputs else {"input_keys": ["summary", "entities"]})},
            {"id": "rev1", "name": "Print", "type": "print", "print_content": "x",
             "next_step_id": "end1"},
            {"id": "end1", "name": "Done", "type": "end"},
        ]
        return _validate_orchestration({
            "id": "o", "entry_step_id": "par1", "steps": steps + (extra or []),
        })["issues"]

    def test_a_parallel_joined_at_a_merge_step_raises_nothing(self):
        assert self._issues("mrg1") == []

    def test_a_parallel_with_no_join_is_an_issue(self):
        issues = [i for i in self._issues("") if "par1" in i]
        assert len(issues) == 1, issues
        assert self.JOIN in issues[0]

    def test_a_parallel_joined_at_something_other_than_a_merge_is_an_issue(self):
        # Reaching a step that is not a merge means the branches' outputs are
        # never combined — and the builder has a merge step for exactly this.
        issues = [i for i in self._issues("rev1") if "par1" in i]
        assert len(issues) == 1, issues
        assert "rev1" in issues[0] and self.JOIN in issues[0]

    def test_a_merge_with_no_input_keys_is_an_issue(self):
        # It combines the keys it is given and nothing else: with none it writes
        # an empty result over its output_key and reports input_count 0. Naveen
        # found his own merge step like this, and nothing said so (2026-09-20).
        issues = [i for i in self._issues("mrg", extra=None, strip_merge_inputs=True) if "mrg1" in i]
        assert len(issues) == 1, issues
        assert "input_keys" in issues[0]

    def test_a_result_step_with_no_output_key_is_an_issue(self):
        # A merge, an agent, an llm — their whole purpose is the value they
        # write. Naveen's Report Compiler had none, and nothing said so; the
        # panel's placeholder even reads like a value (2026-09-20).
        from core.builder_tools import _validate_orchestration
        doc = {"id": "o", "entry_step_id": "l1", "steps": [
            {"id": "l1", "name": "Draft", "type": "llm", "prompt_template": "x", "next_step_id": "e1"},
            {"id": "e1", "name": "Done", "type": "end"},
        ]}
        issues = [i for i in _validate_orchestration(doc)["issues"] if "output_key" in i]
        assert len(issues) == 1, issues
        assert "l1" in issues[0] and "discarded" in issues[0]

    def test_a_routing_or_print_step_needs_no_output_key(self):
        # Their key records a decision they have already acted on, or text the
        # run log carries anyway — warning about those is noise.
        from core.builder_tools import _validate_orchestration
        doc = {"id": "o", "entry_step_id": "s1", "steps": [
            {"id": "s1", "name": "Show", "type": "print", "print_content": "hi", "next_step_id": "s2"},
            {"id": "s2", "name": "Gate", "type": "if_else", "if_condition": "True",
             "if_true_step_id": "e1", "if_false_step_id": "e1"},
            {"id": "e1", "name": "Done", "type": "end"},
        ]}
        assert [i for i in _validate_orchestration(doc)["issues"] if "output_key" in i] == []

    def test_the_message_says_to_add_a_merge_rather_than_delete_anything(self):
        # The saver acts on these messages literally: told that a read had no
        # writer, it deleted the read (F123). This one must point at the fix.
        issue = [i for i in self._issues("") if "par1" in i][0]
        assert "add a merge step" in issue
        assert "delete" not in issue.lower() and "remove" not in issue.lower()


class TestAReadOfAKeyTheEngineWrites:
    """`output_key` is not the only way a key gets into shared state (F123).

    The writer check used to know about nothing else, so a loop body that read
    the loop's own item was told *"reads input_key 'loop_item' but no step
    writes it — either use an existing output_key or delete the read"*. The
    Create Saver acts on these literally: it deleted the read, and Naveen's
    Test Harness ran three iterations of the same question with no query.

    The keys, and only these: a **for-each** loop's `loop_item_key` and
    `loop_index_key` (`steps.py` writes them under `if mode == "foreach"` —
    a count or a while loop writes neither), and `human_response`, which
    `engine.py` writes after every human step whatever that step's own
    output_key is.
    """

    @staticmethod
    def _issues(loop_fields, body_reads, extra=None):
        from core.builder_tools import _validate_orchestration
        steps = [
            {"id": "loop1", "name": "Loop", "type": "loop",
             "loop_step_ids": ["body"], "next_step_id": "done", **loop_fields},
            {"id": "body", "name": "Body", "type": "llm", "prompt_template": "x",
             "output_key": "o1", "input_keys": body_reads, "next_step_id": None},
            {"id": "done", "name": "Done", "type": "end"},
        ]
        return _validate_orchestration({
            "id": "o", "entry_step_id": "loop1", "steps": steps + (extra or []),
        })["issues"]

    @staticmethod
    def _reads(issues, key):
        return [i for i in issues if f"input_key '{key}'" in i]

    def test_a_for_each_body_may_read_the_loop_item(self):
        issues = self._issues({"loop_over": "state.queries"}, ["loop_item", "loop_index"])
        assert self._reads(issues, "loop_item") == [], issues
        assert self._reads(issues, "loop_index") == [], issues

    def test_a_renamed_loop_item_key_counts_too(self):
        issues = self._issues(
            {"loop_over": "state.queries", "loop_item_key": "query",
             "loop_index_key": "nth"},
            ["query", "nth"],
        )
        assert self._reads(issues, "query") == [], issues
        assert self._reads(issues, "nth") == [], issues

    def test_a_count_loop_writes_neither_so_the_read_is_still_flagged(self):
        # `mode == "foreach"` is the gate in the executor, so it is the gate
        # here. Silencing this would hide a read that really does resolve to
        # the schema default for ever.
        issues = self._issues({"loop_count": 3}, ["loop_item"])
        assert self._reads(issues, "loop_item"), issues

    def test_a_while_loop_writes_neither_either(self):
        issues = self._issues({"loop_while_condition": "state.go"}, ["loop_item"])
        assert self._reads(issues, "loop_item"), issues

    def test_a_step_after_a_human_step_may_read_human_response(self):
        # The engine writes the answer to the step's output_key *and* always to
        # `human_response` as well, so evaluators that check it still work.
        issues = self._issues(
            {"loop_count": 3}, ["human_response"],
            extra=[{"id": "ask", "name": "Ask", "type": "human",
                    "human_prompt": "ok?", "output_key": "approval",
                    "next_step_id": "loop1"}],
        )
        assert self._reads(issues, "human_response") == [], issues

    def test_human_response_with_no_human_step_is_still_flagged(self):
        issues = self._issues({"loop_count": 3}, ["human_response"])
        assert self._reads(issues, "human_response"), issues

    def test_a_genuinely_unwritten_read_leads_with_the_repair(self):
        # Still an issue — but the saver reads the first repair it is offered,
        # and "delete the read" was the wrong one to offer first.
        issue = self._reads(self._issues({"loop_count": 3}, ["nowhere"]), "nowhere")[0]
        assert "output_key" in issue
        head = issue.split("—", 1)[1].split(".")[0].lower()
        assert "delete" not in head and "remove" not in head, issue


class TestAContainerMemberEndsWithItsContainer:
    """A loop body step and a parallel branch step need no successor (F124).

    The reachability walk treated both as ordinary steps and demanded one, so
    the builder gave Naveen's Test Harness an edge the loop ignores: its Wiki
    Searcher pointed at Data Formatter, the step *after* the loop. A loop runs
    the steps in `loop_step_ids` and never follows `next_step_id`;
    `resolve_branch_chain` walks a single-entry branch until the join **or a
    step with no next**, so a branch's last step legitimately has none either.

    Their continuation is their container's, and the container is checked.
    """

    DEADEND = "has no next_step_id"

    @staticmethod
    def _deadends(steps, entry):
        from core.builder_tools import _validate_orchestration
        issues = _validate_orchestration({"id": "o", "entry_step_id": entry, "steps": steps})["issues"]
        return [i for i in issues if "has no next_step_id" in i]

    def _loop(self, body_next, **loop_fields):
        return self._deadends([
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_step_ids": ["body"],
             "next_step_id": "done", **loop_fields},
            {"id": "body", "name": "Body", "type": "print", "print_content": "x",
             "next_step_id": body_next},
            {"id": "done", "name": "Done", "type": "end"},
        ], "loop1")

    def test_a_for_each_body_step_needs_no_successor(self):
        assert self._loop(None, loop_over="state.items") == []

    def test_a_count_loop_body_step_needs_one_no_more_than_a_for_each_one(self):
        assert self._loop(None, loop_count=3) == []

    def test_a_parallel_branch_step_needs_no_successor(self):
        assert self._deadends([
            {"id": "par1", "name": "Split", "type": "parallel",
             "parallel_branches": [["a1"], ["b1"]], "next_step_id": "mrg1"},
            {"id": "a1", "name": "A", "type": "print", "print_content": "a"},
            {"id": "b1", "name": "B", "type": "print", "print_content": "b"},
            {"id": "mrg1", "name": "Join", "type": "merge", "merge_strategy": "dict",
             "input_keys": ["x"], "output_key": "y", "next_step_id": "end1"},
            {"id": "end1", "name": "Done", "type": "end"},
        ], "par1") == []

    def test_a_step_outside_any_container_still_has_to_go_somewhere(self):
        assert self._deadends([
            {"id": "p1", "name": "P", "type": "print", "print_content": "x"},
        ], "p1")

    def test_the_container_itself_is_still_checked(self):
        # The exemption is membership, not "is a container": a loop that is a
        # member of nothing and leads nowhere is still a dead end. (A loop with
        # a body is never one — the walk counts its body as a successor, which
        # is how body steps get visited at all.)
        issues = self._deadends([
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_step_ids": [],
             "loop_count": 3},
        ], "loop1")
        assert len(issues) == 1 and "loop1" in issues[0], issues

    def test_a_chain_led_out_of_a_body_is_still_checked_where_it_lands(self):
        # `b2` is reached from a body step but is not itself a member, so the
        # walk must still reach it and still complain.
        issues = self._deadends([
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_step_ids": ["b1"],
             "next_step_id": "done", "loop_count": 3},
            {"id": "b1", "name": "B1", "type": "print", "print_content": "x",
             "next_step_id": "b2"},
            {"id": "b2", "name": "B2", "type": "print", "print_content": "y"},
            {"id": "done", "name": "Done", "type": "end"},
        ], "loop1")
        assert len(issues) == 1 and "b2" in issues[0], issues


class _FakeSession:
    """An MCP session that answers `list_tools()` with what it was given."""

    def __init__(self, tools):
        self._tools = [types.SimpleNamespace(name=n, description=d) for n, d in tools]

    async def list_tools(self):
        return types.SimpleNamespace(tools=self._tools)



class TestAToolServerListingSaysWhatTheServerDoes:
    """`list_tool_servers` is the first call the builder prompts name (F126).

    The Requirements Analyst is told to call it first, to stop when it has what
    it needs, and never to call `list_all_tools` — and it gets one round of
    tool calls. So a listing of bare names is often the whole basis for picking
    an integration: `deepwiki` reads like a wiki to search, and the agent it
    holds answers questions about GitHub repositories.

    A stored row already carries the server's tools, so saying so is free.
    """

    @staticmethod
    async def _list(monkeypatch, rows, sessions=None):
        import core.builder_tools as bt

        async def _rows():
            return rows

        monkeypatch.setattr(bt, "load_mcp_servers", _rows)
        module = types.SimpleNamespace(agent_sessions=sessions or {})
        return json.loads(await bt.execute_builder_tool("list_tool_servers", {}, module))

    ROW = {
        "name": "deepwiki", "label": "DeepWiki", "server_type": "remote",
        "status": "connected",
        "tools": [
            {"name": "ask_question",
             "description": "Ask any question about a GitHub repository and get an "
                            "AI-powered, context-grounded response.\nUsage notes follow."},
            {"name": "read_wiki_contents",
             "description": "View documentation about a GitHub repository."},
        ],
    }

    async def test_a_server_names_the_tools_it_holds(self, monkeypatch):
        [entry] = await self._list(monkeypatch, [self.ROW])
        assert entry["tool_count"] == 2
        assert [t["name"] for t in entry["tools"]] == ["ask_question", "read_wiki_contents"]
        assert entry["tools"][0]["description"].startswith("Ask any question about a GitHub")

    async def test_only_the_first_line_of_a_description_is_kept(self, monkeypatch):
        # An MCP description is often a paragraph of usage notes under one
        # sentence saying what the tool does. The sentence is the useful part.
        [entry] = await self._list(monkeypatch, [self.ROW])
        assert "Usage notes" not in entry["tools"][0]["description"]

    async def test_the_listing_is_bounded(self, monkeypatch):
        many = dict(self.ROW, tools=[{"name": f"t{i}", "description": "d" * 400}
                                     for i in range(37)])
        [entry] = await self._list(monkeypatch, [many])
        assert entry["tool_count"] == 37, "the count is the real one"
        assert len(entry["tools"]) == 8, "the preview is not"
        assert all(len(t["description"]) <= 90 for t in entry["tools"])

    async def test_a_row_with_no_tools_falls_back_to_its_live_session(self, monkeypatch):
        # OSS never caches tools on the row; cloud's install path does.
        session = _FakeSession([("search", "Search the web.")])
        [entry] = await self._list(
            monkeypatch, [dict(self.ROW, tools=[])],
            {"ext_mcp_deepwiki": session},
        )
        assert entry["tool_count"] == 1
        assert entry["tools"][0]["name"] == "search"

    async def test_a_server_that_cannot_be_reached_is_still_listed(self, monkeypatch):
        [entry] = await self._list(monkeypatch, [dict(self.ROW, tools=[])], {})
        assert entry["name"] == "deepwiki"
        assert entry["tool_count"] is None and entry["tools"] == []

    async def test_a_native_server_gets_its_real_label(self, monkeypatch):
        # "Web Scraper", not "Web_Scraper".title() — the name a person sees on
        # the Tools screen is the name the builder should reason about.
        session = _FakeSession([("scrape_url", "Fetch a URL as markdown.")])
        [entry] = await self._list(monkeypatch, [], {"web_scraper": session})
        assert entry["label"] == "Web Scraper"
        assert entry["type"] == "native_mcp" and entry["tool_count"] == 1
