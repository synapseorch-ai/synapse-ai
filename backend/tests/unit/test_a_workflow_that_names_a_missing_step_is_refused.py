"""A workflow that names a step it does not have is refused before any step runs, and is never saved whole — F1459
(ISS1, GitHub #376).

#376, 2.0.0-rc2: a saved `a -> ghost` ran `a` and then failed on "Step 'ghost' not found" — after `a`'s side effect.
`POST /api/orchestrations` saved such a workflow 200, and `update_step` saved it too. The reference checks lived
only inside the builder's validator (`_validate_orchestration`), which no save door and no run door asked: the
engine noticed at the step (`engine.py`, "Step '…' not found").

One pure check (`save_rules.step_reference_problems`) now holds them, in the validator's own words. Every door that
saves a whole workflow refuses with it (`save_rules.refusal`: the editor's save, an import, cloud's save; a plan's
build says them through its compiler's validator). Every door that starts a run checks it before the run starts (a failed run's Replay among them). The engine's own
handling of a missing step met mid-run stays, for a paused run answered: it carries on with its kept copy. The builder's
step tools still save a workflow half built — `create_orchestration_skeleton` names its entry before the step exists.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S

UNKNOWN = "Step 'a' next_step_id 'ghost' points to unknown step."


def _a_to(nxt: str | None, oid: str = "orch_ghost") -> dict:
    return S.make_orchestration(id=oid, entry_step_id="a", steps=[
        {"id": "a", "name": "A", "type": "print", "print_content": "first", "output_key": "o_a",
         "next_step_id": nxt}])


def _names_it(text: str) -> None:
    assert UNKNOWN in text, text


@pytest.fixture
async def dangling():
    await S.seed_orchestrations([_a_to("ghost")])
    return "orch_ghost"


class TestTheCheck:
    def test_it_says_the_step_and_the_field_in_the_validators_words(self):
        from core.orchestration.save_rules import step_reference_problems

        assert step_reference_problems(_a_to("ghost")) == [UNKNOWN]

    def test_a_workflow_whose_steps_all_exist_has_none(self):
        from core.orchestration.save_rules import step_reference_problems

        assert step_reference_problems(_a_to(None)) == []

    def test_an_entry_that_names_no_step(self):
        from core.orchestration.save_rules import step_reference_problems

        orch = {**_a_to(None), "entry_step_id": "ghost"}
        assert step_reference_problems(orch) == ["entry_step_id 'ghost' does not match any step."]

    def test_an_empty_workflow_has_none(self):
        from core.orchestration.save_rules import step_reference_problems

        assert step_reference_problems({"id": "o", "name": "Empty", "steps": [], "entry_step_id": ""}) == []

    @pytest.mark.parametrize("field,value", [
        ("on_exhausted_step_id", "ghost"),
        ("route_map", {"yes": "ghost"}),
        ("if_true_step_id", "ghost"),
        ("if_false_step_id", "ghost"),
        ("switch_cases", {"x": "ghost"}),
        ("switch_default_step_id", "ghost"),
        ("loop_step_ids", ["ghost"]),
        ("parallel_branches", [["ghost"]]),
    ])
    def test_every_field_that_names_a_step(self, field, value):
        """Each field on the step type that reads it, as the validator checks it — and in its very words."""
        from core.builder_tools import _validate_orchestration
        from core.orchestration.save_rules import step_reference_problems

        orch = _a_to(None)
        step = orch["steps"][0]
        if field.startswith("if_"):
            step.update(type="if_else", if_condition="state.x == 1")
        elif field.startswith("switch_"):
            step.update(type="switch", switch_expression="state.kind")
        step[field] = value
        found = step_reference_problems(orch)
        assert len(found) == 1, found
        assert "'a'" in found[0] and ("ghost" in found[0] or field in found[0]), found
        assert found[0] in _validate_orchestration(orch, allow_dead_ends=True)["issues"]

    def test_the_validator_says_the_same_words(self):
        from core.builder_tools import _validate_orchestration

        assert UNKNOWN in _validate_orchestration(_a_to("ghost"), allow_dead_ends=True)["issues"]


class TestEachDoorThatSavesAWholeWorkflow:
    async def test_the_editors_save_is_a_422_naming_the_step_and_field(self, client):
        resp = await client.post("/api/orchestrations", json=_a_to("ghost", "orch_save_ghost"))

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("Refusing to save:"), detail
        _names_it(detail)
        assert (await client.get("/api/orchestrations/orch_save_ghost")).status_code == 404

    async def test_an_entry_that_names_no_step_is_a_422(self, client):
        orch = {**_a_to(None, "orch_save_entry"), "entry_step_id": "ghost"}
        resp = await client.post("/api/orchestrations", json=orch)

        assert resp.status_code == 422, resp.text
        assert "entry_step_id 'ghost' does not match any step." in resp.json()["detail"]

    async def test_a_workflow_whose_steps_all_exist_saves(self, client):
        resp = await client.post("/api/orchestrations", json=_a_to(None, "orch_save_fine"))
        assert resp.status_code == 200, resp.text

    async def test_a_new_empty_workflow_saves(self, client):
        resp = await client.post("/api/orchestrations", json={
            "id": "orch_save_empty", "name": "Empty", "steps": [], "entry_step_id": ""})
        assert resp.status_code == 200, resp.text

    async def test_an_import_of_one_is_refused(self, client):
        resp = await client.post("/api/import", json={
            "bundle": {"synapse_export": True, "version": "1.1", "orchestrations": [_a_to("ghost", "orch_imp")],
                       "agents": [], "mcp_servers": [], "custom_tools": []},
            "selected_orchestration_ids": ["orch_imp"], "selected_agent_ids": [],
            "selected_mcp_server_names": [], "selected_custom_tool_names": []})

        assert resp.status_code == 422, resp.text
        _names_it(resp.json()["detail"])


class TestTheBuilderStillBuildsInOrder:
    async def test_a_skeleton_names_its_entry_before_the_step_exists(self):
        from core.builder_tools import execute_builder_tool

        async def call(tool, args):
            return json.loads(await execute_builder_tool(tool, args, types.SimpleNamespace(agent_sessions={})))

        made = await call("create_orchestration_skeleton", {"name": "In order", "entry_step_id": "a"})
        assert made.get("status") == "created", made
        oid = made["orchestration"]["id"]
        first = await call("add_steps", {"orch_id": oid, "steps_json": json.dumps([
            {"id": "a", "name": "A", "type": "print", "print_content": "x", "output_key": "o_a",
             "next_step_id": "b"}])})
        assert first.get("status") == "appended", first
        second = await call("add_steps", {"orch_id": oid, "steps_json": json.dumps([
            {"id": "b", "name": "B", "type": "print", "print_content": "y", "output_key": "o_b"}])})
        assert second.get("status") == "appended", second


class TestEachDoorThatStartsARun:
    async def test_the_run_route_answers_422(self, client, dangling):
        resp = await client.post(f"/api/orchestrations/{dangling}/run", json={"message": "go"})
        assert resp.status_code == 422, resp.text
        _names_it(resp.json()["detail"])

    @pytest.mark.parametrize("path", ["run", "run/stream"])
    async def test_v1_s_run_routes_answer_422(self, client, api_key, dangling, path):
        resp = await client.post(f"/api/v1/orchestrations/{dangling}/{path}", json={"message": "go"},
                                 headers=api_key["headers"])
        assert resp.status_code == 422, resp.text
        _names_it(resp.json()["detail"])

    async def test_mcp_refuses_it_before_a_run_starts(self, dangling):
        from core.mcp_server.runs import start_run
        from core.product_tools.errors import ToolRefusal

        with pytest.raises(ToolRefusal) as e:
            await start_run(dangling, "go", None, 0)
        _names_it(str(e.value))

    async def test_a_schedule_s_run_stops_on_the_words(self, dangling):
        from core.orchestration.read_shapes import Unreadable
        from core.scheduler import ScheduleManager

        m = ScheduleManager()
        m._server_module = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        with pytest.raises(Unreadable) as e:
            await m._run_orchestration_schedule({"id": "sched_ghost", "target_id": dangling, "prompt": "go"},
                                                types.SimpleNamespace())
        _names_it(str(e.value))

    async def test_an_agent_that_runs_it_says_it_in_the_chat(self, dangling):
        from core.react_engine import run_orchestrator_agent

        agent = S.make_orchestrator_agent(dangling, id="agent_ghost")
        events = [e async for e in run_orchestrator_agent(agent, "go", types.SimpleNamespace(agent_sessions={}))]
        # As the engine said a missing step, so a chat stream reads it as a run's failure.
        errors = [e["error"] for e in events if e.get("type") == "orchestration_error"]
        assert errors, events
        _names_it(errors[0])
        assert not [e for e in events if e.get("type") == "step_start"], events

    async def test_a_workflow_step_whose_child_it_is_fails_before_the_child_runs_a_step(self, dangling):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        parent = S.make_orchestration(id="orch_parent_ghost", entry_step_id="child", steps=[
            {"id": "child", "name": "Run it", "type": "workflow", "workflow_id": dangling, "next_step_id": None}])
        engine = OrchestrationEngine(Orchestration.model_validate(parent), types.SimpleNamespace(agent_sessions={}))
        events = [e async for e in engine.run("go", "run_parent_ghost")]
        reasons = [e["reason"] for e in events if e.get("type") == "step_error" and e.get("reason")]
        assert reasons, events
        _names_it(reasons[0])
        assert not [e for e in events if e.get("type") == "step_start" and e.get("orch_step_id") == "a"], events

    async def test_a_failed_run_replayed_from_the_saved_workflow_says_it(self, dangling):
        from core.orchestration.engine import OrchestrationEngine
        from core.store import get_store, upsert
        from core.store.models import OrchestrationRunDB

        async with (await get_store())() as session:
            await upsert(session, OrchestrationRunDB, values={
                "run_id": "run_ghost_failed", "orchestration_id": dangling, "status": "failed",
                "current_step_id": "a"}, index_elements=["run_id"])
            await session.commit()
        events = [e async for e in OrchestrationEngine.resume_failed(
            "run_ghost_failed", types.SimpleNamespace(agent_sessions={}))]
        errors = [e["error"] for e in events if e.get("type") == "orchestration_error"]
        assert errors, events
        _names_it(errors[0])
        assert not [e for e in events if e.get("type") == "step_start"], events


class TestAnEvalSet:
    async def test_its_run_does_not_start(self, dangling):
        """Each case starts the workflow afresh: refused before any suite run exists, so no case runs."""
        from core.evals import store as evals_store
        from core.evals.service import EvalsRefused, start_suite_run
        from core.evals.suites import running_for

        await evals_store.save_suite({"id": "set_ghost", "name": "Ghost set", "orchestration_id": dangling,
                                      "cases": [{"input": "go"}]})
        with pytest.raises(EvalsRefused) as e:
            await start_suite_run("set_ghost", types.SimpleNamespace(agent_sessions={}))
        assert e.value.status == 422
        _names_it(e.value.detail)
        assert await running_for("set_ghost") is None


class TestAPausedRunIsNotStranded:
    async def test_its_answer_carries_on_past_a_link_it_never_takes(self):
        """A run started before the check, waiting on a person, with a dangling link off its path: answered, it
        finishes as it would have. Only a run's start is refused."""
        from core.orchestration.engine import OrchestrationEngine

        await S.seed_orchestrations([S.make_orchestration(id="orch_paused_ghost", entry_step_id="h", steps=[
            {"id": "h", "name": "Ask", "type": "human", "human_prompt": "ok?", "output_key": "answer",
             "next_step_id": "p"},
            {"id": "p", "name": "P", "type": "print", "print_content": "done", "output_key": "o_p",
             "next_step_id": None},
            {"id": "q", "name": "Q", "type": "print", "print_content": "never", "output_key": "o_q",
             "next_step_id": "ghost"}])])
        await S.seed_paused_run("run_paused_ghost", "orch_paused_ghost")

        events = [e async for e in OrchestrationEngine.resume(
            "run_paused_ghost", {"response": "yes"}, types.SimpleNamespace(agent_sessions={}))]

        assert not [e for e in events if e.get("type") == "orchestration_error"], events
        assert [e for e in events if e.get("type") == "step_complete" and e.get("orch_step_id") == "p"], events
