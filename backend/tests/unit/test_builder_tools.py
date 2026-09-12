"""The builder's own tools — the ones the AI builder calls to build things.

These went untested for a long time and it cost: two of them returned an error
string on every call (an un-awaited loader), every write rewrote the whole
tenant, and `create_agent` minted ids from a millisecond clock. The tests below
pin the behaviour those bugs violated, so the builder that now runs
workspace-wide cannot quietly lose a row again.
"""
from __future__ import annotations

import json

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
