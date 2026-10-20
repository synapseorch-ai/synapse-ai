"""An agent step that pauses for a tool approval keeps the time it worked before (F439).

`pauses.end_step` closed the reopened entry with the resumed stretch's duration
only: the model calls and tools before the step stopped to ask were not in its
`duration_seconds`, so a host that bills a run by its entries' durations (cloud's
`finalize`) charged only the stretch after the answer. Inside a Loop or a
Parallel the entry even started at the pause, so its window missed those calls.

Now the paused entry keeps what was worked before the pause, the resumed
stretch adds to it, and the person's wait is in neither. Inside a container the
entry starts at the step's own start, as at the top level.

Measured on a clock the test moves (BF10, F519's rule): every model call is
half a second of it and the person's wait is a second, and nothing else moves it.
That the person's wait is not counted is proven against the entry's own window
less the wait this test held: the engine paused before the test saw it and
resumed after the answer, so its count can never exceed that. On the real clock
a loaded machine still broke it: two pauses' roundings and the scheduler's gaps
between the engine's stamps and the test's own were more than its 20 ms
(`TestTwoApprovalsInOneExecution[fleet]` and `TestInALoop[engine]` in loaded
full suites); a fixed margin ("under 1.8 s") had failed before that.
"""
import asyncio
import time

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_human_in_containers import AGENT, _Tools, _run, _work, driver, risky_agent, worker  # noqa: F401

CALL_S = 0.5
WAIT_S = 1.0


#: The clock the engine reads (`time.time`): moved by a model call and by the person's wait, never by the machine.
_NOW: list[float] = []


@pytest.fixture
def half_second_calls(monkeypatch, fake_llm):
    """Every model call is half a second of a clock only this test moves."""
    _NOW[:] = [time.time()]
    monkeypatch.setattr(time, "time", lambda: _NOW[0])

    async def half_a_second():
        _NOW[0] += CALL_S
        await asyncio.sleep(0)  # still a turn of the loop, as a call is

    monkeypatch.setattr(fake_llm, "_maybe_delay", half_a_second)


async def _wait_for_the_person():
    """The person's wait: `WAIT_S` of the test's clock, and what it held, read from that clock."""
    seen = time.time()
    _NOW[0] += WAIT_S
    await asyncio.sleep(0)
    return time.time() - seen


async def _approved(driver, orch, run_id, fake_llm, script, approvals=1):
    """The step's entries, and how long this test held the run paused, in seconds."""
    await S.seed_orchestrations([orch])
    tools = _Tools()
    a, b = tools.patch()
    held = 0.0
    with a, b:
        fake_llm.script(script)
        status = await driver.start(orch, run_id)
        for _ in range(approvals):
            assert status == "paused"
            held += await _wait_for_the_person()
            status = await driver.answer(run_id, {"approve": True})
    assert status == "completed"
    return [h for h in (await _run(run_id)).step_history if h.get("step_id") == "work"], held


def _work_only(entry, held):
    """Its window less the wait this test held: the most its work can be (see the docstring)."""
    return (entry["ended_ms"] - entry["started_ms"]) / 1000 - held + ROUNDING


#: A duration is kept to the hundredth and a stamp to the millisecond.
ROUNDING = 0.02


class TestAtTheTop:
    async def test_the_work_before_the_pause_is_counted(self, driver, risky_agent, fake_llm, half_second_calls):
        orch = S.make_orchestration(id="orch_f439_top", entry_step_id="work", steps=[_work()])
        (entry,), held = await _approved(driver, orch, "run_f439_top", fake_llm, [tool_call("danger"), "done"])

        assert entry["status"] == "completed"
        # Two calls of half a second each: one before the pause, one after — and not the wait.
        assert 2 * CALL_S - 0.1 <= entry["duration_seconds"] <= _work_only(entry, held), (entry, held)
        assert "worked_seconds" not in entry


class TestTwoApprovalsInOneExecution:
    """F463 (Naveen's call, F140's shape): the step's second question used to add a
    second entry and leave the first `running` for ever — one execution read as two
    by the loop guard and a host's pricing. Pausing the one entry again would have
    given both questions one `pause_ordinal`, which is how an answer names the
    question it answers. Now the first becomes a closed `human` wait (free, outside
    active time) and the second, typed as the step, carries its start and all the
    work: one priced entry, and each question its own ordinal."""

    async def test_one_priced_entry_with_all_its_work_and_two_questions(
            self, driver, risky_agent, fake_llm, half_second_calls):
        from core.orchestration.pauses import pause_ordinal
        orch = S.make_orchestration(id="orch_f463", entry_step_id="work", steps=[_work()])
        await S.seed_orchestrations([orch])
        tools = _Tools()
        a, b = tools.patch()
        ordinals = []
        held = 0.0
        with a, b:
            fake_llm.script([tool_call("danger"), tool_call("danger", n=2), "done"])
            status = await driver.start(orch, "run_f463")
            for _ in range(2):
                assert status == "paused"
                ordinals.append(pause_ordinal((await _run("run_f463")).step_history))
                held += await _wait_for_the_person()
                status = await driver.answer("run_f463", {"approve": True})
        assert status == "completed"
        assert ordinals[0] != ordinals[1], "each question is its own"

        run = await _run("run_f463")
        entries = [h for h in run.step_history if h.get("step_id") == "work"]
        assert [(e["step_type"], e["status"]) for e in entries] == [("human", "completed"), ("agent", "completed")]
        wait, step = entries
        assert 3 * CALL_S - 0.1 <= step["duration_seconds"] <= _work_only(step, held), (step, held)
        assert step["started_ms"] <= wait["started_ms"], "the step's entry opens at its own start"
        assert wait["ended_at"] and "worked_seconds" not in step
        assert not [e for e in run.step_history if e.get("status") == "running"]


class TestInALoop:
    async def test_the_entry_starts_at_the_step_and_counts_its_work(self, driver, risky_agent, fake_llm,
                                                                    half_second_calls):
        loop = {"id": "lp", "name": "Each", "type": "loop", "loop_count": 1, "loop_step_ids": ["work"],
                "next_step_id": None}
        orch = S.make_orchestration(id="orch_f439_loop", entry_step_id="lp", steps=[loop, _work()])
        (entry,), held = await _approved(driver, orch, "run_f439_loop", fake_llm, [tool_call("danger"), "done"])

        assert entry["status"] == "completed"
        assert 2 * CALL_S - 0.1 <= entry["duration_seconds"] <= _work_only(entry, held), (entry, held)
        # Its window holds the call before the pause: it opens at the step's start.
        assert entry["ended_ms"] - entry["started_ms"] >= (2 * CALL_S + WAIT_S) * 1000 - 100, entry
