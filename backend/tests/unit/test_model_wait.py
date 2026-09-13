"""
`core.model_wait` — a model call that says it is still alive.

A run page watching an agent step saw "thinking · Analyzing your request..."
and then nothing for 187 seconds: one gateway call hung, the provider's
timeout fired, it retried at `debug` level, and the second attempt answered in
two. Nothing reached the stream in between, so a healthy run read as a stuck
one. These pin the two things a watcher is now told: that the wait is still
going, and that an attempt was abandoned and is being retried.
"""
import asyncio

import pytest

import core.model_wait as mw


@pytest.fixture(autouse=True)
def quick_heartbeat(monkeypatch):
    monkeypatch.setattr(mw, "HEARTBEAT_SECONDS", 0.05)


async def _collect(wait):
    return [event async for event in wait.events()]


async def test_a_slow_call_keeps_saying_it_is_waiting():
    async def slow():
        await asyncio.sleep(0.23)
        return "answer"

    ticks = iter(range(0, 1000, 15))  # each reading of the clock is 15s later
    wait = mw.ModelWait(slow(), label="Agent step", clock=lambda: float(next(ticks)))
    events = await _collect(wait)

    assert wait.result == "answer"
    beats = [e for e in events if e["type"] == "thinking"]
    assert len(beats) >= 3, events
    # Elapsed time is the whole point: a number that moves reads as alive.
    assert "waiting on the model" in beats[-1]["message"]
    assert beats[-1]["message"] != beats[0]["message"]


async def test_a_fast_call_adds_nothing():
    async def fast():
        return 42

    wait = mw.ModelWait(fast(), label="Agent step")
    assert await _collect(wait) == []
    assert wait.result == 42


async def test_a_retry_reported_inside_the_call_reaches_the_watcher():
    async def retrying():
        mw.report("The model did not answer within 180s — retrying (2 of 5) in 5s.")
        await asyncio.sleep(0.01)
        return "ok"

    wait = mw.ModelWait(retrying(), label="Agent step")
    events = await _collect(wait)
    notices = [e for e in events if e["type"] == "status"]
    # `status`, not `thinking`: a retry is a fact worth keeping in the log,
    # where a heartbeat is only the live line.
    assert [n["message"] for n in notices] == [
        "The model did not answer within 180s — retrying (2 of 5) in 5s."
    ]


async def test_events_carry_the_callers_fields():
    async def slow():
        await asyncio.sleep(0.12)

    events = await _collect(mw.ModelWait(slow(), label="Decide", extra={"orch_step_id": "ev"}))
    assert events and all(e.get("orch_step_id") == "ev" for e in events)


async def test_an_error_from_the_call_is_raised_to_the_caller():
    async def broken():
        await asyncio.sleep(0.01)
        raise RuntimeError("model unreachable")

    with pytest.raises(RuntimeError, match="model unreachable"):
        await _collect(mw.ModelWait(broken(), label="Agent step"))


async def test_abandoning_the_wait_cancels_the_call():
    """A step timeout or a user's cancel lands on the waiter. The model call
    must not outlive it — a task still spending tokens after the run has been
    told to stop is the failure a cancel exists to prevent."""
    finished = asyncio.Event()
    cancelled = asyncio.Event()

    async def endless():
        try:
            await asyncio.sleep(30)
            finished.set()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    consumer = asyncio.ensure_future(_collect(mw.ModelWait(endless(), label="Agent step")))
    await asyncio.sleep(0.12)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert not finished.is_set()


def test_a_report_outside_any_wait_is_harmless():
    mw.report("nobody is listening")
