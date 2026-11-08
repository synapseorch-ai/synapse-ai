"""A nested orchestration that fails fails the step that called it (F253).

The nested executor took the child's final text as its result whatever the
child's status was, so a child that failed handed its parent "Orchestration
failed. Error: … Traceback …" as data: the parent's step completed, the next
step used the traceback, and the run — and v1's answer — said `completed`.
Inside a Loop the child's forwarded `step_error` marked the iteration's step
failed and the loop carried on to the next item, so a loop whose every child
failed completed with nothing collected.

Now a child that did not complete fails the parent step with the child's
reason: at the top level, inside a Loop (which then stops), and when the child
fails after a person answered it. The same holds for a body step that times out
inside a Loop or a Parallel branch: it used to be skipped, and the step after
it ran on whatever state it had.

Each engine-level case runs in-process and through the fleet's jobs.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _print(sid, content=None, nxt=None, key=None):
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": content or sid,
            "output_key": key or f"o_{sid}", "next_step_id": nxt}


def _broken(sid="broken", nxt=None):
    """A step that always fails, with no model: a Tool step with no tool."""
    return {"id": sid, "name": "Broken tool", "type": "tool", "output_key": "never",
            "prompt_template": "x", "next_step_id": nxt}


def _human(sid, key="approval", nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "human", "human_prompt": "ok?",
            "output_key": key, "next_step_id": nxt}


def _delegate(agent_id, nxt=None, sid="delegate"):
    return {"id": sid, "name": "Deep dive", "type": "agent", "agent_id": agent_id,
            "prompt_template": "go", "output_key": "result", "next_step_id": nxt}


@pytest.fixture
def worker(monkeypatch):
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

    async def acquire():
        return _server()

    async def release():
        return None

    async def no_settings():
        return None

    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", release)
    monkeypatch.setattr(worker_mod, "_load_job_settings", no_settings)
    return worker_mod


async def _ctx(fake_redis) -> dict:
    from core.store import get_store
    return {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1, "job_id": "job-1"}


def _status_of(events):
    if events and events[-1].get("type") == "human_input_required":
        return "paused"
    for e in reversed(events):
        if e.get("type") == "orchestration_complete":
            return e.get("status", "completed")
    return "unknown"


class _Driver:
    def __init__(self, kind, worker_mod, fake_redis):
        self.kind, self._worker, self._redis = kind, worker_mod, fake_redis
        self.events: list[dict] = []

    async def start(self, orch, run_id, initial_state=None):
        if self.kind == "engine":
            from core.models_orchestration import Orchestration
            from core.orchestration.engine import OrchestrationEngine
            engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
            self.events = [e async for e in engine.run("go", run_id=run_id, initial_state=initial_state)]
            return _status_of(self.events)
        result = await self._worker.run_orchestration_job(
            await _ctx(self._redis), run_id=run_id, orch_id=orch["id"], initial_input="go",
            initial_state=initial_state)
        return result["status"]

    async def answer(self, run_id, response):
        if self.kind == "engine":
            from core.orchestration.engine import OrchestrationEngine
            self.events = [e async for e in OrchestrationEngine.resume(run_id, response, _server())]
            return _status_of(self.events)
        result = await self._worker.resume_orchestration_job(
            await _ctx(self._redis), run_id=run_id, human_response=response)
        return result["status"]


@pytest.fixture(params=["engine", "fleet"])
def driver(request, worker, fake_redis):
    return _Driver(request.param, worker, fake_redis)


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _entries(run, step_id):
    return [h for h in run.step_history if h.get("step_id") == step_id]


class TestAFailedChildFailsTheStep:
    async def test_at_the_top_level(self, driver):
        parent = S.make_orchestration(id="orch_f253", entry_step_id="delegate", steps=[
            _delegate("agent_f253", "tail"), _print("tail", "parent carried on with {state.result}"),
        ])
        child = S.make_orchestration(id="orch_f253_child", entry_step_id="broken", steps=[_broken()])
        await S.seed_agents([S.make_orchestrator_agent("orch_f253_child", id="agent_f253")])
        await S.seed_orchestrations([parent, child])

        assert await driver.start(parent, "run_f253") == "failed"

        run = await _run("run_f253")
        assert run.status == "failed"
        assert "o_tail" not in run.shared_state          # the next step never ran
        assert "result" not in run.shared_state           # the failure is not the step's output
        failed = _entries(run, "delegate")
        assert failed and failed[-1]["status"] == "failed"
        # The child's own reason, named on the step that called it.
        assert "Sub-orchestration" in failed[-1]["error"]
        assert "no tool configured" in failed[-1]["error"]

    async def test_inside_a_loop_the_loop_stops(self, driver):
        parent = S.make_orchestration(id="orch_f253l", entry_step_id="loop1", steps=[
            {"id": "loop1", "name": "Each competitor", "type": "loop", "loop_over": "state.items",
             "loop_step_ids": ["delegate"], "output_key": "dives", "next_step_id": "tail"},
            _delegate("agent_f253l"), _print("tail", "priced from {state.dives}"),
        ])
        child = S.make_orchestration(id="orch_f253l_child", entry_step_id="broken", steps=[_broken()])
        await S.seed_agents([S.make_orchestrator_agent("orch_f253l_child", id="agent_f253l")])
        await S.seed_orchestrations([parent, child])

        status = await driver.start(parent, "run_f253l", initial_state={"items": ["a", "b", "c"]})

        assert status == "failed"
        run = await _run("run_f253l")
        assert "o_tail" not in run.shared_state
        assert "dives" not in run.shared_state
        # The loop failed with the child's reason, where it used to carry on
        # and complete all three iterations with nothing collected.
        assert "no tool configured" in _entries(run, "loop1")[-1]["error"]
        if driver.kind == "engine":
            assert [e["iteration"] for e in driver.events if e.get("type") == "loop_iteration"] == [1]
            assert not any(e.get("type") == "loop_complete" for e in driver.events)

    async def test_after_a_person_answered_it(self, driver):
        parent = S.make_orchestration(id="orch_f253r", entry_step_id="delegate", steps=[
            _delegate("agent_f253r", "tail"), _print("tail"),
        ])
        child = S.make_orchestration(id="orch_f253r_child", entry_step_id="ask", steps=[
            _human("ask", nxt="broken"), _broken(),
        ])
        await S.seed_agents([S.make_orchestrator_agent("orch_f253r_child", id="agent_f253r")])
        await S.seed_orchestrations([parent, child])
        assert await driver.start(parent, "run_f253r") == "paused"

        assert await driver.answer("run_f253r", "yes") == "failed"

        run = await _run("run_f253r")
        assert run.status == "failed"
        assert run.nested_run_id is None and not run.waiting_for_human
        assert "o_tail" not in run.shared_state and "result" not in run.shared_state
        entries = _entries(run, "delegate")
        assert len(entries) == 1                          # its pause entry, closed — not a second
        assert entries[0]["status"] == "failed"
        assert "no tool configured" in entries[0]["error"]
        if driver.kind == "engine":
            errors = [e for e in driver.events if e.get("type") == "step_error"]
            assert any(e.get("orch_step_id") == "delegate" and "no tool configured" in (e.get("reason") or "")
                       for e in errors)


    async def test_a_recursion_past_the_depth_limit_names_the_limit(self, driver):
        """An orchestration calling itself: the fourth level is refused. That
        refusal used to come back up as three levels of text and a completed
        run; each level wrapping it in its own words would push the one
        sentence that matters past the engine's 300-character cap."""
        rec = S.make_orchestration(id="orch_f253rec", entry_step_id="delegate", steps=[
            _delegate("agent_f253rec", "tail"), _print("tail", "depth result: {state.result}"),
        ])
        await S.seed_agents([S.make_orchestrator_agent("orch_f253rec", id="agent_f253rec")])
        await S.seed_orchestrations([rec])

        assert await driver.start(rec, "run_f253rec") == "failed"

        error = _entries(await _run("run_f253rec"), "delegate")[-1]["error"]
        assert "depth limit exceeded" in error
        assert error.count("Sub-orchestration") == 1 and error.count(" › ") == 2, error


class TestV1SaysFailed:
    async def test_the_run_route_reports_the_failure(self, client, api_key):
        parent = S.make_orchestration(id="orch_f253v", entry_step_id="delegate", steps=[
            _delegate("agent_f253v", "tail"), _print("tail", "Parent carried on. dive = {state.result}"),
        ])
        child = S.make_orchestration(id="orch_f253v_child", entry_step_id="broken", steps=[_broken()])
        await S.seed_agents([S.make_orchestrator_agent("orch_f253v_child", id="agent_f253v")])
        await S.seed_orchestrations([parent, child])

        resp = await client.post("/api/v1/orchestrations/orch_f253v/run",
                                 json={"message": "go"}, headers=api_key["headers"])

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "failed"
        assert "Parent carried on" not in body["response"]
        assert "no tool configured" in body["response"]


class TestABodyStepThatTimesOutFailsTheRun:
    """It used to be recorded and skipped: the next body step ran on stale state."""

    def _slow(self, sid="slow"):
        return {"id": sid, "name": "Slow", "type": "llm", "prompt_template": "x",
                "output_key": "out", "timeout_seconds": 1, "next_step_id": None}

    async def _run(self, orch):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        return [e async for e in engine.run("go", run_id=f"run_{orch['id']}")]

    async def test_in_a_loop(self, gated_llm):
        orch = S.make_orchestration(id="orch_to_loop", entry_step_id="loop1", steps=[
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
             "loop_step_ids": ["slow", "after"], "next_step_id": "tail"},
            self._slow(), _print("after", "used {state.out}"), _print("tail"),
        ])
        events = await self._run(orch)

        assert _status_of(events) == "failed"
        assert any(e.get("type") == "step_error" and e.get("orch_step_id") == "slow" for e in events)
        started = [e.get("orch_step_id") for e in events if e.get("type") == "step_start"]
        assert "after" not in started and "tail" not in started
        assert [e["iteration"] for e in events if e.get("type") == "loop_iteration"] == [1]

    async def test_in_a_parallel_branch(self, gated_llm):
        orch = S.make_orchestration(id="orch_to_par", entry_step_id="fan", steps=[
            {"id": "fan", "name": "Fan", "type": "parallel", "parallel_branches": [["slow", "after"], ["p2"]],
             "next_step_id": "m"},
            self._slow(), _print("after", "used {state.out}"), _print("p2"),
            {"id": "m", "name": "Merge", "type": "merge", "input_keys": ["o_after", "o_p2"],
             "output_key": "merged", "next_step_id": None},
        ])
        events = await self._run(orch)

        assert _status_of(events) == "failed"
        started = [e.get("orch_step_id") for e in events if e.get("type") == "step_start"]
        assert "after" not in started and "m" not in started
