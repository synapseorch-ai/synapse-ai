"""An expression the engine refuses fails its step; it never routes — F850 (BF11, D311; Naveen 2026-10-23).

Found by QA1b (D314) on prod: a Switch whose `switch_expression` called `.splitlines()` saved with no issue, and
its run took the default branch and read Completed, with "Expression eval error: unsafe expression — …" only in
Activity. An If/Else did the same by design, treating a refused condition as False: a gate in front of an approval
or a publish could skip it. A refused expression was routed as if it were a decision.

Naveen's call: at run time a REFUSED If/Else, Switch or Loop expression fails the step, naming why. An expression
that is allowed but cannot read this run's state (a key not there yet) keeps routing as before: that is data, and
the workflow's author planned for it. Saving one is refused at every door (`core.orchestration.save_rules`); this
is the run that meets one already stored.
"""
import types

import pytest
from _fakes import seed as S

# QA1b's expression used `.splitlines()`; F957 offers it now, so the refused example is a method that stays out
# (`.zfill`: its size is the caller's). The rule tested is the same.
SPLIT = "'urgent' if 'urgent' in str(state.classification).lower().zfill(9) else 'routine'"
REFUSED = ".zfill(…) is not one of the methods available here"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _print(sid):
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid, "output_key": "o",
            "next_step_id": None}


async def _run(orch, initial_state=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [e async for e in engine.run("go", run_id=f"run_{orch['id']}", initial_state=initial_state)]


def _status(events):
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"]


def _reason(events):
    return next(e for e in events if e.get("type") == "step_error")["reason"]


def _reached(events, sid):
    return any(e.get("type") == "step_start" and e.get("orch_step_id") == sid for e in events)


def _if_else(condition):
    return S.make_orchestration(id="orch_f850_if", entry_step_id="gate", steps=[
        {"id": "gate", "name": "Gate", "type": "if_else", "if_condition": condition,
         "if_true_step_id": "yes", "if_false_step_id": "no", "next_step_id": None},
        _print("yes"), _print("no"),
    ])


def _switch(expression):
    return S.make_orchestration(id="orch_f850_switch", entry_step_id="route", steps=[
        {"id": "route", "name": "Route", "type": "switch", "switch_expression": expression,
         "switch_cases": {"urgent": "urgent", "routine": "routine"}, "switch_default_step_id": "routine",
         "next_step_id": None},
        _print("urgent"), _print("routine"),
    ])


class TestARefusedExpression:
    async def test_an_if_else_fails_and_takes_neither_branch(self):
        events = await _run(_if_else("str(state.classification).zfill(9) == ''"),
                            initial_state={"classification": "urgent"})

        assert _status(events) == "failed"
        assert "If/Else condition" in _reason(events) and REFUSED in _reason(events)
        assert not any(e.get("type") == "if_decision" for e in events)
        assert not _reached(events, "yes") and not _reached(events, "no")

    async def test_a_switch_fails_and_never_takes_its_default(self):
        """QA1b's own expression."""
        events = await _run(_switch(SPLIT), initial_state={"classification": "Sort\nurgent"})

        assert _status(events) == "failed"
        assert "Switch expression" in _reason(events) and REFUSED in _reason(events)
        assert not any(e.get("type") == "switch_decision" for e in events)
        assert not _reached(events, "routine")

    @pytest.mark.parametrize(("field", "expr", "words"), [
        ("loop_over", "state.items.zfill(9)", "Loop's list"),
        ("loop_while_condition", "state.items.zfill(9) != ''", "Loop's while condition"),
        ("loop_break_condition", "state.items.zfill(9) == ''", "Loop's break condition"),
    ])
    async def test_a_loop_fails_on_any_of_its_three(self, field, expr, words):
        loop = {"id": "loop1", "name": "Each", "type": "loop", "loop_step_ids": ["body"],
                "next_step_id": "after", "loop_max_iterations": 3, field: expr}
        if field == "loop_break_condition":
            loop["loop_count"] = 2
        orch = S.make_orchestration(id=f"orch_f850_{field}", entry_step_id="loop1", steps=[
            loop, {**_print("body"), "output_key": "seen"}, _print("after")])
        events = await _run(orch, initial_state={"items": "a\nb"})

        assert _status(events) == "failed"
        assert words in _reason(events) and REFUSED in _reason(events)
        assert not _reached(events, "after")


class TestAnExpressionThatCannotReadTheState:
    """Allowed, but this run's state has no such key: the author's branch for it still runs."""

    async def test_an_if_else_still_routes_false(self):
        events = await _run(_if_else("state.missing.deep.attr == 1"))

        assert _status(events) == "completed"
        assert [e["result"] for e in events if e.get("type") == "if_decision"] == ["false"]
        assert _reached(events, "no")

    async def test_a_switch_still_takes_its_default(self):
        events = await _run(_switch("state.missing.deep.attr"))

        assert _status(events) == "completed"
        assert _reached(events, "routine")


class TestTheSaveRules:
    def test_a_refused_expression_is_named_with_its_step_and_field(self):
        from core.orchestration.save_rules import refusal

        said = refusal({"id": "o1", "steps": [{"id": "step_route", "type": "switch", "switch_expression": SPLIT}]},
                       {})
        assert said is not None and said.startswith("Refusing to save:")
        assert f"step 'step_route' switch_expression: {REFUSED}" in said

    def test_an_agent_step_on_its_own_workflow_agent_is_refused_in_f691s_words(self):
        from core.orchestration.save_rules import refusal

        agents = {"agent_desk": {"id": "agent_desk", "type": "orchestrator", "orchestration_id": "o1"},
                  "agent_other": {"id": "agent_other", "type": "orchestrator", "orchestration_id": "o2"},
                  "agent_plain": {"id": "agent_plain", "type": "conversational"}}
        said = refusal({"id": "o1", "steps": [{"id": "step_research", "type": "agent", "agent_id": "agent_desk"}]},
                       agents)
        assert said == (
            "Refusing to save: Agent step 'step_research' uses workflow agent 'agent_desk', which runs this "
            "workflow: every run would call itself again, without end. Point the step at the agent that does the "
            "work, not at a workflow agent of this workflow.")
        for other in ("agent_other", "agent_plain", "agent_gone"):
            assert refusal({"id": "o1", "steps": [{"id": "s", "type": "agent", "agent_id": other}]}, agents) is None

    def test_a_workflow_step_on_its_own_workflow_is_refused(self):
        from core.orchestration.save_rules import refusal

        said = refusal({"id": "o1", "steps": [{"id": "step_again", "type": "workflow", "workflow_id": "o1"}]}, {})
        assert said is not None and "Workflow step 'step_again' runs this workflow" in said
        assert refusal({"id": "o1", "steps": [{"id": "s", "type": "workflow", "workflow_id": "o2"}]}, {}) is None

    def test_every_workflow_this_product_ships_passes(self):
        """Synapse AI's own definition and every example pack: a later edit that slips in an expression the
        engine refuses would fail that step on every run (SAI2's condition, 2026-10-23)."""
        import json
        from pathlib import Path

        from core.orchestration.save_rules import refusal

        backend = Path(__file__).resolve().parents[2]
        shipped = {"native_builder": json.loads((backend / "core/native_builder/orchestration.json").read_text())}
        for pack in sorted((backend / "examples").glob("*.bundle.json")):
            for orch in json.loads(pack.read_text()).get("orchestrations") or []:
                shipped[f"{pack.name}:{orch.get('id')}"] = orch
        assert len(shipped) > 5
        assert {name: said for name, orch in shipped.items() if (said := refusal(orch, {}))} == {}

    def test_an_expression_that_is_only_a_typo_is_refused_naming_step_and_field(self):
        """A typo is refused like a refused expression (F1433, Naveen 11-06): at run time it now fails its step, so
        it is never saved; it was the validator's to report while the engine routed it as False."""
        from core.orchestration.save_rules import refusal

        said = refusal({"id": "o1", "steps": [{"id": "s", "type": "if_else", "if_condition": "state.x =="}]}, {})
        assert said and said.startswith("Refusing to save:") and "step 's' if_condition: invalid syntax" in said, said
