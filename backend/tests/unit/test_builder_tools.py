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
