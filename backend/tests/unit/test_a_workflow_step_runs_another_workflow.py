"""A Workflow step runs another workflow by its id, as a nested run (E3, F770; SAI2-P1, Naveen 2026-10-21).

Nesting used to be reachable only through an orchestrator agent: an agent row per nested workflow, a deploy step, and
an Agent step whose agent stood for the workflow. P0's schema probe found models asked for a nested build invent a
"run a workflow" step type. The Workflow step is that type, in the engine: it names the workflow, and the engine's own
nested executor runs it — in-process, inside its parent's run, with its root's cap, resumed through the step when the
child asks a person something. The orchestrator agent stays for chat and channels (D204).

Run both in-process and on the fleet's adapter (`driver`).
"""
import pytest

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _broken, _human, _print, _run, _server, driver, worker,
)


def _workflow_step(workflow_id, nxt=None, sid="nested", prompt="{state.user_input}", key="result"):
    step = {"id": sid, "name": "Nested", "type": "workflow", "workflow_id": workflow_id, "output_key": key,
            "next_step_id": nxt}
    if prompt is not None:
        step["prompt_template"] = prompt
    return step


async def _seed(*orchs):
    await S.seed_orchestrations(list(orchs))


def _entries(run, step_id):
    return [h for h in run.step_history if h.get("step_id") == step_id]


class TestItRunsTheWorkflow:
    async def test_the_child_runs_and_what_it_made_is_the_steps_output(self, driver):
        parent = S.make_orchestration(id="orch_e3", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_child", "tail"), _print("tail", "parent got {state.result}", key="tail")])
        child = S.make_orchestration(id="orch_e3_child", entry_step_id="c", steps=[
            _print("c", "child read {state.user_input}", key="made")], result_key="made")
        await _seed(parent, child)

        assert await driver.start(parent, "run_e3") == "completed"

        run = await _run("run_e3")
        assert run.shared_state["result"] == "child read go"
        assert run.shared_state["tail"] == "parent got child read go"
        entries = _entries(run, "nested")
        assert len(entries) == 1 and entries[0]["step_type"] == "workflow"

    async def test_its_rendered_prompt_is_the_childs_input(self, driver):
        parent = S.make_orchestration(id="orch_e3_in", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_in_child", prompt="summarise {state.user_input} for ops")])
        child = S.make_orchestration(id="orch_e3_in_child", entry_step_id="c", steps=[
            _print("c", "{state.user_input}", key="made")])
        await _seed(parent, child)

        await driver.start(parent, "run_e3_in")

        assert (await _run("run_e3_in")).shared_state["result"] == "summarise go for ops"

    async def test_with_no_prompt_the_runs_input_is_the_childs(self, driver):
        parent = S.make_orchestration(id="orch_e3_np", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_np_child", prompt=None)])
        child = S.make_orchestration(id="orch_e3_np_child", entry_step_id="c", steps=[
            _print("c", "{state.user_input}", key="made")])
        await _seed(parent, child)

        await driver.start(parent, "run_e3_np")

        assert (await _run("run_e3_np")).shared_state["result"] == "go"

    async def test_a_value_that_is_not_text_stays_what_it_is(self, driver):
        parent = S.make_orchestration(id="orch_e3_list", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_list_child")])
        child = S.make_orchestration(id="orch_e3_list_child", entry_step_id="t", result_key="items", steps=[
            {"id": "t", "name": "T", "type": "transform", "transform_code": "result = [1, 2, 3]",
             "output_key": "items", "next_step_id": None}])
        await _seed(parent, child)

        await driver.start(parent, "run_e3_list")

        assert (await _run("run_e3_list")).shared_state["result"] == [1, 2, 3]


class TestWhenItCannotRun:
    async def test_a_child_that_fails_fails_the_step(self, driver):
        parent = S.make_orchestration(id="orch_e3_f", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_f_child", "tail"), _print("tail", "carried on", key="tail")])
        child = S.make_orchestration(id="orch_e3_f_child", entry_step_id="broken", steps=[_broken()])
        await _seed(parent, child)

        assert await driver.start(parent, "run_e3_f") == "failed"

        run = await _run("run_e3_f")
        assert "tail" not in run.shared_state and "result" not in run.shared_state

    async def test_a_workflow_that_is_not_there_fails_the_step_naming_it(self, driver):
        parent = S.make_orchestration(id="orch_e3_gone", entry_step_id="nested", steps=[
            _workflow_step("orch_nowhere")])
        await _seed(parent)

        assert await driver.start(parent, "run_e3_gone") == "failed"

        run = await _run("run_e3_gone")
        assert any("orch_nowhere" in str(h.get("error") or "") for h in _entries(run, "nested"))

    async def test_a_deleted_workflow_kept_as_a_record_never_runs(self, driver):
        parent = S.make_orchestration(id="orch_e3_del", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_del_child")])
        child = S.make_orchestration(id="orch_e3_del_child", entry_step_id="c", is_active=False,
                                     steps=[_print("c", "ran", key="made")])
        await _seed(parent, child)

        assert await driver.start(parent, "run_e3_del") == "failed"
        assert "result" not in (await _run("run_e3_del")).shared_state

    async def test_a_step_with_no_workflow_fails(self, driver):
        parent = S.make_orchestration(id="orch_e3_none", entry_step_id="nested", steps=[
            _workflow_step(None)])
        await _seed(parent)

        assert await driver.start(parent, "run_e3_none") == "failed"

    async def test_a_workflow_that_runs_itself_stops_at_the_depth_limit(self, driver):
        parent = S.make_orchestration(id="orch_e3_self", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_self")])
        await _seed(parent)

        assert await driver.start(parent, "run_e3_self") == "failed"


class TestAQuestionInsideTheChild:
    async def test_it_waits_and_the_answer_resumes_the_child_through_the_step(self, driver):
        parent = S.make_orchestration(id="orch_e3_ask", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_ask_child", "tail"), _print("tail", "after {state.result}", key="tail")])
        child = S.make_orchestration(id="orch_e3_ask_child", entry_step_id="ask", result_key="made", steps=[
            _human("ask", key="approval", nxt="c"), _print("c", "child saw {state.approval}", key="made")])
        await _seed(parent, child)

        assert await driver.start(parent, "run_e3_ask") == "paused"
        paused = await _run("run_e3_ask")
        assert paused.nested_orch_id == "orch_e3_ask_child" and paused.nested_run_id

        assert await driver.answer("run_e3_ask", "yes") == "completed"

        run = await _run("run_e3_ask")
        assert run.shared_state["result"] == "child saw yes"
        assert run.shared_state["tail"] == "after child saw yes"
        entries = _entries(run, "nested")
        assert len(entries) == 1 and entries[0]["status"] == "completed"
        assert entries[0]["step_type"] == "workflow"


class TestItIsItsRootsWork:
    async def test_the_roots_cap_holds_inside_the_child(self, driver):
        from core.orchestration.run_cap import RunCap
        from tests.unit.test_a_run_stops_at_its_cap import reader

        asked = reader()
        parent = S.make_orchestration(id="orch_e3_cap", entry_step_id="nested", max_run_credits=2, steps=[
            _workflow_step("orch_e3_cap_child", "tail"), _print("tail", "parent", key="tail")])
        child = S.make_orchestration(id="orch_e3_cap_child", entry_step_id="c0", max_run_credits=50, steps=[
            _print(f"c{i}", f"child {i}", nxt=f"c{i + 1}" if i < 2 else None, key=f"c{i}") for i in range(3)])
        await _seed(parent, child)

        assert await driver.start(parent, "run_e3_cap") == "failed"

        assert any(rid.startswith("run_e3_cap__nested_") for rid, _, _ in asked)
        assert {c for _, _, c in asked} == {RunCap(credits=2.0)}

    async def test_the_child_runs_inside_its_parent_not_through_a_new_runs_start(self, monkeypatch):
        """A child is part of its parent's run, as the orchestrator agent's child is: never placed, queued or
        dispatched as a run of its own (cloud's run_queue and its admission count one per root, F283)."""
        from core.models_orchestration import Orchestration
        from core.orchestration import engine as engine_mod

        started: list[tuple[str, int]] = []
        real_run = engine_mod.OrchestrationEngine.run

        def spy(self, *args, **kwargs):
            started.append((kwargs.get("run_id") or "", self.depth))
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(engine_mod.OrchestrationEngine, "run", spy)
        parent = S.make_orchestration(id="orch_e3_door", entry_step_id="nested", steps=[
            _workflow_step("orch_e3_door_child")])
        child = S.make_orchestration(id="orch_e3_door_child", entry_step_id="c", steps=[_print("c", "ok", key="m")])
        await _seed(parent, child)

        events = [e async for e in engine_mod.OrchestrationEngine(
            Orchestration.model_validate(parent), _server()).run("go", run_id="run_e3_door")]

        assert [d for _, d in started] == [0, 1]
        assert started[1][0].startswith("run_e3_door__nested_")
        assert not [e for e in events if e.get("type") == "orchestration_start" and e.get("run_id") != "run_e3_door"]


class TestTheOrchestratorAgentStays:
    async def test_an_agent_step_whose_agent_is_a_workflow_still_runs_it(self, driver):
        parent = S.make_orchestration(id="orch_e3_agent", entry_step_id="delegate", steps=[
            {"id": "delegate", "name": "Deep dive", "type": "agent", "agent_id": "agent_e3_wrap",
             "prompt_template": "go", "output_key": "result", "next_step_id": None}])
        child = S.make_orchestration(id="orch_e3_agent_child", entry_step_id="c", steps=[
            _print("c", "wrapped {state.user_input}", key="made")])
        await S.seed_agents([S.make_orchestrator_agent("orch_e3_agent_child", id="agent_e3_wrap")])
        await _seed(parent, child)

        assert await driver.start(parent, "run_e3_agent") == "completed"
        run = await _run("run_e3_agent")
        assert run.shared_state["result"] == "wrapped go"
        assert _entries(run, "delegate")[0]["step_type"] == "agent"


@pytest.mark.parametrize("step_type", ["workflow"])
def test_the_engine_knows_the_type(step_type):
    from core.models_orchestration import StepConfig, StepType
    from core.orchestration.steps import STEP_EXECUTORS

    assert StepType(step_type) in STEP_EXECUTORS
    assert StepConfig(id="w", name="W", type=step_type, workflow_id="orch_x").workflow_id == "orch_x"
