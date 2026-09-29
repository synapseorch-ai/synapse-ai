"""
A parallel step's branches run at the same time.

Branches of llm / transform / print / extract_json / if_else / switch / merge
steps overlap, and since F255 so do agent and tool branches
(`test_parallel_agents_run_together.py`). A branch that could stop to ask a
person still runs alone, afterwards. The fake LLM sleeps for a fixed 0.3s here,
so "at the same time" is a wall-clock fact.
"""
import asyncio
import time
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _llm(sid):
    return {"id": sid, "name": sid.upper(), "type": "llm", "prompt_template": f"angle {sid}",
            "output_key": f"o_{sid}", "model": "claude-test", "next_step_id": None}


def _fanout(branches, tail=None):
    steps = [{"id": "fan", "name": "Fan", "type": "parallel",
              "parallel_branches": [[b["id"]] for b in branches],
              "next_step_id": "join"}, *branches,
             {"id": "join", "name": "Join", "type": "merge", "merge_strategy": "dict",
              "input_keys": [b["output_key"] for b in branches], "output_key": "joined",
              "next_step_id": None}]
    if tail:
        steps.extend(tail)
    return S.make_orchestration(id="orch_par", entry_step_id="fan", steps=steps)


async def _run(orch_dict, run_id="run_par", initial_state=None, engine=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = engine or OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    return [ev async for ev in engine.run("go", run_id=run_id, initial_state=initial_state)]


def _final(events):
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["final_state"]


@pytest.fixture
def slow_llm(fake_llm, monkeypatch):
    for k, v in (("MIN", "0.3"), ("MAX", "0.3"), ("PROB", "1")):
        monkeypatch.setenv(f"SYNAPSE_FAKE_LLM_DELAY_{k}", v)
    fake_llm.set_default("branch output")
    return fake_llm


class TestToolFreeBranchesOverlap:
    async def test_three_llm_branches_take_the_max_not_the_sum(self, slow_llm):
        t0 = time.perf_counter()
        events = await _run(_fanout([_llm("a"), _llm("b"), _llm("c")]))
        elapsed = time.perf_counter() - t0

        assert any(e.get("type") == "parallel_complete" for e in events)
        assert elapsed < 0.7, f"{elapsed:.2f}s — branches ran one after another"
        state = _final(events)
        assert all(state[f"o_{s}"] == "branch output" for s in "abc")
        assert set(state["joined"]) and len(state["joined"]) == 3

    async def test_every_event_is_attributed_and_the_node_completes_last(self, slow_llm):
        events = await _run(_fanout([_llm("a"), _llm("b"), _llm("c")]))
        # The executor's own last word is parallel_complete: every branch
        # event precedes it. (The engine then records the node itself.)
        types_ = [e.get("type") for e in events]
        done = types_.index("parallel_complete")
        last_branch_event = max(i for i, e in enumerate(events) if e.get("orch_step_id") in {"a", "b", "c"})
        assert last_branch_event < done
        starts = [e["branch_index"] for e in events if e.get("type") == "branch_start"]
        assert sorted(starts) == [0, 1, 2]
        for sid in "abc":
            types_ = [e["type"] for e in events if e.get("orch_step_id") == sid]
            assert "step_start" in types_ and "step_complete" in types_

    async def test_the_kill_switch_restores_sequential(self, slow_llm, monkeypatch):
        import core.config as config
        monkeypatch.setattr(config, "PARALLEL_BRANCHES_CONCURRENT", False)
        t0 = time.perf_counter()
        events = await _run(_fanout([_llm("a"), _llm("b"), _llm("c")]))
        elapsed = time.perf_counter() - t0
        assert elapsed >= 0.85
        # Strictly one branch at a time: every branch_start follows the
        # previous branch's step_complete.
        order = [(e["type"], e.get("branch_index"), e.get("orch_step_id"))
                 for e in events if e.get("type") == "branch_start"
                 or (e.get("type") == "step_complete" and e.get("orch_step_id") in {"a", "b", "c"})]
        assert order == [("branch_start", 0, "fan"), ("step_complete", None, "a"),
                         ("branch_start", 1, "fan"), ("step_complete", None, "b"),
                         ("branch_start", 2, "fan"), ("step_complete", None, "c")]


class TestABranchThatCanAskStaysSequential:
    async def test_an_agent_branch_runs_after_the_overlapping_ones(self, slow_llm, monkeypatch):
        """An agent that keeps memory can stop to ask (a workflow's `remember`
        asks first), and a run waits on one question at a time. Any agent ran
        here until F255; a plain one now runs beside the others."""
        import core.react_engine as re_mod
        timeline: list[tuple[str, float]] = []

        async def agent(**kwargs):
            timeline.append(("agent_start", time.perf_counter()))
            yield {"type": "final", "response": "agent done"}

        monkeypatch.setattr(re_mod, "run_agent_step", agent)
        await S.seed_agents([S.make_agent(id="a1", memory=True)])
        agent_step = {"id": "ag", "name": "Agent", "type": "agent", "agent_id": "a1",
                      "prompt_template": "x", "output_key": "o_ag", "next_step_id": None}
        orch = _fanout([_llm("a"), agent_step, _llm("b")])

        t0 = time.perf_counter()
        events = await _run(orch)

        finished_llms = [e for e in events if e.get("type") == "step_complete" and e.get("orch_step_id") in {"a", "b"}]
        assert len(finished_llms) == 2
        agent_started = timeline[0][1] - t0
        assert agent_started >= 0.28, "the agent branch started before the llm branches were done"
        state = _final(events)
        assert state["o_ag"] == "agent done" and state["o_a"] == state["o_b"] == "branch output"


class _FlakyLLM:
    """The real LLM executor, except that step `b` raises (while `fail` is set)."""

    def __init__(self):
        from core.models_orchestration import StepType
        from core.orchestration import steps as steps_mod
        self.real = steps_mod.STEP_EXECUTORS[StepType.LLM]
        self.fail = True

    async def execute(self, step, run, engine):
        if step.id == "b" and self.fail:
            raise RuntimeError("provider exploded")
        async for ev in self.real.execute(step, run, engine):
            yield ev


def _flaky_engine(orch, flaky):
    from core.models_orchestration import Orchestration, StepType
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    engine.executors = {**engine.executors, StepType.LLM: flaky}
    return engine


class TestAFailedBranchFailsTheRun:
    """F88 (Naveen, D112). A step raising in a concurrent branch used to be that
    branch's own problem — reported, and the run moved on with the merge working
    from what arrived — while the same step in a sequential branch failed the
    run. Both now fail it; the concurrent siblings still finish first, so their
    work is kept for a Replay, which reruns only the branch that failed."""

    async def test_the_others_finish_and_the_run_fails(self, slow_llm):
        orch = _fanout([_llm("a"), _llm("b"), _llm("c")])
        events = await _run(orch, engine=_flaky_engine(orch, _FlakyLLM()))

        errors = [e for e in events if e.get("type") == "step_error"]
        assert any("provider exploded" in e["error"] for e in errors)
        complete = next(e for e in events if e.get("type") == "orchestration_complete")
        assert complete["status"] == "failed"
        state = complete["final_state"]
        assert state["o_a"] == state["o_c"] == "branch output", "the siblings finished"
        assert "o_b" not in state
        assert "joined" not in state, "the merge does not run on what arrived"

    async def test_a_sequential_branch_does_not_start_after_one_failed(self, slow_llm, monkeypatch):
        import core.react_engine as re_mod
        started = []

        async def agent(**kwargs):
            started.append(kwargs.get("agent_id"))
            yield {"type": "final", "response": "agent done"}

        monkeypatch.setattr(re_mod, "run_agent_step", agent)
        # An agent that can ask runs in the sequential phase (F255).
        await S.seed_agents([S.make_agent(id="a1", memory=True)])
        agent_step = {"id": "ag", "name": "Agent", "type": "agent", "agent_id": "a1",
                      "prompt_template": "x", "output_key": "o_ag", "next_step_id": None}
        orch = _fanout([_llm("a"), _llm("b"), agent_step])
        events = await _run(orch, engine=_flaky_engine(orch, _FlakyLLM()))

        assert next(e for e in events if e.get("type") == "orchestration_complete")["status"] == "failed"
        assert started == [], "a branch started after the run had already failed"

    async def test_a_replay_reruns_only_the_branch_that_failed(self, slow_llm):
        from core.orchestration.engine import OrchestrationEngine
        orch = _fanout([_llm("a"), _llm("b"), _llm("c")])
        await S.seed_orchestrations([orch])
        flaky = _FlakyLLM()
        await _run(orch, run_id="run_par_replay", engine=_flaky_engine(orch, flaky))

        flaky.fail = False
        import core.orchestration.steps as steps_mod
        from core.models_orchestration import StepType
        real = steps_mod.STEP_EXECUTORS[StepType.LLM]
        steps_mod.STEP_EXECUTORS[StepType.LLM] = flaky
        try:
            events = [e async for e in OrchestrationEngine.resume_failed("run_par_replay", _server())]
        finally:
            steps_mod.STEP_EXECUTORS[StepType.LLM] = real

        started = [e["orch_step_id"] for e in events if e.get("type") == "step_start"
                   and e["orch_step_id"] in {"a", "b", "c"}]
        assert started == ["b"]
        complete = next(e for e in events if e.get("type") == "orchestration_complete")
        assert complete["status"] == "completed"
        assert len(complete["final_state"]["joined"]) == 3


class TestABranchStepThatReportsAnError:
    async def test_is_recorded_failed_not_completed(self):
        """A step that reports `step_error` without raising was recorded
        `completed` in a branch — where a loop body recorded it `failed` (F88)."""
        from core.models_orchestration import Orchestration, StepType
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        class Reports:
            async def execute(self, step, run, engine):
                yield {"type": "step_error", "orch_step_id": step.id, "error": "reported"}

        orch = S.make_orchestration(entry_step_id="par", steps=[
            {"id": "par", "name": "Par", "type": "parallel", "parallel_branches": [["bad"]],
             "next_step_id": None},
            {"id": "bad", "name": "Bad", "type": "tool", "forced_tool": "t", "output_key": "b",
             "next_step_id": None}])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.executors = {**engine.executors, StepType.TOOL: Reports()}
        [e async for e in engine.run("go", run_id="run_par_reported")]

        history = (await SharedState.restore("run_par_reported")).run.step_history
        assert [(h["step_id"], h["status"]) for h in history if h["step_id"] == "bad"] == [("bad", "failed")]


class TestCrashRecoveryStillHolds:
    async def test_only_the_unfinished_branch_reruns(self, slow_llm):
        events = await _run(
            _fanout([_llm("a"), _llm("b"), _llm("c")]),
            initial_state={"_parallel_progress_fan": {"completed_branches": [0, 2],
                                                      "completed_steps": ["a", "c"]},
                           "o_a": "from before", "o_c": "from before"},
        )
        started = [e["orch_step_id"] for e in events if e.get("type") == "step_start"
                   and e["orch_step_id"] in {"a", "b", "c"}]
        assert started == ["b"]
        state = _final(events)
        assert state["o_a"] == state["o_c"] == "from before" and state["o_b"] == "branch output"
        assert "_parallel_progress_fan" not in state

    async def test_concurrent_checkpoints_never_overlap(self, slow_llm, monkeypatch):
        import core.orchestration.state as state_mod
        backend = state_mod.get_state_backend()
        real = backend.save
        active = {"now": 0, "max": 0}

        async def spy(run, *a, **kw):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                await asyncio.sleep(0.01)
                return await real(run, *a, **kw)
            finally:
                active["now"] -= 1

        monkeypatch.setattr(backend, "save", spy)
        await _run(_fanout([_llm("a"), _llm("b"), _llm("c")]))
        assert active["max"] == 1


class TestTheNodeDoesNotLeakTasks:
    async def test_no_branch_task_outlives_the_step(self, slow_llm):
        before = {t for t in asyncio.all_tasks() if not t.done()}
        await _run(_fanout([_llm("a"), _llm("b"), _llm("c")]))
        await asyncio.sleep(0)
        after = {t for t in asyncio.all_tasks() if not t.done()} - before
        assert not [t for t in after if "_pump" in repr(t.get_coro())], after
