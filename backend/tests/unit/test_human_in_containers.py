"""
A step that stops to ask a person, inside a Loop, a Parallel branch or a nested
orchestration, is answered — and the container carries on (F109).

Until F109 the two resumes disagreed about where such a run was. The engine
recorded the container as the run's position and, answered, resumed *past* it:
the remaining iterations were dropped, and the validator warned that they
would be. The fleet adapter overwrote that position with the inner step's id,
so a fleet run resumed from the body step as if it stood alone and, its
`next_step_id` being `None`, completed there — iteration two and the loop's own
next step never ran, and nothing said so.

Now the container keeps its place at the pause, the answer goes to the step
that asked, and the resume re-enters the container. Every case runs twice: the
in-process engine and the real fleet job functions (fakeredis, the SQLite store;
only the agent loop and the MCP pool are faked), because the fleet is where the
two used to part.
"""
import types
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _print(sid, content=None, nxt=None, key=None):
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": content or sid,
            "output_key": key or f"o_{sid}", "next_step_id": nxt}


def _human(sid, key="answer", prompt="ok?", nxt=None, **extra):
    return {"id": sid, "name": sid.upper(), "type": "human", "human_prompt": prompt,
            "output_key": key, "next_step_id": nxt, **extra}


def _loop(sid, body, nxt=None, **mode):
    mode = mode or {"loop_count": 2}
    return {"id": sid, "name": sid.upper(), "type": "loop", "loop_step_ids": body,
            "next_step_id": nxt, **mode}


# ── the two drivers ──────────────────────────────────────────────────────────
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


async def _ctx(fake_redis, job_try: int = 1) -> dict:
    from core.store import get_store
    return {"redis": fake_redis, "session_factory": await get_store(),
            "job_try": job_try, "job_id": f"job-{job_try}"}


class _Driver:
    """Start a run and answer it, either in-process or through the fleet's jobs."""

    def __init__(self, kind, worker_mod, fake_redis):
        self.kind = kind
        self._worker = worker_mod
        self._redis = fake_redis

    async def start(self, orch: dict, run_id: str, initial_state: dict | None = None) -> str:
        if self.kind == "engine":
            from core.models_orchestration import Orchestration
            from core.orchestration.engine import OrchestrationEngine
            engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
            events = [e async for e in engine.run("go", run_id=run_id, initial_state=initial_state)]
            return _status_of(events)
        result = await self._worker.run_orchestration_job(
            await _ctx(self._redis), run_id=run_id, orch_id=orch["id"], initial_input="go",
            initial_state=initial_state,
        )
        return result["status"]

    async def answer(self, run_id: str, response) -> str:
        if self.kind == "engine":
            from core.orchestration.engine import OrchestrationEngine
            events = [e async for e in OrchestrationEngine.resume(run_id, response, _server())]
            return _status_of(events)
        result = await self._worker.resume_orchestration_job(
            await _ctx(self._redis), run_id=run_id, human_response=response,
        )
        return result["status"]


def _status_of(events: list[dict]) -> str:
    if events and events[-1].get("type") == "human_input_required":
        return "paused"
    for e in reversed(events):
        if e.get("type") == "orchestration_complete":
            return e.get("status", "completed")
    return "unknown"


@pytest.fixture(params=["engine", "fleet"])
def driver(request, worker, fake_redis):
    return _Driver(request.param, worker, fake_redis)


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _entries(run, step_id):
    return [h for h in run.step_history if h.get("step_id") == step_id]


def _results(value) -> list:
    """The per-iteration results a loop promotes into a body step's key."""
    return [r["result"] for r in value]


# ── a nested orchestration waiting on its own human step ─────────────────────
def _child(orch_id):
    return S.make_orchestration(id=orch_id, entry_step_id="approve", steps=[
        _human("approve", key="approval", nxt="p"),
        _print("p", "got {state.approval}", key="final"),
    ])


def _delegate(agent_id, nxt=None, sid="delegate"):
    return {"id": sid, "name": sid.upper(), "type": "agent", "agent_id": agent_id,
            "prompt_template": "go", "output_key": "result", "next_step_id": nxt}


# ── a loop ───────────────────────────────────────────────────────────────────
class TestALoopAsksEachIteration:
    async def test_each_iteration_is_answered_and_the_loop_moves_on(self, driver):
        orch = S.make_orchestration(id="orch_hloop", entry_step_id="loop1", steps=[
            _loop("loop1", ["before", "ask", "after"], nxt="tail"),
            _print("before"), _human("ask"), _print("after"), _print("tail"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_hloop") == "paused"
        paused = await _run("run_hloop")
        assert paused.current_step_id == "loop1", "the run's position is the container"
        assert "_loop_current_iteration" not in paused.shared_state

        second = await driver.answer("run_hloop", "one")
        assert second == "paused", "iteration two asks again"
        assert (await _run("run_hloop")).current_step_id == "loop1"

        assert await driver.answer("run_hloop", "two") == "completed"
        run = await _run("run_hloop")
        state = run.shared_state
        assert _results(state["answer"]) == ["one", "two"]
        assert len(state["o_after"]) == 2, "the steps after the question ran each time"
        assert state["o_tail"] == "tail", "the loop's next step ran"
        asks = _entries(run, "ask")
        assert [(h["status"], h["step_type"], h.get("within")) for h in asks] == [
            ("completed", "human", "loop1"), ("completed", "human", "loop1")]
        assert [h["status"] for h in _entries(run, "loop1")] == ["completed"]
        assert not any(k.startswith("_loop_") for k in state), sorted(state)


class TestTheModesOfALoop:
    async def test_a_for_each_asks_about_each_item(self, driver):
        orch = S.make_orchestration(id="orch_hfor", entry_step_id="loop1", steps=[
            _loop("loop1", ["ask"], nxt="tail", loop_over="state.items"),
            _human("ask", prompt="Ship {state.loop_item}?"), _print("tail"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_hfor", {"items": ["a", "b"]}) == "paused"
        assert (await _run("run_hfor")).human_prompt == "Ship a?"
        assert await driver.answer("run_hfor", "yes") == "paused"
        assert (await _run("run_hfor")).human_prompt == "Ship b?"
        assert await driver.answer("run_hfor", "no") == "completed"

        state = (await _run("run_hfor")).shared_state
        assert _results(state["answer"]) == ["yes", "no"]
        assert state["o_tail"] == "tail"

    async def test_a_while_loop_finishes_the_iteration_it_paused_in(self, driver):
        """The first body step makes the condition false. Asked again when the
        iteration is re-entered, it would stop the loop before `after` ran."""
        orch = S.make_orchestration(id="orch_hwhile", entry_step_id="loop1", steps=[
            _loop("loop1", ["mark", "ask", "after"], nxt="tail",
                  loop_while_condition="state.flag != 'set'"),
            _print("mark", "set", key="flag"), _human("ask"), _print("after"), _print("tail"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_hwhile", {"flag": ""}) == "paused"
        assert await driver.answer("run_hwhile", "go on") == "completed"

        run = await _run("run_hwhile")
        assert len(run.shared_state["o_after"]) == 1
        assert run.shared_state["o_tail"] == "tail"
        assert [h["status"] for h in _entries(run, "after")] == ["completed"]


class TestAParallelBranchAsks:
    async def test_the_branch_carries_on_and_so_do_the_ones_after_it(self, driver):
        orch = S.make_orchestration(id="orch_hpar", entry_step_id="par", steps=[
            {"id": "par", "name": "PAR", "type": "parallel", "next_step_id": "tail",
             "parallel_branches": [["p0"], ["ask", "after"], ["ask2"]]},
            _print("p0"), _human("ask"), _print("after"), _human("ask2", key="answer2"),
            _print("tail"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_hpar") == "paused"
        assert (await _run("run_hpar")).current_step_id == "par"
        assert await driver.answer("run_hpar", "one") == "paused", "the next branch asks"
        assert await driver.answer("run_hpar", "two") == "completed"

        run = await _run("run_hpar")
        state = run.shared_state
        assert (state["answer"], state["answer2"]) == ("one", "two")
        assert state["o_after"] == "after" and state["o_tail"] == "tail"
        assert [h["status"] for h in _entries(run, "p0")] == ["completed"], "p0 ran once"
        assert [(h["status"], h.get("within")) for h in _entries(run, "ask")] == [("completed", "par")]
        assert "_parallel_progress_par" not in state


class TestALoopInALoop:
    async def test_every_question_is_asked_and_both_loops_finish(self, driver):
        orch = S.make_orchestration(id="orch_hll", entry_step_id="outer", steps=[
            _loop("outer", ["inner"], nxt="tail"), _loop("inner", ["ask"]),
            _human("ask"), _print("tail"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_hll") == "paused"
        state = (await _run("run_hll")).shared_state
        assert state["_loop_progress_outer"]["paused"]["step_id"] == "inner"
        assert state["_loop_progress_inner"]["paused"]["step_id"] == "ask"
        assert not any(k in state for k in ("_loop_current_iteration", "_loop_total"))

        for answer in ("one", "two", "three"):
            assert await driver.answer("run_hll", answer) == "paused"
        assert await driver.answer("run_hll", "four") == "completed"

        run = await _run("run_hll")
        assert _results(run.shared_state["answer"]) == ["three", "four"]  # the inner loop's last pass
        assert [(h["status"], h.get("within")) for h in _entries(run, "ask")] == [("completed", "inner")] * 4
        assert run.shared_state["o_tail"] == "tail"
        assert not any(k.startswith("_loop_") for k in run.shared_state), sorted(run.shared_state)


class TestANestedStepInALoop:
    async def test_each_iteration_waits_on_its_own_sub_run(self, driver):
        orch = S.make_orchestration(id="orch_hnest", entry_step_id="loop1", steps=[
            _loop("loop1", ["delegate"], nxt="tail"), _delegate("agent_hnest"), _print("tail"),
        ])
        await S.seed_agents([S.make_orchestrator_agent("orch_hnest_child", id="agent_hnest")])
        await S.seed_orchestrations([orch, _child("orch_hnest_child")])

        assert await driver.start(orch, "run_hnest") == "paused"
        assert await driver.answer("run_hnest", "one") == "paused"
        assert await driver.answer("run_hnest", "two") == "completed"

        run = await _run("run_hnest")
        assert _results(run.shared_state["result"]) == ["got one", "got two"]
        assert run.shared_state["o_tail"] == "tail"
        assert run.nested_run_id is None
        child = await _run("run_hnest__delegate_d1")
        assert child.status == "completed" and child.shared_state["approval"] == "two"


# ── a tool call waiting on approval, through the fleet's jobs ────────────────
AGENT = "risky_agent"
_DANGER = {"type": "function", "function": {"name": "danger", "description": "d",
                                            "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture
async def risky_agent():
    await S.seed_agents([S.make_agent(
        id=AGENT, tools=["all"], skip_default_tools=True,
        tool_policy={"undeclared": "confirm"},
    )])


class _Tools:
    """The real agent loop with one confirm-policy tool; records what ran."""

    def __init__(self):
        self.executed: list = []

    def patch(self):
        import core.react_engine as re_mod
        real = re_mod.run_agent_step

        async def with_tools(**kwargs):
            async def executor(name, args):
                self.executed.append((name, args))
                return f"ran-{name}"
            kwargs.setdefault("tools_override", [_DANGER])
            kwargs.setdefault("tool_executor", executor)
            async for ev in real(**kwargs):
                yield ev

        stack = _m.patch.object(re_mod, "run_agent_step", with_tools)
        inner = _m.patch("core.orchestration.steps.run_agent_step", with_tools, create=True)
        return stack, inner


def _work(nxt=None):
    return {"id": "work", "name": "Do the work", "type": "agent", "agent_id": AGENT,
            "prompt_template": "go", "output_key": "result", "max_turns": 5, "next_step_id": nxt}


class TestAToolApprovalOnTheFleet:
    async def test_an_approved_call_runs_and_the_step_finishes(
        self, risky_agent, worker, fake_redis, fake_llm,
    ):
        """The fleet's resume used to be a copy of the engine's without its
        tool-approval branch, so an approval was taken for a human step's answer:
        written into the step's output key and stepped past, the call never run."""
        orch = S.make_orchestration(id="orch_fleet_ap", entry_step_id="work",
                                    steps=[_work("after"), _print("after", "done", key="tail")])
        await S.seed_orchestrations([orch])
        tools = _Tools()
        a, b = tools.patch()
        with a, b:
            fake_llm.script([tool_call("danger"), "done"])
            started = await worker.run_orchestration_job(
                await _ctx(fake_redis), run_id="run_fleet_ap", orch_id="orch_fleet_ap",
                initial_input="go",
            )
            assert started["status"] == "paused"
            assert tools.executed == []

            fake_llm.script(["all finished"])
            result = await worker.resume_orchestration_job(
                await _ctx(fake_redis), run_id="run_fleet_ap", human_response={"approve": True},
            )

        assert tools.executed == [("danger", {})], "the approved call never ran"
        assert result["status"] == "completed"
        run = await _run("run_fleet_ap")
        assert run.shared_state["result"] == "all finished"
        assert run.shared_state["tail"] == "done"
        assert "pending_approval" not in (run.step_progress.get("work") or {})


class TestANestedResume:
    async def test_a_sub_run_two_levels_down_that_asks_twice(self, driver):
        """The parent waits on its sub-run, which waits on its own. When the
        innermost asked again, the parent recorded *that* run as the one it
        waits on, so the next answer resumed it directly: the middle run never
        finished and the parent took the innermost's answer as its step's."""
        inner = S.make_orchestration(id="orch_n2_inner", entry_step_id="h1", steps=[
            _human("h1", key="first", nxt="h2"), _human("h2", key="second", nxt="p"),
            _print("p", "got {state.first} and {state.second}", key="final"),
        ])
        middle = S.make_orchestration(id="orch_n2_middle", entry_step_id="delegate",
                                      steps=[_delegate("agent_n2_inner")])
        parent = S.make_orchestration(id="orch_n2", entry_step_id="delegate",
                                      steps=[_delegate("agent_n2_middle", "tail"), _print("tail")])
        await S.seed_agents([
            S.make_orchestrator_agent("orch_n2_inner", id="agent_n2_inner"),
            S.make_orchestrator_agent("orch_n2_middle", id="agent_n2_middle"),
        ])
        await S.seed_orchestrations([parent, middle, inner])

        assert await driver.start(parent, "run_n2") == "paused"
        assert (await _run("run_n2")).nested_run_id == "run_n2__delegate_d1"
        assert await driver.answer("run_n2", "a") == "paused"
        assert (await _run("run_n2")).nested_run_id == "run_n2__delegate_d1"
        assert await driver.answer("run_n2", "b") == "completed"

        run = await _run("run_n2")
        assert run.shared_state["result"] == "got a and b"
        assert run.shared_state["o_tail"] == "tail"
        assert (await _run("run_n2__delegate_d1")).status == "completed"

    async def test_a_sub_run_that_cannot_finish_fails_its_parent(self, driver):
        """Its sub-orchestration deleted while the parent waited. The parent was
        failed in its checkpoint, but no `orchestration_complete` followed — so
        the fleet, which takes the status from that event, recorded it
        `completed`."""
        parent = S.make_orchestration(id="orch_nfail", entry_step_id="delegate",
                                      steps=[_delegate("agent_nfail", "tail"), _print("tail")])
        await S.seed_agents([S.make_orchestrator_agent("orch_nfail_child", id="agent_nfail")])
        await S.seed_orchestrations([parent, _child("orch_nfail_child")])
        assert await driver.start(parent, "run_nfail") == "paused"

        await S.seed_orchestrations([parent])  # saving replaces the set: the child is gone
        assert await driver.answer("run_nfail", "yes") == "failed"

        run = await _run("run_nfail")
        assert run.status == "failed"
        assert not run.waiting_for_human
        assert "o_tail" not in run.shared_state

    async def test_the_sub_run_resumes_at_its_own_depth(self, driver, monkeypatch):
        """A resumed sub-run used to be built at depth 0, so it wrote memory
        summaries and was scored as if it were a run of its own."""
        from core.orchestration.engine import OrchestrationEngine
        depths: dict[str, int] = {}
        real = OrchestrationEngine._write_memory_summaries

        async def recording(self, run):
            depths[run.run_id] = self.depth
            return await real(self, run)

        monkeypatch.setattr(OrchestrationEngine, "_write_memory_summaries", recording)
        parent = S.make_orchestration(id="orch_ndepth", entry_step_id="delegate",
                                      steps=[_delegate("agent_ndepth")])
        await S.seed_agents([S.make_orchestrator_agent("orch_ndepth_child", id="agent_ndepth")])
        await S.seed_orchestrations([parent, _child("orch_ndepth_child")])
        assert await driver.start(parent, "run_ndepth") == "paused"

        assert await driver.answer("run_ndepth", "yes") == "completed"
        assert (await _run("run_ndepth")).shared_state["result"] == "got yes"
        assert depths == {"run_ndepth__delegate_d1": 1, "run_ndepth": 0}


class TestAToolApprovalInALoop:
    async def test_the_call_runs_and_the_loop_moves_on(self, driver, risky_agent, fake_llm):
        orch = S.make_orchestration(id="orch_hloop_ap", entry_step_id="loop1", steps=[
            _loop("loop1", ["work"], nxt="after", loop_count=1), _work(),
            _print("after", "done", key="tail"),
        ])
        await S.seed_orchestrations([orch])
        tools = _Tools()
        a, b = tools.patch()
        with a, b:
            fake_llm.script([tool_call("danger"), "done"])
            assert await driver.start(orch, "run_hloop_ap") == "paused"
            paused = await _run("run_hloop_ap")
            assert paused.current_step_id == "loop1"
            assert paused.step_progress["work"]["pending_approval"]["tool_name"] == "danger"

            fake_llm.script(["all finished"])
            assert await driver.answer("run_hloop_ap", {"approve": True}) == "completed"

        assert tools.executed == [("danger", {})]
        state = (await _run("run_hloop_ap")).shared_state
        assert _results(state["result"]) == ["all finished"]
        assert state["tail"] == "done"


class _Agents:
    """run_agent_step, counting calls per agent; named agents block on their first call."""

    def __init__(self):
        import asyncio
        self.calls: dict[str, int] = {}
        self.block_once: set[str] = set()
        self.started = asyncio.Event()

    async def __call__(self, **kwargs):
        import asyncio
        agent_id = kwargs["agent_id"]
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        if agent_id in self.block_once:
            self.block_once.discard(agent_id)
            self.started.set()
            await asyncio.sleep(30)
        yield {"type": "final", "response": f"{agent_id} answered"}


class TestARedeliveredAnswerInALoop:
    async def test_the_answer_is_applied_once_and_the_iteration_finishes(
        self, worker, fake_redis, monkeypatch,
    ):
        """An answer job stopped mid-iteration (a deploy) and handed out again
        resumes from the checkpoint: the answered step is not asked again, and
        the step it was running runs again."""
        import asyncio
        import core.react_engine as re_mod
        agents = _Agents()
        monkeypatch.setattr(re_mod, "run_agent_step", agents)
        orch = S.make_orchestration(id="orch_hredeliver", entry_step_id="loop1", steps=[
            _loop("loop1", ["ask", "work"], nxt="tail", loop_count=1), _human("ask"),
            {"id": "work", "name": "WORK", "type": "agent", "agent_id": "a2",
             "output_key": "o_work", "next_step_id": None, "max_turns": 3},
            _print("tail"),
        ])
        await S.seed_orchestrations([orch])
        started = await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_hredeliver", orch_id="orch_hredeliver",
            initial_input="go",
        )
        assert started["status"] == "paused"

        agents.block_once.add("a2")
        job = asyncio.create_task(worker.resume_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_hredeliver", human_response="yes"))
        await asyncio.wait_for(agents.started.wait(), 5)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

        result = await worker.resume_orchestration_job(
            await _ctx(fake_redis, 2), run_id="run_hredeliver", human_response="yes")

        assert result["status"] == "completed"
        assert agents.calls == {"a2": 2}
        run = await _run("run_hredeliver")
        assert _results(run.shared_state["answer"]) == ["yes"]
        assert [h["status"] for h in _entries(run, "ask")] == ["completed"]
        assert run.shared_state["o_tail"] == "tail"


# ── runs paused before F109 ──────────────────────────────────────────────────
async def _as_before_f109(run_id: str, *, fleet: bool) -> None:
    """Rewrite a paused loop run into the shape a pre-F109 build left.

    Both shapes dropped the loop's progress at the pause and left its iteration
    markers in the checkpoint; the engine's history entry named the loop. The
    fleet then overwrote the position with the step that asked.
    """
    from core.orchestration.state import SharedState
    run = await _run(run_id)
    for entry in run.step_history:
        if entry.get("status") == "paused":
            entry.update({"step_id": "loop1", "step_name": "LOOP1", "step_type": "loop"})
            entry.pop("within", None)
    run.shared_state.pop("_loop_progress_loop1", None)
    run.shared_state.update({"_loop_current_iteration": 1, "_loop_total": 2})
    run.current_step_id = "ask" if fleet else "loop1"
    await SharedState(run).checkpoint()


class TestARunPausedBeforeF109:
    """Its place in the loop was never kept, so it cannot carry on inside. It
    resumes past the loop, as a pre-F109 in-process run did — where the fleet's
    shape used to end the run at the step that asked."""

    @pytest.mark.parametrize("fleet_shape", [True, False], ids=["fleet-shape", "engine-shape"])
    async def test_it_resumes_past_the_loop(self, driver, fleet_shape):
        orch = S.make_orchestration(id="orch_hold", entry_step_id="loop1", steps=[
            _loop("loop1", ["ask", "after"], nxt="tail"), _human("ask"), _print("after"),
            _print("tail"),
        ])
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_hold") == "paused"
        await _as_before_f109("run_hold", fleet=fleet_shape)

        assert await driver.answer("run_hold", "yes") == "completed"

        run = await _run("run_hold")
        assert run.shared_state["o_tail"] == "tail", "the loop's next step ran"
        assert run.shared_state["human_response"] == "yes"
        assert not [h for h in run.step_history if h.get("status") == "paused"]
        assert not any(k in run.shared_state for k in ("_loop_current_iteration", "_loop_total"))
        if fleet_shape:
            # The loop's open entry was the wait: closed as the human step's.
            assert [(h["status"], h["step_type"], h.get("within")) for h in _entries(run, "ask")] == [
                ("completed", "human", "loop1")]
            assert run.shared_state["answer"] == "yes"


# ── an agent step's wait is one entry, closed ────────────────────────────────
def _open(run) -> list:
    return [(h["step_id"], h["status"]) for h in run.step_history
            if h.get("status") in ("paused", "running")]


class TestAnAgentStepsWaitIsOneEntry:
    """An agent step that waited — on a sub-orchestration's question, or on a
    tool call's approval — ran once, and has one history entry, closed.

    It used to have two: a nested resume appended a completed entry and left
    the paused one open, and an approval reopened the paused entry as `running`
    for ever while the step's end appended another. Cloud prices every entry
    and lets an open one claim every later usage row; a loop multiplied both.
    Neither counts the person's wait as the step's duration."""

    async def test_a_nested_step(self, driver):
        orch = S.make_orchestration(id="orch_wnest", entry_step_id="delegate",
                                    steps=[_delegate("agent_wnest", "tail"), _print("tail")])
        await S.seed_agents([S.make_orchestrator_agent("orch_wnest_child", id="agent_wnest")])
        await S.seed_orchestrations([orch, _child("orch_wnest_child")])
        assert await driver.start(orch, "run_wnest") == "paused"
        assert await driver.answer("run_wnest", "yes") == "completed"

        run = await _run("run_wnest")
        assert [(h["status"], h["step_type"]) for h in _entries(run, "delegate")] == [
            ("completed", "agent")]
        assert _open(run) == []
        assert not _entries(run, "delegate")[0].get("duration_seconds")

    async def test_a_nested_step_in_a_loop(self, driver):
        orch = S.make_orchestration(id="orch_wnestl", entry_step_id="loop1", steps=[
            _loop("loop1", ["delegate"], nxt="tail"), _delegate("agent_wnestl"), _print("tail"),
        ])
        await S.seed_agents([S.make_orchestrator_agent("orch_wnestl_child", id="agent_wnestl")])
        await S.seed_orchestrations([orch, _child("orch_wnestl_child")])
        assert await driver.start(orch, "run_wnestl") == "paused"
        assert await driver.answer("run_wnestl", "one") == "paused"
        assert await driver.answer("run_wnestl", "two") == "completed"

        run = await _run("run_wnestl")
        assert [(h["status"], h.get("within")) for h in _entries(run, "delegate")] == [
            ("completed", "loop1")] * 2
        assert _open(run) == []

    @pytest.mark.parametrize("in_loop", [False, True], ids=["top-level", "in-a-loop"])
    async def test_an_approved_tool_call(self, driver, risky_agent, fake_llm, in_loop):
        steps = [_work("after"), _print("after", "done", key="tail")]
        if in_loop:
            steps = [_loop("loop1", ["work"], nxt="after", loop_count=1), _work(),
                     _print("after", "done", key="tail")]
        orch = S.make_orchestration(id="orch_wap", entry_step_id=steps[0]["id"], steps=steps)
        await S.seed_orchestrations([orch])
        tools = _Tools()
        a, b = tools.patch()
        with a, b:
            fake_llm.script([tool_call("danger"), "done"])
            assert await driver.start(orch, "run_wap") == "paused"
            fake_llm.script(["all finished"])
            assert await driver.answer("run_wap", {"approve": True}) == "completed"

        run = await _run("run_wap")
        assert [h["status"] for h in _entries(run, "work")] == ["completed"]
        assert _open(run) == []
