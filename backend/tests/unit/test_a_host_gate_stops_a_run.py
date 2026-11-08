"""A host's gate is asked before every step, and stops the run in its words (F419).

Cloud charges a run per step, and its run length bounds time, not steps: a Free
run of nested loops ran 1,519 steps in its 30 minutes and overdrew its credit
wallet by 833 credits, because nothing asked between its first step and its
last whether there was anything left to pay with. `run_gate.set_run_gate` lets a
host answer that before each step — at the top of the run, in a Loop's body, in
a Parallel's branches and in a nested run, under the nested run's own id. The
self-hosted product sets none.

Run both in-process and on the fleet's adapter (`driver`).
"""
import pytest

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _delegate, _print, _run, _server, driver, worker,
)

STOP = "Stopped: this workspace is out of execution credits. Add credits to continue."


def gate(allow: int):
    """A host that lets `allow` steps start, then stops the run. Returns what it was asked."""
    from core.orchestration import run_gate

    asked: list[tuple[str, str]] = []

    async def hook(run_id: str, step_type: str):
        asked.append((run_id, step_type))
        return STOP if len(asked) > allow else None

    run_gate.set_run_gate(hook)
    return asked


def _chain(orch_id, n):
    steps = [_print(f"p{i}", f"step {i}", nxt=f"p{i + 1}" if i + 1 < n else None, key=f"k{i}")
             for i in range(n)]
    return S.make_orchestration(id=orch_id, entry_step_id="p0", steps=steps)


def _ran(run, keys):
    return [k for k in keys if k in run.shared_state]


class TestTheHostsGate:
    async def test_at_the_top_it_stops_the_run_before_the_step_in_its_words(self, driver):
        asked = gate(2)
        orch = _chain("orch_f419_top", 4)
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_f419_top") == "failed"

        run = await _run("run_f419_top")
        assert _ran(run, ["k0", "k1", "k2", "k3"]) == ["k0", "k1"]
        assert asked == [("run_f419_top", "print")] * 3
        from core.orchestration.run_error import failure_reason, run_error

        assert run_error(run) == STOP
        assert failure_reason(run.status, run.step_history, run.step_progress) == STOP

    async def test_in_a_loop_it_stops_mid_body(self, driver):
        asked = gate(1 + 3)  # the loop itself, then three of its body's runs
        orch = S.make_orchestration(id="orch_f419_loop", entry_step_id="loop", steps=[
            {"id": "loop", "name": "Loop", "type": "loop", "loop_count": 10,
             "loop_step_ids": ["body"], "next_step_id": "after"},
            _print("body", "iteration {state.loop_index}", key="last"),
            _print("after", "done", key="after"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_f419_loop") == "failed"

        run = await _run("run_f419_loop")
        assert "after" not in run.shared_state
        assert [t for _, t in asked] == ["loop"] + ["print"] * 4
        from core.orchestration.run_error import failure_reason

        assert failure_reason(run.status, run.step_history, run.step_progress) == STOP
        assert sum(1 for e in run.step_history if e.get("step_id") == "body") == 3

    async def test_in_a_parallel_branch_it_stops_the_node(self, driver):
        gate(1)  # the node, then nothing
        orch = S.make_orchestration(id="orch_f419_par", entry_step_id="par", steps=[
            {"id": "par", "name": "Both", "type": "parallel",
             "parallel_branches": [["a"], ["b"]], "next_step_id": "after"},
            _print("a", "a", key="ka"),
            _print("b", "b", key="kb"),
            _print("after", "done", key="after"),
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_f419_par") == "failed"

        run = await _run("run_f419_par")
        assert _ran(run, ["ka", "kb", "after"]) == []
        from core.orchestration.run_error import failure_reason

        assert failure_reason(run.status, run.step_history, run.step_progress) == STOP

    async def test_a_nested_run_asks_under_its_own_id(self, driver):
        asked = gate(100)
        parent = S.make_orchestration(id="orch_f419_nest", entry_step_id="delegate", steps=[
            _delegate("agent_f419_nest", "tail"), _print("tail", "parent", key="tail"),
        ])
        child = S.make_orchestration(id="orch_f419_nest_child", entry_step_id="c0", steps=[
            _print("c0", "child", key="c0"),
        ])
        await S.seed_agents([S.make_orchestrator_agent(child["id"], id="agent_f419_nest")])
        await S.seed_orchestrations([parent, child])

        assert await driver.start(parent, "run_f419_nest") == "completed"

        ids = [run_id for run_id, _ in asked]
        assert ids[0] == "run_f419_nest"
        assert any(i.startswith("run_f419_nest__delegate_") for i in ids)

    async def test_with_none_nothing_changes(self, driver):
        orch = _chain("orch_f419_none", 3)
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f419_none") == "completed"

    async def test_a_gate_that_fails_stops_nothing(self, driver):
        from core.orchestration import run_gate

        async def broken(run_id, step_type):
            raise RuntimeError("the ledger is down")

        run_gate.set_run_gate(broken)
        orch = _chain("orch_f419_broken", 3)
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, "run_f419_broken") == "completed"
