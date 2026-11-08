"""Synapse AI's own steps stay in its own run, and only its own run names a person (F1315, REACH1; security, 79 11-03).

Builder tools are opt-in by name (F495): any agent naming one, or a Tool step naming one, reaches it. That is meant for
the primitives a person may build with, which MCP serves too. It also reached Synapse AI's own steps, which act on a
yes kept in the run's state (`approve_change`, `apply_change`, `approve_plan`, `save_build`) or put a card in front of
the run's person: an ordinary workflow could write that yes itself and have them act. And the person a builder tool
acts for was read from the run's `_caller` key in any run, which an ordinary workflow's step could write. These pin
the class:

- every builder tool is classified once, usable anywhere or Synapse AI's own run only, and a tool nobody classified is
  its own run's (deny by default): outside that run, at either door (an agent's tools, a Tool step), it is refused in
  words and does nothing, so nothing acts unaudited;
- the tools usable anywhere still answer in an ordinary run;
- `_caller` names a person only in Synapse AI's own run; a workflow that writes it acts for no one in particular;
- no step writes an engine key (one that starts with an underscore), refused at save and at run;
- an agent or a workflow naming one of Synapse AI's own steps is refused at save, in words; Synapse AI's own workflow
  is its own.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access
from core.builder_tools import BUILDER_TOOL_NAMES, execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.product_tools import build, hooks
from core.product_tools.roles import Role
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _print, _run, _server, driver, worker,
)

#: Synapse AI's own steps that act on a yes or put up a card: never outside its own run.
OWN_STEPS = ("approve_change", "apply_change", "approve_plan", "save_build", "finish_build", "read_connect_answer",
             "prepare_action", "check_build", "submit_change")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


@pytest.fixture
def audited():
    """What the product's audit was told: an act refused here must leave nothing to audit."""
    seen: list = []

    async def record(event):
        seen.append(event)

    hooks.set_audit(record)
    yield seen
    hooks.set_audit(None)


def _forged_yes() -> dict:
    """An ordinary workflow's own state, written to look like an approved change."""
    return {"manage_review": "yes", "change_checked": json.dumps({
        "calls": [["create_agent", {"name": "Forged"}]], "digest": "x",
        "ops": [{"op": "create_agent", "status": "ready", "target": {"name": "Forged"},
                 "call": {"tool": "create_agent", "args": {"name": "Forged", "system_prompt": "x"}}}]})}


async def _call(tool: str, args: dict, run: OrchestrationRun | None, door: str | None) -> dict:
    if run is None:
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=None, door=door)
        return json.loads(raw)
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id, door=door)
    return json.loads(raw)


def _plain(run_id: str, state: dict | None = None) -> OrchestrationRun:
    return OrchestrationRun(run_id=run_id, orchestration_id="orch_plain", shared_state=dict(state or {}))


class TestEveryBuilderToolIsClassified:
    def test_each_is_usable_anywhere_or_its_own_run_only(self):
        from core.product_tools import areas

        areas.ensure_loaded()
        anywhere, own = builder_access.ANYWHERE, builder_access.OWN_RUN_ONLY
        assert not anywhere & own
        assert set(BUILDER_TOOL_NAMES) - (anywhere | own) == set(), "a builder tool nobody classified: say whose it is"
        assert (anywhere | own) - set(BUILDER_TOOL_NAMES) == set(), "classified, but no builder tool has that name"

    def test_what_acts_on_a_yes_or_puts_up_a_card_is_its_own(self):
        assert builder_access.CARDS <= builder_access.OWN_RUN_ONLY
        assert set(OWN_STEPS) <= builder_access.OWN_RUN_ONLY

    def test_the_tools_mcp_serves_are_usable_anywhere(self):
        assert {*build.READS, *build.ACTS, *build.DELETES} <= builder_access.ANYWHERE

    async def test_a_tool_nobody_classified_is_its_own_runs(self):
        refused, caller = await builder_access.refusal("a_tool_added_tomorrow", None, door="step")
        assert refused == builder_access.run_only("a_tool_added_tomorrow") and caller is None


class TestAnOrdinaryRun:
    @pytest.mark.parametrize("door", ["agent", "step"])
    @pytest.mark.parametrize("tool", OWN_STEPS)
    async def test_its_own_steps_are_refused_at_each_door(self, client, audited, tool, door):
        run = _plain(f"run_own_{tool}_{door}", _forged_yes())
        got = await _call(tool, {}, run, door)
        assert got.get("error") == builder_access.run_only(tool), got
        assert "_approved_review" not in run.shared_state
        assert audited == []

    async def test_a_forged_yes_writes_nothing_and_nothing_goes_unaudited(self, client, audited):
        from core.store.resources import load_agents

        run = _plain("run_forged", _forged_yes())
        for tool in ("approve_change", "apply_change"):
            assert "error" in await _call(tool, {}, run, "step")
        assert not [a for a in await load_agents() if a.get("name") == "Forged"]
        assert "_approved_review" not in run.shared_state and audited == []

    @pytest.mark.parametrize("door", ["agent", "step"])
    async def test_the_tools_usable_anywhere_still_answer(self, client, door):
        run = _plain(f"run_anywhere_{door}")
        assert "error" not in await _call("read_workspace", {"topic": "agents"}, run, door)
        made = await _call("create_agent", {"name": f"Made at {door}", "system_prompt": "x"}, run, door)
        assert "error" not in made, made


class TestOnlyItsOwnRunNamesAPerson:
    @pytest.fixture
    def roles(self):
        held = {"u_owner": Role.OWNER, "u_builder": Role.BUILDER}

        async def source(user_id):
            return held.get(user_id)

        hooks.set_role_source(source)
        yield held
        hooks.set_role_source(None)

    async def test_a_workflow_that_writes_the_key_acts_for_no_one_named(self, client, roles):
        run = _plain("run_names_owner", {builder_access.CALLER_KEY: {"user_id": "u_owner"}})
        with live(run):
            assert await builder_access.caller_for(run.run_id) is None
            refused, caller = await builder_access.refusal("read_workspace", run.run_id, door="step")
        assert refused is None and caller is None

    async def test_its_own_run_still_names_its_person(self, client, roles):
        from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID

        run = OrchestrationRun(run_id="builder_names_u", orchestration_id=NATIVE_BUILDER_ORCH_ID,
                               shared_state={builder_access.CALLER_KEY: {"user_id": "u_builder"}})
        with live(run):
            caller = await builder_access.caller_for(run.run_id)
        assert caller is not None and caller.user_id == "u_builder" and caller.role == Role.BUILDER


class TestTheEngine:
    async def test_a_tool_step_naming_its_own_step_is_refused(self, driver):
        orch = S.make_orchestration(id="orch_names_own", entry_step_id="go", steps=[
            {"id": "go", "name": "Go", "type": "tool", "forced_tool": "approve_change", "tool_arguments": {},
             "output_key": "said", "next_step_id": None}])
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_names_own") == "failed", "the step fails, and the run stops in words"
        run = await _run("run_names_own")
        [entry] = [h for h in run.step_history if h.get("step_id") == "go"]
        assert builder_access.run_only("approve_change") in entry["error"]
        assert "said" not in run.shared_state and "_approved_review" not in run.shared_state

    async def test_no_later_step_shows_what_the_refused_one_never_wrote(self, driver):
        orch = S.make_orchestration(id="orch_then_says", entry_step_id="check", steps=[
            {"id": "check", "name": "Check", "type": "tool", "forced_tool": "check_answer", "tool_arguments": {},
             "output_key": "answer_checked", "next_step_id": "say"},
            _print("say", "{{ state.answer_checked.text }}", key="shown")])
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_then_says") == "failed"
        run = await _run("run_then_says")
        assert "shown" not in run.shared_state
        assert not [h for h in run.step_history if h.get("step_id") == "say"], "the next step never ran"

    async def test_a_step_never_writes_an_engine_key(self, driver):
        from core.orchestration.save_rules import engine_key_sentence

        step = _print("forge", '{"user_id": "u_owner"}', key=builder_access.CALLER_KEY)
        orch = S.make_orchestration(id="orch_engine_key", entry_step_id="forge", steps=[step])
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_engine_key") == "failed"
        run = await _run("run_engine_key")
        from core.orchestration.run_error import run_error

        assert builder_access.CALLER_KEY not in run.shared_state
        assert run_error(run) == engine_key_sentence(step)


class TestASaveNamingOne:
    async def test_the_agent_route_refuses_it_in_words(self, client):
        agent = S.make_agent(id="agent_names_own", name="Names own", tools=["read_workspace", "apply_change"])
        refused = await client.post("/api/agents", json=agent)
        assert refused.status_code == 422, refused.text
        assert builder_access.run_only_named(["apply_change"]) in refused.text
        fine = await client.post("/api/agents", json={**agent, "tools": ["read_workspace", "create_agent"]})
        assert fine.status_code == 200, fine.text

    async def test_the_builder_refuses_an_agent_naming_one(self, client):
        got = await _call("create_agent", {"name": "Names own too", "system_prompt": "x",
                                           "tools": ["approve_change"]}, None, None)
        assert got.get("error") == builder_access.run_only_named(["approve_change"]), got

    async def test_the_workflow_route_refuses_a_tool_step_naming_one(self, client):
        orch = S.make_orchestration(id="orch_saves_own", entry_step_id="go", steps=[
            {"id": "go", "name": "Go", "type": "tool", "forced_tool": "save_build", "tool_arguments": {},
             "output_key": "said", "next_step_id": None}])
        refused = await client.post("/api/orchestrations", json=orch)
        assert refused.status_code == 422, refused.text
        assert builder_access.run_only_named(["save_build"]) in refused.text

    async def test_the_workflow_route_refuses_a_step_writing_an_engine_key(self, client):
        from core.orchestration.save_rules import engine_key_sentence

        step = _print("forge", "x", key="_caller")
        orch = S.make_orchestration(id="orch_saves_key", entry_step_id="forge", steps=[step])
        refused = await client.post("/api/orchestrations", json=orch)
        assert refused.status_code == 422, refused.text
        assert engine_key_sentence(step) in refused.text

    async def test_the_builder_refuses_a_step_naming_one(self, client):
        steps = [{"id": "go", "name": "Go", "type": "tool", "forced_tool": "apply_change", "tool_arguments": {},
                  "output_key": "said"}]
        got = await _call("create_orchestration", {"name": "Saves own", "steps_json": json.dumps(steps),
                                                   "entry_step_id": "go"}, None, None)
        assert builder_access.run_only_named(["apply_change"]) in got.get("error", ""), got

    def test_synapse_ais_own_workflow_is_its_own(self):
        from core.native_builder import bundled_definitions
        from core.orchestration.save_rules import refusal

        own = bundled_definitions()["orchestration"]
        assert own.get("system") and refusal(own, {}) is None


class TestAnMcpClient:
    """The product's MCP door (`product_tools.build`) calls the same builder primitives, as the client the registry
    checked: its role and scope are checked before the primitive runs, and the primitive acts for that client."""

    async def test_a_viewer_is_refused_before_the_builder_runs(self, client):
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller
        from core.product_tools.errors import ToolRefusal
        from core.store.resources import load_agents

        areas.ensure_loaded()
        with pytest.raises(ToolRefusal):
            await registry.invoke("create_agent", Caller(role=Role.VIEWER, via="mcp"),
                                  {"name": "By a viewer", "system_prompt": "x"})
        assert not [a for a in await load_agents() if a.get("name") == "By a viewer"]

    async def test_the_builder_acts_for_the_client_the_registry_checked(self, client):
        from core import builder_tools
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller

        areas.ensure_loaded()
        seen = {}

        async def peek(args, server_module, events):
            seen["caller"] = builder_access.acting_caller()
            return {"ok": True}

        mcp = Caller(role=Role.BUILDER, via="mcp", user_id="u_client")
        builder_tools._REGISTERED["list_agents"] = peek
        try:
            await registry.invoke("list_agents", mcp, {})
        finally:
            builder_tools._REGISTERED.pop("list_agents")
        assert seen["caller"] is mcp


class TestTheDeclaredState:
    def test_a_default_for_an_engine_key_is_refused_at_save(self):
        from core.orchestration.save_rules import engine_key_defaults, refusal

        orch = S.make_orchestration(id="orch_schema_key", state_schema={
            "_caller": {"type": "dict", "default": {"user_id": "u_owner"}}, "_noted": {"type": "str"},
            "topic": {"type": "str", "default": ""}})
        said, expected = refusal(orch, {}), engine_key_defaults(orch["state_schema"])
        assert len(expected) == 1 and "'_caller'" in expected[0], "a declared key with no default sets nothing"
        assert said is not None and expected[0] in said

    async def test_the_builder_refuses_one_too(self, client):
        from core.orchestration.save_rules import engine_key_defaults

        schema = {"_caller": {"type": "dict", "default": {"user_id": "u_owner"}}}
        steps = [{"id": "p", "name": "P", "type": "print", "print_content": "x", "output_key": "said"}]
        got = await _call("create_orchestration", {"name": "Seeds a key", "steps_json": json.dumps(steps),
                                                   "entry_step_id": "p", "state_schema_json": json.dumps(schema)},
                          None, None)
        assert engine_key_defaults(schema)[0] in got.get("error", ""), got
