"""Everything that knows "an orchestrator agent runs a workflow" knows a Workflow step does too (E3, F770; SAI2-P1).

A nested workflow was reachable only through an agent, so every place that reads what a workflow calls looked for an
orchestrator agent: the validator and its loop check (F691), what a delete is refused for (F690), CE1's estimate, a
workflow's model readiness, a step's time limit, whether a Parallel branch may run beside the others, and an
import's warnings. A Workflow step names its workflow itself; each reads it the same way.
"""
import pytest

from _fakes import seed as S


def _wf(sid, workflow_id, nxt=None, key="result"):
    return {"id": sid, "name": sid.title(), "type": "workflow", "workflow_id": workflow_id, "output_key": key,
            "next_step_id": nxt}


def _end():
    return {"id": "end", "name": "End", "type": "end"}


def _parent(orch_id, child_id, **kw):
    return S.make_orchestration(id=orch_id, name=f"Parent {orch_id}", entry_step_id="nested",
                                steps=[_wf("nested", child_id, "end"), _end()], **kw)


def _child(orch_id, steps=None, **kw):
    return S.make_orchestration(id=orch_id, name=f"Child {orch_id}", entry_step_id="c", steps=steps or [
        {"id": "c", "name": "C", "type": "print", "print_content": "hi", "output_key": "made", "next_step_id": "end"},
        _end()], **kw)


class TestTheValidator:
    def test_a_workflow_step_names_its_workflow(self):
        from core.builder_tools import _validate_orchestration

        orch = _parent("orch_v1", None)
        verdict = _validate_orchestration(orch)
        assert any("'nested'" in i and "workflow_id" in i for i in verdict["issues"])
        assert _validate_orchestration(_parent("orch_v2", "orch_other"))["ok"]

    async def test_the_workflow_it_runs_is_called_with_it(self):
        from core.builder_tools import _workflows_called

        await S.seed_orchestrations([_child("orch_v3_child")])
        called, issues = await _workflows_called(_parent("orch_v3", "orch_v3_child"), {})
        assert [c["id"] for c in called] == ["orch_v3_child"] and issues == []

    async def test_a_workflow_that_is_not_there_is_an_issue(self):
        from core.builder_tools import _workflows_called

        called, issues = await _workflows_called(_parent("orch_v4", "orch_nowhere"), {})
        assert called == [] and len(issues) == 1
        assert "'nested'" in issues[0] and "orch_nowhere" in issues[0]

    async def test_a_step_that_runs_its_own_workflow_is_a_loop_f691(self):
        from core.builder_tools import _workflows_called

        _, issues = await _workflows_called(_parent("orch_v5", "orch_v5"), {})
        assert len(issues) == 1 and "without end" in issues[0]

    async def test_a_loop_through_an_inner_workflow_is_found_f691(self):
        from core.builder_tools import _workflows_called

        child = S.make_orchestration(id="orch_v6_child", entry_step_id="back",
                                     steps=[_wf("back", "orch_v6", "end"), _end()])
        await S.seed_orchestrations([child])
        _, issues = await _workflows_called(_parent("orch_v6", "orch_v6_child"), {})
        assert any("without end" in i and "orch_v6_child" in i for i in issues)


class TestADelete:
    async def test_a_workflow_a_workflow_step_runs_is_named_f690(self):
        from core.orchestration.dependents import what_runs

        await S.seed_orchestrations([_parent("orch_d1", "orch_d1_child"), _child("orch_d1_child")])
        found = await what_runs("orch_d1_child")
        assert [(d.kind, d.name, d.id) for d in found] == [("workflow", "Parent orch_d1", "orch_d1")]

    async def test_a_deleted_parent_holds_nothing(self):
        from core.orchestration.dependents import what_runs

        await S.seed_orchestrations([_parent("orch_d2", "orch_d2_child", is_active=False), _child("orch_d2_child")])
        assert await what_runs("orch_d2_child") == []


class TestTheEstimate:
    def test_it_is_marked_nested_as_a_workflow_agents_step_is(self):
        from core.orchestration.estimate import NESTED, span

        priced = span(_parent("orch_e1", "orch_e1_child"), lambda step: (1.0, 1.0))
        assert NESTED in priced.open
        assert priced.low == priced.high == 1.0

    def test_it_calls_no_model_itself(self):
        from core.orchestration.estimate import step_model

        assert step_model(_wf("w", "orch_x"), {}, "ollama.mistral") is None


class TestModelReadiness:
    async def test_the_childs_model_steps_are_read_through_the_step(self):
        from core.orchestration.step_models import walk_agent

        parent = _parent("orch_m1", "orch_m1_child")
        child = _child("orch_m1_child", steps=[
            {"id": "c", "name": "Write", "type": "llm", "prompt_template": "go", "model": "claude-x",
             "next_step_id": None}])
        orchs = {o["id"]: o for o in (parent, child)}

        async def load_orchestration(oid):
            return orchs.get(oid)

        async def load_agent(aid):
            return None

        walked = await walk_agent({"id": "w", "type": "orchestrator", "orchestration_id": "orch_m1"},
                                  load_agent=load_agent, load_orchestration=load_orchestration,
                                  workspace_model="ollama.mistral")
        assert [(s.path, s.model) for s in walked.steps] == [("Nested › Write", "claude-x")]


class TestTheTimeLimit:
    async def test_it_is_the_childs_own_budget(self):
        from core.models_orchestration import StepConfig
        from core.orchestration.time_limits import _step_limit

        await S.seed_orchestrations([_child("orch_t1_child", timeout_minutes=90)])
        limit, why = await _step_limit(StepConfig(**_wf("nested", "orch_t1_child")), engine=None)
        assert limit == 90 * 60 and "Child orch_t1_child" in why


class TestAParallelBranch:
    async def test_a_child_that_can_ask_keeps_the_branch_in_order(self):
        import types

        from core.models_orchestration import StepConfig
        from core.orchestration.steps import _why_in_order

        await S.seed_orchestrations([_child("orch_p1_child", steps=[
            {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?", "output_key": "a",
             "next_step_id": None}])])
        step = StepConfig(**_wf("nested", "orch_p1_child"))
        why = await _why_in_order(types.SimpleNamespace(), ["nested"], {"nested": step})
        assert why and "Child orch_p1_child" in why


class TestAnImport:
    async def test_a_workflow_step_naming_a_workflow_not_here_is_said(self):
        from core.routes.import_export import _warnings

        warnings = await _warnings([_parent("orch_i1", "orch_not_here")], [], [])
        assert any("orch_not_here" in w["message"] for w in warnings)


class TestTheRunsModel:
    def test_a_runs_model_is_not_set_on_it(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import apply_model_override

        orch = Orchestration.model_validate(_parent("orch_o1", "orch_o1_child"))
        apply_model_override(orch, "claude-x")
        assert orch.steps[0].model is None
