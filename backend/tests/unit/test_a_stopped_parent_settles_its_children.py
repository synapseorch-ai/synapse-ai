"""A parent that stops settles the runs nested under it; a failed run says why (F257).

A parent step's timeout or a cancel stopped the parent, but its nested children
kept `running` — or `paused` on a question nobody could answer any more — for
ever: the stop reached them as a cancellation neither their executor nor their
loop records, and the cancel route, the fleet's cancel and the startup sweep all
settled the root alone (the sweep skipped `__` runs on purpose, trusting a
Replay to reuse the child's id).

Now a run that ends anything but completed hands its ending to every unfinished
run under it, with the reason, and so do the cancel paths and the sweep. And a
run that failed outside any step — its time budget, its turn limit, a step that
does not exist, a restart — says why instead of "Unknown error": the reason is
kept on the run (`step_progress["__run__"]["error"]`, no migration) and every
reader of the run's failure reads it.
"""
import asyncio

import pytest

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _human, _print, _run, _server, driver, worker,
)
from tests.unit.test_a_root_reads_running_while_its_child_works import _SLOW, _three_levels, gate  # noqa: F401


def _error(run):
    return ((run.step_progress or {}).get("__run__") or {}).get("error")


async def _nest_ids(root_id):
    l2 = (await _run(root_id)).nested_run_id
    l3 = (await _run(l2)).nested_run_id if l2 else None
    return l2, l3


class TestACancelSettlesTheChildren:
    async def test_a_paused_nest(self, driver):
        from core.orchestration.cancel import cancel_run
        run_id = f"run_f257p_{driver.kind}"
        l1 = await _three_levels(f"orch_f257p_{driver.kind}", [_human("ask")], "ask")
        assert await driver.start(l1, run_id) == "paused"
        l2, l3 = await _nest_ids(run_id)

        assert (await cancel_run(run_id)).status == "cancelled"

        for child_id in (l2, l3):
            child = await _run(child_id)
            assert (child.status, child.waiting_for_human) == ("cancelled", False)
            assert "cancelled" in _error(child)
            assert all(h.get("status") != "paused" for h in child.step_history)

    async def test_a_running_nest(self, gate):
        from core.models_orchestration import Orchestration
        from core.orchestration.cancel import cancel_run
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.runner import spawn_engine_run
        l1 = await _three_levels("orch_f257r", [dict(_SLOW)], "slow")
        engine = OrchestrationEngine(Orchestration.model_validate(l1), _server())
        task, _queue = spawn_engine_run(engine.run("go", run_id="run_f257r"), "run_f257r")
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        from tests.unit.test_nested_runs_can_be_told_apart import _prefixed
        children = await _prefixed("run_f257r__")
        assert len(children) == 2
        assert [(await _run(c)).status for c in children] == ["running", "running"]

        await cancel_run("run_f257r")
        await asyncio.wait({task}, timeout=5)

        assert (await _run("run_f257r")).status == "cancelled"
        assert [(await _run(c)).status for c in children] == ["cancelled", "cancelled"]


class TestAParentStepTimeoutSettlesTheChild:
    async def test_the_child_is_not_left_running(self, driver, gate):
        run_id = f"run_f257t_{driver.kind}"
        l2 = S.make_orchestration(id=f"orch_f257t_{driver.kind}_child", name="Child", entry_step_id="slow",
                                  steps=[dict(_SLOW)])
        l1 = S.make_orchestration(id=f"orch_f257t_{driver.kind}", name="Parent", entry_step_id="call", steps=[
            {"id": "call", "name": "Call", "type": "agent", "agent_id": f"agent_f257t_{driver.kind}",
             "prompt_template": "go", "output_key": "out", "timeout_seconds": 1, "next_step_id": None}])
        await S.seed_agents([S.make_orchestrator_agent(l2["id"], id=f"agent_f257t_{driver.kind}")])
        await S.seed_orchestrations([l1, l2])

        assert await driver.start(l1, run_id) == "failed"

        from tests.unit.test_nested_runs_can_be_told_apart import _prefixed
        (child_id,) = await _prefixed(f"{run_id}__call_d1_")
        child = await _run(child_id)
        assert (child.status, child.waiting_for_human) == ("failed", False)
        assert "timed out" in _error(child) or "Timed out" in _error(child)
        gate.release.set()


class TestTheStartupSweep:
    async def _row(self, run_id, status, waiting=False):
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState
        run = OrchestrationRun(run_id=run_id, orchestration_id="orch_sweep", status=status,
                               waiting_for_human=waiting, current_step_id="s1")
        await SharedState(run).checkpoint()

    async def test_children_of_a_swept_root_are_settled(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await self._row("run_sw1", "running")
        await self._row("run_sw1__call_d1", "running")
        await self._row("run_sw1__call_d1__call2_d2", "paused", waiting=True)

        assert await sweep_interrupted_runs() == ["run_sw1"]

        for cid in ("run_sw1__call_d1", "run_sw1__call_d1__call2_d2"):
            child = await _run(cid)
            assert (child.status, child.waiting_for_human) == ("failed", False)
            assert "restarted" in _error(child)
        assert "restarted" in _error(await _run("run_sw1"))

    async def test_a_paused_nest_is_a_real_wait_and_is_left_alone(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await self._row("run_sw2", "paused", waiting=True)
        await self._row("run_sw2__call_d1", "paused", waiting=True)

        assert await sweep_interrupted_runs() == []
        assert (await _run("run_sw2__call_d1")).status == "paused"

    async def test_an_orphan_of_a_finished_root_is_settled(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await self._row("run_sw3", "completed")
        await self._row("run_sw3__call_d1", "running")

        await sweep_interrupted_runs()

        assert (await _run("run_sw3__call_d1")).status == "failed"
        assert (await _run("run_sw3")).status == "completed"      # a finished root is not rewritten


class TestAFailedRunSaysWhy:
    async def _run_flat(self, orch_id, steps, entry, **kw):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps, **kw)
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        return [e async for e in engine.run("go", run_id=f"run_{orch_id}")]

    async def test_a_turn_limit(self):
        from core.orchestration.run_view import read_run
        from core.routes.orchestrations import _public_run_view
        events = await self._run_flat("orch_f257k", [_print("a", nxt="b"), _print("b")], "a", max_total_turns=1)

        final = next(e["response"] for e in reversed(events) if e.get("type") == "final" and e.get("intent") == "orchestration")
        assert "Unknown error" not in final and "turn limit" in final
        run = await _run("run_orch_f257k")
        assert "turn limit" in _public_run_view(run)["error"]
        assert "turn limit" in (await read_run("run_orch_f257k"))["error"]

    async def test_a_missing_step(self):
        events = await self._run_flat("orch_f257m", [_print("a", nxt="nowhere")], "a")

        final = next(e["response"] for e in reversed(events) if e.get("type") == "final" and e.get("intent") == "orchestration")
        assert "Unknown error" not in final and "nowhere" in final

    async def test_the_run_list_says_why(self):
        from core.orchestration.state import SharedState
        await self._run_flat("orch_f257l", [_print("a", nxt="b"), _print("b")], "a", max_total_turns=1)

        rows = [r for r in await SharedState.list_runs(limit=50) if r["run_id"] == "run_orch_f257l"]
        assert rows and "turn limit" in (rows[0].get("error") or "")

    async def test_a_replay_starts_without_the_old_reason(self):
        from core.orchestration.engine import OrchestrationEngine
        await self._run_flat("orch_f257y", [_print("a", nxt="b"), _print("b")], "a", max_total_turns=1)

        # Room to finish now: the definition is changed and the run replayed.
        orch = S.make_orchestration(id="orch_f257y", entry_step_id="a",
                                    steps=[_print("a", nxt="b"), _print("b")], max_total_turns=10)
        await S.seed_orchestrations([orch])
        [e async for e in OrchestrationEngine.resume_failed("run_orch_f257y", _server())]

        run = await _run("run_orch_f257y")
        assert run.status == "completed" and _error(run) is None


class TestACancelledAgentStepIsLoggedCancelled:
    """An agent step stopped mid-call was logged "completed": only an Exception set
    its log status, and a cancellation is not one."""

    async def test_the_agent_log_says_cancelled(self, monkeypatch):
        import core.agent_logger as agent_logger
        import core.react_engine as react_engine
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.runner import spawn_engine_run

        ended: list[str] = []
        real_end = agent_logger.AgentLogger.run_end
        monkeypatch.setattr(agent_logger.AgentLogger, "run_end",
                            lambda self, status: (ended.append(status), real_end(self, status))[1])
        started = asyncio.Event()

        async def blocked_step(**kwargs):
            started.set()
            await asyncio.Event().wait()
            yield {"type": "final", "response": "never"}

        monkeypatch.setattr(react_engine, "run_agent_step", blocked_step)
        agent = S.make_agent(id="agent_f257log", tools=[])
        orch = S.make_orchestration(id="orch_f257log", entry_step_id="work", steps=[
            {"id": "work", "name": "Work", "type": "agent", "agent_id": agent["id"], "prompt_template": "go",
             "output_key": "out", "next_step_id": None}])
        await S.seed_agents([agent])
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        task, _queue = spawn_engine_run(engine.run("go", run_id="run_f257log"), "run_f257log")
        await asyncio.wait_for(started.wait(), timeout=10)

        task.cancel()
        await asyncio.wait({task}, timeout=5)

        assert ended == ["cancelled"]
