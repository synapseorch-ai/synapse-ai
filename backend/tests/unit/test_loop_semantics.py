"""Loop node semantics: for-each / while / break, iteration context, accounting.

Crash recovery lives in test_loop_parallel_checkpoint.py; this file is about
what the loop *means*. Bodies are `print` or `transform` steps, so nothing here
needs an LLM.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _engine(orch_dict):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    return OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())


async def _run(orch_dict, initial_state=None, run_id="run_loopsem", engine=None):
    engine = engine or _engine(orch_dict)
    return [ev async for ev in engine.run("go", run_id=run_id, initial_state=initial_state)]


def _host_transforms(monkeypatch):
    """Run transform steps in the host subprocess runtime — no Docker needed."""
    import core.config as config
    monkeypatch.setattr(config, "load_settings",
                        lambda: {"transform_runtime": "host", "model": "mistral"})


def _loop_complete(events):
    return next(e for e in events if e.get("type") == "loop_complete")


def _iterations(events):
    return [e["iteration"] for e in events if e.get("type") == "loop_iteration"]


def _final_state(events):
    return next(e for e in reversed(events)
                if e.get("type") == "orchestration_complete")["final_state"]


def _orch(loop_fields, body=None, after=True, **orch_kw):
    """A loop over one `print` body step, optionally followed by a plain step."""
    steps = [
        {"id": "loop1", "name": "Loop", "type": "loop",
         "loop_step_ids": ["body"], "next_step_id": "after" if after else None,
         **loop_fields},
        body or {"id": "body", "name": "Body", "type": "print",
                 "print_content": "tick", "output_key": "tick_out",
                 "next_step_id": None},
    ]
    if after:
        steps.append({"id": "after", "name": "After", "type": "print",
                      "print_content": "done", "output_key": "tail",
                      "next_step_id": None})
    return S.make_orchestration(id="orch_loopsem", entry_step_id="loop1",
                                steps=steps, **orch_kw)


class TestForEach:
    async def test_iterates_a_state_collection(self):
        orch = _orch(
            {"loop_over": "state.items"},
            body={"id": "body", "name": "Body", "type": "print",
                  "print_content": "{state.loop_index}:{state.loop_item}",
                  "output_key": "seen", "next_step_id": None},
        )
        events = await _run(orch, initial_state={"items": ["a", "b", "c"]})

        assert _iterations(events) == [1, 2, 3]
        state = _final_state(events)
        assert [e["result"] for e in state["seen"]] == ["0:a", "1:b", "2:c"]
        assert _loop_complete(events)["stopped_reason"] == "collection_exhausted"

    async def test_item_and_index_keys_are_configurable(self):
        orch = _orch(
            {"loop_over": "state.items", "loop_item_key": "ticket",
             "loop_index_key": "pos"},
            body={"id": "body", "name": "Body", "type": "print",
                  "print_content": "{state.pos}={state.ticket}",
                  "output_key": "seen", "next_step_id": None},
        )
        events = await _run(orch, initial_state={"items": ["x", "y"]})
        state = _final_state(events)
        assert [e["result"] for e in state["seen"]] == ["0=x", "1=y"]

    async def test_empty_collection_runs_zero_iterations(self):
        events = await _run(_orch({"loop_over": "state.items"}),
                            initial_state={"items": []})
        assert _iterations(events) == []
        assert _loop_complete(events)["iterations_completed"] == 0
        # the run still moves on down the done path
        assert _final_state(events)["tail"] == "done"

    async def test_non_list_source_warns_and_runs_zero_iterations(self):
        events = await _run(_orch({"loop_over": "state.items"}),
                            initial_state={"items": "not a list"})
        warnings = [e["message"] for e in events if e.get("type") == "step_warning"]
        assert any("not a list" in w for w in warnings)
        assert _iterations(events) == []
        assert _final_state(events)["tail"] == "done"

    async def test_a_source_written_as_json_text_is_typed_and_iterates(self):
        """F14, the headline: the upstream step answers in text, the loop runs.

        This is Naveen's run. An llm step returned the perfectly good JSON text
        `["…", "…"]` into a key declared `list`; the loop got a `str`, warned
        "not a list — zero iterations", and the rest of the orchestration ran on
        an empty result. A `print` body stands in for the llm here.
        """
        orch = _orch({"loop_over": "state.items"}, after=True)
        orch["steps"].insert(0, {
            "id": "writer", "name": "Writer", "type": "print",
            "print_content": '["a", "b"]', "output_key": "items",
            "next_step_id": "loop1",
        })
        orch["entry_step_id"] = "writer"
        orch["state_schema"] = {"items": {"type": "list", "default": []}}

        events = await _run(orch)
        assert _iterations(events) == [1, 2]
        assert _loop_complete(events)["iterations_completed"] == 2
        assert not any("not a list" in e.get("message", "") for e in events)

    async def test_empty_array_text_types_to_an_empty_list_and_still_runs_nothing(self):
        """The caveat, pinned so nobody later reads it as a regression.

        In the screenshot that reopened F14 the model had answered with the
        literal text `[]` after a 500-retry. Typing turns that into an empty
        list, which is honest — and the loop still runs zero times, because an
        empty collection is genuinely empty. F14 removes the type error, not
        that run's empty result.
        """
        orch = _orch({"loop_over": "state.items"}, after=True)
        orch["steps"].insert(0, {
            "id": "writer", "name": "Writer", "type": "print",
            "print_content": "[]", "output_key": "items", "next_step_id": "loop1",
        })
        orch["entry_step_id"] = "writer"
        orch["state_schema"] = {"items": {"type": "list", "default": []}}

        events = await _run(orch)
        assert _iterations(events) == []
        assert _final_state(events)["items"] == []
        # and the misleading line is gone — it was never the real problem
        assert not any("not a list" in e.get("message", "") for e in events)

    async def test_missing_source_warns_rather_than_crashing(self):
        events = await _run(_orch({"loop_over": "state.nope"}))
        assert _iterations(events) == []
        assert any(e.get("type") == "orchestration_complete" for e in events)

    async def test_collection_is_capped_by_loop_max_iterations(self):
        events = await _run(
            _orch({"loop_over": "state.items", "loop_max_iterations": 2}),
            initial_state={"items": [1, 2, 3, 4, 5]},
        )
        assert _iterations(events) == [1, 2]
        assert _loop_complete(events)["stopped_reason"] == "collection_exhausted"


class TestWhile:
    async def test_loops_until_the_condition_flips(self, monkeypatch):
        _host_transforms(monkeypatch)
        orch = _orch(
            {"loop_while_condition": "(state.n or 0) < 3"},
            body={"id": "body", "name": "Body", "type": "transform",
                  "transform_code": "result = (state.get('n') or 0) + 1",
                  "output_key": "n", "timeout_seconds": 30, "next_step_id": None},
        )
        events = await _run(orch)
        assert _iterations(events) == [1, 2, 3]
        assert _loop_complete(events)["stopped_reason"] == "condition_false"

    async def test_false_from_the_start_runs_nothing(self):
        events = await _run(_orch({"loop_while_condition": "False"}))
        assert _iterations(events) == []
        assert _loop_complete(events)["iterations_completed"] == 0

    async def test_a_runaway_condition_is_capped(self):
        events = await _run(
            _orch({"loop_while_condition": "True", "loop_max_iterations": 4}))
        assert _iterations(events) == [1, 2, 3, 4]
        lc = _loop_complete(events)
        assert lc["stopped_reason"] == "max_iterations"
        assert lc["iterations_completed"] == 4

    async def test_a_broken_condition_stops_the_loop_with_a_warning(self):
        events = await _run(_orch({"loop_while_condition": "state.a.b.c ==="}))
        assert _iterations(events) == []
        assert any("loop_while_condition" in e.get("message", "")
                   for e in events if e.get("type") == "step_warning")


class TestBreak:
    async def test_break_stops_mid_collection(self):
        orch = _orch(
            {"loop_over": "state.items", "loop_break_condition": "state.seen == 'b'"},
            body={"id": "body", "name": "Body", "type": "print",
                  "print_content": "{state.loop_item}", "output_key": "seen",
                  "next_step_id": None},
        )
        events = await _run(orch, initial_state={"items": ["a", "b", "c", "d"]})
        assert _iterations(events) == [1, 2]
        lc = _loop_complete(events)
        assert lc["stopped_reason"] == "break"
        assert lc["iterations_completed"] == 2
        assert [e["result"] for e in _final_state(events)["seen"]] == ["a", "b"]

    async def test_break_applies_to_a_fixed_count_loop_too(self):
        orch = _orch(
            {"loop_count": 5, "loop_break_condition": "state.seen == 'tick'"},
            body={"id": "body", "name": "Body", "type": "print",
                  "print_content": "tick", "output_key": "seen",
                  "next_step_id": None},
        )
        events = await _run(orch)
        assert _iterations(events) == [1]
        assert _loop_complete(events)["stopped_reason"] == "break"


class TestAccounting:
    async def test_a_failing_body_step_contributes_no_result(self, monkeypatch):
        """A step that fails every iteration must not have whatever its output
        key already held recorded as that iteration's result."""
        _host_transforms(monkeypatch)
        orch = _orch(
            {"loop_count": 3},
            body={"id": "body", "name": "Body", "type": "transform",
                  "transform_code": "raise ValueError('boom')",
                  "output_key": "risky", "timeout_seconds": 30, "next_step_id": None},
        )
        events = await _run(orch, initial_state={"risky": "STALE"})

        assert _iterations(events) == [1, 2, 3]
        assert any(e.get("type") == "step_error" for e in events)
        state = _final_state(events)
        # Not three copies of "STALE" dressed up as iteration results.
        assert state["risky"] == "STALE"

    async def test_a_timing_out_body_step_also_contributes_nothing(self, monkeypatch):
        _host_transforms(monkeypatch)
        orch = _orch(
            {"loop_count": 2},
            body={"id": "body", "name": "Body", "type": "transform",
                  "transform_code": "import time; time.sleep(5); result = 'never'",
                  "output_key": "risky", "timeout_seconds": 1, "next_step_id": None},
            after=False,
        )
        events = await _run(orch, initial_state={"risky": "STALE"})
        assert _iterations(events) == [1, 2]
        assert _final_state(events)["risky"] == "STALE"

    async def test_loop_summary_omits_steps_that_produced_nothing(self, monkeypatch):
        _host_transforms(monkeypatch)
        orch = S.make_orchestration(
            id="orch_loopsum", entry_step_id="loop1",
            steps=[
                {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
                 "loop_step_ids": ["good", "bad"], "output_key": "summary",
                 "next_step_id": None},
                {"id": "good", "name": "Good", "type": "print",
                 "print_content": "ok", "output_key": "good_out", "next_step_id": None},
                {"id": "bad", "name": "Bad", "type": "transform",
                 "transform_code": "raise ValueError('boom')",
                 "output_key": "bad_out", "timeout_seconds": 30, "next_step_id": None},
            ],
        )
        events = await _run(orch, initial_state={"bad_out": "STALE"})
        summary = _final_state(events)["summary"]
        assert "Good" in summary
        assert "Bad" not in summary

    async def test_iterations_completed_is_the_real_count(self):
        events = await _run(_orch({"loop_count": 3}),
                            initial_state={"_loop_progress_loop1":
                                           {"completed_iterations": 1}})
        assert _iterations(events) == [2, 3]
        assert _loop_complete(events)["iterations_completed"] == 3

    async def test_loop_bookkeeping_never_reaches_final_state(self):
        events = await _run(_orch({"loop_over": "state.items"}),
                            initial_state={"items": ["a", "b"]})
        state = _final_state(events)
        assert [k for k in state if k.startswith("_loop_")] == []


class TestIterationContext:
    """The loop must tell a body step where it is — and must not tell the step
    after the loop that it is inside one."""

    def _capture(self, orch):
        engine = _engine(orch)
        seen: list[tuple] = []

        async def go():
            async for ev in engine.run("go", run_id="run_loopctx"):
                if ev.get("type") == "step_start":
                    t = engine.current_transition
                    seen.append((ev.get("orch_step_id"), t.origin_type,
                                 t.loop_iteration, t.loop_total))
        return engine, seen, go

    async def test_body_steps_see_their_iteration(self):
        engine, seen, go = self._capture(_orch({"loop_count": 2}))
        await go()
        body = [s for s in seen if s[0] == "body"]
        assert body == [("body", "loop", 1, 2), ("body", "loop", 2, 2)]

    async def test_the_step_after_the_loop_is_a_linear_transition(self):
        engine, seen, go = self._capture(_orch({"loop_count": 2}))
        await go()
        after = [s for s in seen if s[0] == "after"]
        assert after == [("after", "linear", None, None)]

    async def test_shared_state_markers_are_visible_to_the_body(self):
        orch = _orch(
            {"loop_count": 2},
            body={"id": "body", "name": "Body", "type": "print",
                  "print_content": "{state._loop_current_iteration}/{state._loop_total}",
                  "output_key": "seen", "next_step_id": None},
        )
        events = await _run(orch)
        assert [e["result"] for e in _final_state(events)["seen"]] == ["1/2", "2/2"]


class TestTimeoutBudget:
    async def test_a_loop_outlives_a_single_step_budget(self, monkeypatch):
        """Pre-fix the engine gave the whole loop one step's worth of time, so a
        loop doing more work than that died mid-iteration."""
        _host_transforms(monkeypatch)
        orch = _orch(
            {"loop_count": 3, "timeout_seconds": 1},
            body={"id": "body", "name": "Body", "type": "transform",
                  "transform_code": "import time; time.sleep(0.6); result = 'ok'",
                  "output_key": "out", "timeout_seconds": 30, "next_step_id": None},
            after=False,
        )
        events = await _run(orch)
        assert _iterations(events) == [1, 2, 3]
        assert not [e for e in events if e.get("type") == "orchestration_error"]
        assert len(_final_state(events)["out"]) == 3

    async def test_body_steps_keep_their_own_timeout(self, monkeypatch):
        _host_transforms(monkeypatch)
        orch = _orch(
            {"loop_count": 1},
            body={"id": "body", "name": "Body", "type": "transform",
                  "transform_code": "import time; time.sleep(5); result = 'never'",
                  "output_key": "out", "timeout_seconds": 1, "next_step_id": None},
            after=False,
        )
        events = await _run(orch)
        assert any("timed out" in e.get("error", "")
                   for e in events if e.get("type") == "step_error")


class TestNesting:
    async def test_a_self_referencing_loop_is_refused(self):
        orch = S.make_orchestration(
            id="orch_selfloop", entry_step_id="loop1",
            steps=[
                {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
                 "loop_step_ids": ["loop1"], "next_step_id": None},
            ],
        )
        events = await _run(orch)
        assert any("nesting" in e.get("error", "").lower()
                   for e in events if e.get("type") == "step_error")

    async def test_a_nested_loop_restores_its_parent_context(self):
        orch = S.make_orchestration(
            id="orch_nestloop", entry_step_id="outer",
            steps=[
                {"id": "outer", "name": "Outer", "type": "loop", "loop_count": 2,
                 "loop_step_ids": ["inner"], "next_step_id": None},
                {"id": "inner", "name": "Inner", "type": "loop", "loop_count": 2,
                 "loop_step_ids": ["body"], "next_step_id": None},
                {"id": "body", "name": "Body", "type": "print",
                 "print_content": "{state._loop_current_iteration}",
                 "output_key": "seen", "next_step_id": None},
            ],
        )
        events = await _run(orch)
        # inner runs twice per outer iteration
        assert len([e for e in events if e.get("type") == "step_start"
                    and e.get("orch_step_id") == "body"]) == 4
        assert [k for k in _final_state(events) if k.startswith("_loop_")] == []


class TestLoopValidation:
    """The wiring checks the AI builder and the canvas both run before a save."""

    @staticmethod
    def _issues(loop_fields, extra_steps=()):
        from core.builder_tools import _validate_orchestration
        orch = {
            "id": "o", "entry_step_id": "loop1",
            "steps": [
                {"id": "loop1", "name": "Loop", "type": "loop",
                 "loop_step_ids": ["body"], "next_step_id": "done", **loop_fields},
                {"id": "body", "name": "Body", "type": "print", "print_content": "x",
                 "output_key": "o1", "next_step_id": None},
                {"id": "done", "name": "Done", "type": "end"},
                *extra_steps,
            ],
        }
        return _validate_orchestration(orch)["issues"]

    def test_a_loop_that_contains_itself_is_an_issue(self):
        from core.builder_tools import _validate_orchestration
        orch = {
            "id": "o", "entry_step_id": "loop1",
            "steps": [
                {"id": "loop1", "name": "Loop", "type": "loop",
                 "loop_step_ids": ["loop1"], "next_step_id": "done"},
                {"id": "done", "name": "Done", "type": "end"},
            ],
        }
        issues = _validate_orchestration(orch)["issues"]
        assert any("lists itself" in i for i in issues)

    def test_loop_count_above_the_ceiling_is_flagged(self):
        issues = self._issues({"loop_count": 100, "loop_max_iterations": 10})
        assert any("exceeds its" in i and "loop_max_iterations" in i for i in issues)

    def test_a_for_each_loop_is_not_judged_on_loop_count(self):
        issues = self._issues({"loop_over": "state.items", "loop_count": 100,
                               "loop_max_iterations": 10})
        assert not any("loop_count" in i for i in issues)

    def test_a_routing_step_in_the_body_is_flagged(self):
        from core.builder_tools import _validate_orchestration
        orch = {
            "id": "o", "entry_step_id": "loop1",
            "steps": [
                {"id": "loop1", "name": "Loop", "type": "loop",
                 "loop_step_ids": ["branch"], "next_step_id": "done"},
                {"id": "branch", "name": "Branch", "type": "if_else",
                 "if_condition": "True", "if_true_step_id": "done",
                 "if_false_step_id": "done"},
                {"id": "done", "name": "Done", "type": "end"},
            ],
        }
        issues = _validate_orchestration(orch)["issues"]
        assert any("routing is ignored inside a loop body" in i for i in issues)

    def test_a_human_step_in_the_body_is_flagged(self):
        from core.builder_tools import _validate_orchestration
        orch = {
            "id": "o", "entry_step_id": "loop1",
            "steps": [
                {"id": "loop1", "name": "Loop", "type": "loop",
                 "loop_step_ids": ["ask"], "next_step_id": "done"},
                {"id": "ask", "name": "Ask", "type": "human",
                 "human_prompt": "ok?", "next_step_id": None},
                {"id": "done", "name": "Done", "type": "end"},
            ],
        }
        issues = _validate_orchestration(orch)["issues"]
        assert any("dropping the remaining iterations" in i for i in issues)

    def test_a_body_step_chaining_outside_the_body_is_flagged(self):
        from core.builder_tools import _validate_orchestration
        orch = {
            "id": "o", "entry_step_id": "loop1",
            "steps": [
                {"id": "loop1", "name": "Loop", "type": "loop",
                 "loop_step_ids": ["b1"], "next_step_id": "done"},
                {"id": "b1", "name": "B1", "type": "print", "print_content": "x",
                 "output_key": "o1", "next_step_id": "b2"},
                {"id": "b2", "name": "B2", "type": "print", "print_content": "y",
                 "output_key": "o2", "next_step_id": "done"},
                {"id": "done", "name": "Done", "type": "end"},
            ],
        }
        issues = _validate_orchestration(orch)["issues"]
        assert any("only the steps listed in loop_step_ids" in i for i in issues)

    def test_a_well_formed_loop_raises_nothing_about_the_loop(self):
        issues = self._issues({"loop_count": 3})
        assert not any("Loop" in i or "loop" in i for i in issues), issues


class TestTurnGuard:
    async def test_a_resume_after_a_large_loop_does_not_trip_max_total_turns(self):
        """`_execute_loop` used to seed its turn counter from the raw history
        length. A loop's body steps are in that history, so a for-each over 150
        items that paused at a human step came back from resume to an instant
        "Global turn limit exceeded" — with the loop having counted as one turn
        while it was actually running."""
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(
            id="orch_turnguard", entry_step_id="loop1", max_total_turns=100,
            steps=[
                {"id": "loop1", "name": "Loop", "type": "loop",
                 "loop_over": "state.items", "loop_max_iterations": 500,
                 "loop_step_ids": ["body"], "next_step_id": "ask"},
                {"id": "body", "name": "Body", "type": "print", "print_content": "x",
                 "output_key": "o", "next_step_id": None},
                {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?",
                 "output_key": "ans", "next_step_id": "tail"},
                {"id": "tail", "name": "Tail", "type": "print", "print_content": "done",
                 "output_key": "t", "next_step_id": None},
            ],
        )
        await S.seed_orchestrations([orch])

        events = await _run(orch, run_id="run_turnguard",
                            initial_state={"items": list(range(150))})
        assert events[-1]["type"] == "human_input_required"
        assert _iterations(events)[-1] == 150

        resumed = [e async for e in
                   OrchestrationEngine.resume("run_turnguard", {"ans": "yes"}, _server())]
        errors = [e["error"] for e in resumed if e.get("type") == "orchestration_error"]
        assert errors == []
        assert _final_state(resumed)["t"] == "done"

    async def test_graph_cycles_are_still_bounded(self):
        """The fix must not loosen the guard for what it is actually for."""
        orch = S.make_orchestration(
            id="orch_cycle", entry_step_id="a", max_total_turns=10,
            steps=[
                {"id": "a", "name": "A", "type": "print", "print_content": "a",
                 "output_key": "oa", "next_step_id": "b", "max_iterations": 1000},
                {"id": "b", "name": "B", "type": "print", "print_content": "b",
                 "output_key": "ob", "next_step_id": "a", "max_iterations": 1000},
            ],
        )
        events = await _run(orch, run_id="run_cycle")
        errors = [e["error"] for e in events if e.get("type") == "orchestration_error"]
        assert any("turn limit" in e for e in errors)
