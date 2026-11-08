"""Two graph shapes that ended a run as if nothing was wrong (F256).

A Switch whose value matched no case, with no default path, ended the run
`completed` after the switch alone — no warning. Two Parallel branches that both
wrote `shared` left one value (the later writer's) and the Merge labelled it
with the other branch; nothing said a value was lost, and neither the editor nor
the API save flagged the shape.

Now the Switch fails the run, naming the value. Same-key branches fail at the
Parallel step before any branch runs, naming the branches and the key; the
builder's validator reports the shape, and the editor marks it (blocking Run,
not Save — Naveen's call: a half-built draft must always be saveable).
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch, initial_state=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [e async for e in engine.run("go", run_id=f"run_{orch['id']}", initial_state=initial_state)]


def _status(events):
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"]


def _started(events):
    return [e.get("orch_step_id") for e in events if e.get("type") == "step_start"]


async def _history(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run.step_history


def _print(sid, nxt=None, key=None):
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid,
            "output_key": key or f"o_{sid}", "next_step_id": nxt}


def _switch(orch_id, expression, cases, default=None):
    return S.make_orchestration(id=orch_id, entry_step_id="sw", steps=[
        {"id": "sw", "name": "Route by kind", "type": "switch", "switch_expression": expression,
         "switch_cases": cases, "switch_default_step_id": default},
        _print("cat"), _print("other"),
    ])


class TestASwitchThatMatchesNothing:
    async def test_with_no_default_fails_naming_the_value(self):
        events = await _run(_switch("orch_sw_none", "'zebra'", {"cat": "cat"}))

        assert _status(events) == "failed"
        error = next(e for e in events if e.get("type") == "step_error")
        assert error["orch_step_id"] == "sw"
        assert "'zebra'" in error["reason"] and "no default" in error["reason"]
        entry = (await _history("run_orch_sw_none"))[-1]
        assert entry["step_id"] == "sw" and entry["status"] == "failed"
        # No routing decision claiming the run went nowhere on purpose.
        assert not any(e.get("type") == "routing_decision" for e in events)

    async def test_an_empty_default_is_no_default(self):
        events = await _run(_switch("orch_sw_blank", "'zebra'", {"cat": "cat"}, default=""))
        assert _status(events) == "failed"

    async def test_with_a_default_takes_it(self):
        events = await _run(_switch("orch_sw_default", "'zebra'", {"cat": "cat"}, default="other"))
        assert _status(events) == "completed"
        assert "other" in _started(events)

    async def test_a_case_that_names_no_step_still_ends_the_run(self):
        """An explicit end is the author's choice, not a miss."""
        events = await _run(_switch("orch_sw_end", "'cat'", {"cat": None}))
        assert _status(events) == "completed"


def _fan(orch_id, branches, extra):
    return S.make_orchestration(id=orch_id, entry_step_id="fan", steps=[
        {"id": "fan", "name": "Fan out", "type": "parallel", "parallel_branches": branches, "next_step_id": "m"},
        *extra,
        {"id": "m", "name": "Merge", "type": "merge", "input_keys": ["shared"], "merge_strategy": "dict",
         "output_key": "merged", "next_step_id": None},
    ])


class TestBranchesThatWriteTheSameKey:
    async def test_fail_at_the_parallel_before_either_runs(self):
        orch = _fan("orch_samekey", [["t1"], ["t2"]], [
            {"id": "t1", "name": "T1", "type": "print", "print_content": "from branch ONE",
             "output_key": "shared", "next_step_id": "m"},
            {"id": "t2", "name": "T2", "type": "print", "print_content": "from branch TWO",
             "output_key": "shared", "next_step_id": "m"},
        ])
        events = await _run(orch)

        assert _status(events) == "failed"
        started = _started(events)
        assert "t1" not in started and "t2" not in started
        reason = next(e for e in events if e.get("type") == "step_error" and e.get("reason"))["reason"]
        assert "'shared'" in reason and "T1" in reason and "T2" in reason

    async def test_a_loop_inside_a_branch_counts_its_body(self):
        orch = _fan("orch_samekey_loop", [["lp"], ["t2"]], [
            {"id": "lp", "name": "Loop", "type": "loop", "loop_count": 2, "loop_step_ids": ["body"],
             "next_step_id": "m"},
            {"id": "body", "name": "Body", "type": "print", "print_content": "x", "output_key": "shared"},
            {"id": "t2", "name": "T2", "type": "print", "print_content": "y", "output_key": "shared",
             "next_step_id": "m"},
        ])
        assert _status(await _run(orch)) == "failed"

    async def test_a_branch_chain_is_followed(self):
        """A branch naming only its entry step runs the chain after it."""
        orch = _fan("orch_samekey_chain", [["a1"], ["b1"]], [
            _print("a1", "a2"), _print("a2", "m", key="shared"),
            _print("b1", "m", key="shared"),
        ])
        assert _status(await _run(orch)) == "failed"

    async def test_distinct_keys_run(self):
        orch = _fan("orch_distinct", [["t1"], ["t2"]], [
            _print("t1", "m", key="one"), _print("t2", "m", key="two"),
        ])
        orch["steps"][-1]["input_keys"] = ["one", "two"]
        assert _status(await _run(orch)) == "completed"


class TestTheValidatorsSayIt:
    def test_the_builders_validator(self):
        from core.builder_tools import _validate_orchestration

        orch = _fan("orch_v", [["t1"], ["t2"]], [
            _print("t1", "m", key="shared"), _print("t2", "m", key="shared"),
        ])
        issues = _validate_orchestration(orch)["issues"]
        assert any("'shared'" in i and "Fan out" in i for i in issues), issues

    def test_no_bundled_flow_has_the_shape(self):
        import glob
        import json
        import os

        from core.native_builder.seed import _load_bundled_orchestration
        from core.orchestration.branch_keys import same_key_branches

        flows = [_load_bundled_orchestration()]
        here = os.path.dirname(__file__)
        for path in glob.glob(os.path.join(here, "..", "..", "examples", "*.bundle.json")):
            with open(path, encoding="utf-8") as f:
                flows += json.load(f).get("orchestrations") or []
        for orch in flows:
            assert same_key_branches(orch) == [], orch.get("name")


@pytest.mark.parametrize("value, expected", [("zebra", "'zebra'"), (None, "None")])
async def test_the_value_is_named_as_evaluated(value, expected):
    orch = _switch("orch_sw_val", "state.kind", {"cat": "cat"})
    events = await _run(orch, initial_state={"kind": value})
    reason = next(e for e in events if e.get("type") == "step_error")["reason"]
    assert expected in reason
