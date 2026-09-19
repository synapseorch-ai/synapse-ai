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


# ── a nested orchestration waiting on its own human step ─────────────────────
def _child(orch_id):
    return S.make_orchestration(id=orch_id, entry_step_id="approve", steps=[
        _human("approve", key="approval", nxt="p"),
        _print("p", "got {state.approval}", key="final"),
    ])


def _delegate(agent_id, nxt=None, sid="delegate"):
    return {"id": sid, "name": sid.upper(), "type": "agent", "agent_id": agent_id,
            "prompt_template": "go", "output_key": "result", "next_step_id": nxt}


class TestANestedResume:
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
