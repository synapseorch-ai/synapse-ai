"""A run stopped at its limit inside a Loop or a Parallel says which, in whole words (F420).

Measured on the fleet (C2, `c2len`, Free's 30-minute plan, a top-level Loop):
the run ended `failed` at 1,800 s of work, charged for what ran, and its reason
and the Loop's entry read "Timed out after 1799.9887125492096s" — a raw float,
and not PL1a's sentence naming the plan's limit. A top-level container was given
what was left of the run with its `why` dropped, so the message neither rounded
nor named the budget.

Now every "Timed out …" goes through `timed_out` (whole seconds from ten up),
and a container keeps its why: "Timed out after 1800s (what was left of the
30-minute run limit of this workspace's plan)".

And the body step the limit cut is entered (Naveen, D267). Its own timer saw a
cancel, not a timeout, so it wrote nothing and the log never named it: now a
plain step gets a `cut` entry with the same reason (priced as one execution, as
C3's cancel cut entries are), and a container cut inside gets a `failed` entry,
not `cut` — a host times a container's own entry only when it is cut, and its
body's entries are there. The container at the top is entered last, so the
run's reason is its sentence. A person's cancel writes no body entry here.
"""
import asyncio
import contextlib
import re
import time
import types

import anyio
import pytest

from _fakes import seed as S

LIMIT = "the test's one-second limit"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture(autouse=True)
def no_ceiling():
    from core.orchestration import time_limits
    yield
    time_limits.set_run_ceiling(None)


def ceiling(seconds: float = 1.0, name: str = LIMIT):
    from core.orchestration import time_limits

    async def hook(run_id):
        return None if "__" in run_id else time_limits.RunCeiling(seconds=seconds, name=name)

    time_limits.set_run_ceiling(hook)


@pytest.fixture
def slow_prints(monkeypatch):
    """A Print step that takes 0.4 s of real time."""
    from core.models_orchestration import StepType
    from core.orchestration import steps as steps_mod

    class Slow:
        async def execute(self, step, run, engine):
            await anyio.sleep(0.4)
            run.shared_state[step.output_key] = step.id
            return
            yield

    monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.PRINT, Slow())


#: How long a finished held Print takes, on the test's clock.
PRINT_S = 0.1


@pytest.fixture
def held_prints(monkeypatch):
    """Prints that finish at once until `held_prints.after` have, then are held until the limit cuts them (F519).

    A 0.4 s Print timed the cut against step boundaries: on a loaded machine the
    limit could fire while the run was between two steps — writing its checkpoint
    — and the run was left with no cut entry while the engine was right. Held,
    there is always a step in progress when the limit comes, and the ones before
    it have finished.

    And the limit comes when the steps are held, not at one second of the real
    clock (BF10): a loaded machine took longer than that to seed, start and run
    the first prints, and the limit still fired between them
    (`test_a_container_cut_inside_is_failed_not_cut` in loaded full suites). The
    engine reads the run's budget from `time.time`, which is the test's clock
    here: a finished Print is `PRINT_S` of it, and once `held_prints.parks` steps
    are held it moves to the run's limit and the run's outermost `fail_after` —
    the top container's, what was left of the ceiling — is made due. The engine
    meets the same `TimeoutError`, from the same scope, with its budget spent, as
    when the ceiling runs out on the real clock.
    """
    from core.models_orchestration import StepType
    from core.orchestration import steps as steps_mod

    start = time.time()
    now = [start]
    monkeypatch.setattr(time, "time", lambda: now[0])

    scopes: list[anyio.CancelScope] = []
    real_fail_after = anyio.fail_after

    @contextlib.contextmanager
    def watched_fail_after(delay, shield=False):
        # No deadline of its own on the real clock, which a loaded machine reaches first: due only when the
        # held steps say so, below. `fail_after` still raises `TimeoutError` then, as when its delay runs out.
        with real_fail_after(None, shield) as scope:
            scopes.append(scope)
            yield scope

    monkeypatch.setattr(anyio, "fail_after", watched_fail_after)

    class Held:
        after = 0
        done = 0
        parks = 1      # how many are held when the limit comes (one per Parallel branch)
        parked = 0
        limit_s = 1.0  # the ceiling the test set (`ceiling()`)

        async def execute(self, step, run, engine):
            if self.done >= self.after:
                self.parked += 1
                if self.parked == self.parks:
                    now[0] = start + self.limit_s                 # the run's limit, reached
                    scopes[0].deadline = anyio.current_time()     # and its scope, due
                await anyio.sleep_forever()
            self.done += 1
            now[0] += PRINT_S
            run.shared_state[step.output_key] = step.id
            return
            yield

    held = Held()
    monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.PRINT, held)
    return held


def _print(sid, nxt=None):
    return {"id": sid, "name": sid.title(), "type": "print", "print_content": "x",
            "output_key": f"o_{sid}", "next_step_id": nxt}


def _loop(sid, body, count=20):
    return {"id": sid, "name": sid.title(), "type": "loop", "loop_count": count, "loop_step_ids": body,
            "next_step_id": None}


async def _run(steps, entry, run_id, minutes=10):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id=entry, steps=steps, timeout_minutes=minutes)
    await S.seed_orchestrations([orch])
    return [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), _server()).run(
        "go", run_id=run_id)]


async def _row(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _reason(run):
    from core.orchestration.run_error import failure_reason
    return failure_reason(run.status, run.step_history, run.step_progress)


def _no_raw_float(text):
    assert not re.search(r"\d+\.\d{2,}s", text), text


class TestAContainerAtTheTop:
    async def test_a_loop_names_the_limit_in_whole_words(self, slow_prints):
        ceiling()
        await _run([_loop("outer", ["inner"]), _print("inner")], "outer", "run_f420_loop")

        run = await _row("run_f420_loop")
        assert run.status == "failed"
        outer = [h for h in run.step_history if h["step_id"] == "outer"][-1]
        assert re.fullmatch(rf"Timed out after [\d.]+s \(what was left of {LIMIT}\)", outer["error"]), outer
        _no_raw_float(outer["error"])
        assert _reason(run) == outer["error"], "the run's reason is the container's sentence"

    async def test_the_step_it_cut_is_entered(self, held_prints):
        held_prints.after = 2  # two finished, the third in progress when the limit comes
        ceiling()
        await _run([_loop("outer", ["inner"]), _print("inner")], "outer", "run_f420_cut")

        run = await _row("run_f420_cut")
        inner = [h for h in run.step_history if h["step_id"] == "inner"]
        done, cut = inner[:-1], inner[-1]
        assert done and all(h["status"] == "completed" for h in done)
        assert cut["status"] == "failed" and cut["cut"] is True
        assert f"(what was left of {LIMIT})" in cut["error"]
        assert cut["started_at"] and cut["ended_at"] and 0 <= cut["duration_seconds"] < 1
        assert run.step_history[-1]["step_id"] == "outer", "the container's entry is the newest"

    async def test_a_container_cut_inside_is_failed_not_cut(self, held_prints):
        held_prints.after = 2
        ceiling()
        await _run([_loop("outer", ["middle"], count=5), _loop("middle", ["inner"], count=20), _print("inner")],
                   "outer", "run_f420_nest")

        run = await _row("run_f420_nest")
        order = [h["step_id"] for h in run.step_history if h.get("status") == "failed"]
        assert order == ["inner", "middle", "outer"], run.step_history
        by_id = {h["step_id"]: h for h in run.step_history if h.get("status") == "failed"}
        assert by_id["inner"]["cut"] is True
        assert "cut" not in by_id["middle"], "a host times a cut container, and its body is entered"
        assert f"(what was left of {LIMIT})" in by_id["middle"]["error"]
        assert _reason(run) == by_id["outer"]["error"]

    async def test_a_parallels_branches_are_entered_before_it(self, held_prints):
        held_prints.parks = 2  # the step each branch is in
        ceiling()
        fan = {"id": "fan", "name": "Fan", "type": "parallel", "next_step_id": None,
               "parallel_branches": [["a1", "a2", "a3", "a4"], ["b1", "b2", "b3", "b4"]]}
        steps = [fan] + [_print(f"{b}{i}", f"{b}{i + 1}" if i < 4 else None) for b in "ab" for i in range(1, 5)]
        await _run(steps, "fan", "run_f420_par")

        run = await _row("run_f420_par")
        assert run.status == "failed"
        cut = [h for h in run.step_history if h.get("cut")]
        assert {h["step_id"][0] for h in cut} == {"a", "b"}, "the step each branch was in"
        assert run.step_history[-1]["step_id"] == "fan"
        fan_entry = run.step_history[-1]
        assert re.fullmatch(rf"Timed out after [\d.]+s \(what was left of {LIMIT}\)", fan_entry["error"])
        assert _reason(run) == fan_entry["error"]


class TestTheRunsOwnBudget:
    async def test_it_is_named_when_no_host_limit_binds(self, slow_prints, monkeypatch):
        from core.orchestration import engine as engine_mod
        monkeypatch.setattr(engine_mod.OrchestrationEngine, "_active_seconds",
                            staticmethod(lambda run: 10 * 60 - 1.0))
        await _run([_loop("outer", ["inner"]), _print("inner")], "outer", "run_f420_own")

        run = await _row("run_f420_own")
        assert "(what was left of this run's own timeout)" in _reason(run)
        _no_raw_float(_reason(run))


class TestAPersonsCancel:
    async def test_writes_no_body_entry_here(self, slow_prints):
        """The run's limit is not what stopped it: its host enters the step it
        was in (the fleet's adapter), not the body loop."""
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = S.make_orchestration(id="orch_f420_cancel", entry_step_id="outer", timeout_minutes=10,
                                    steps=[_loop("outer", ["inner"]), _print("inner")])
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())

        async def drive():
            async for _ in engine.run("go", run_id="run_f420_cancel"):
                pass

        task = asyncio.create_task(drive())
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        run = await _row("run_f420_cancel")
        assert not [h for h in run.step_history if h.get("cut")]


class TestABodyStepsOwnLimit:
    async def test_reads_in_whole_seconds(self, monkeypatch):
        from core.models_orchestration import StepType
        from core.orchestration import steps as steps_mod

        class Stuck:
            async def execute(self, step, run, engine):
                await anyio.sleep(5)
                return
                yield

        monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.PRINT, Stuck())
        inner = {**_print("inner"), "timeout_seconds": 1}
        started = time.monotonic()
        await _run([_loop("outer", ["inner"], count=2), inner], "outer", "run_f420_body")
        assert time.monotonic() - started < 4

        run = await _row("run_f420_body")
        entry = [h for h in run.step_history if h["step_id"] == "inner"][-1]
        assert entry["error"] == "Timed out after 1s"
        assert "cut" not in entry, "its own limit, not the run's"
