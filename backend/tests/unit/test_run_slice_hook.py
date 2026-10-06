"""A run's events outlive their stream's trim when an embedder slices them out (F377).

`RunEventPublisher` caps a run's Redis stream at about `STREAM_MAXLEN` events,
so an embedder that keeps a run's whole log (cloud's archive) read only the last
~10,000 of a chatty long run. It now installs a hook that the publisher nudges
as events arrive, so it can copy them out before they are trimmed. The hook owns
its cursor; the publisher only says "more came", never waits for it, and never
runs two for one run at once.
"""
import asyncio

import pytest

from core.scale import pubsub


@pytest.fixture
def hook(monkeypatch):
    calls: list[str] = []
    gate = asyncio.Event()
    gate.set()

    async def slice_out(run_id: str) -> None:
        calls.append(run_id)
        await gate.wait()

    monkeypatch.setattr(pubsub.RunEventPublisher, "SLICE_EVERY", 3)
    pubsub.set_run_slice_hook(slice_out)
    yield calls, gate
    pubsub.set_run_slice_hook(None)


async def _publish(publisher, n: int) -> None:
    for i in range(n):
        await publisher.publish({"type": "tick", "i": i})


async def test_the_hook_is_nudged_as_events_arrive(fake_redis, hook):
    calls, _gate = hook
    await _publish(pubsub.RunEventPublisher(fake_redis, "run_a"), 7)
    await pubsub.drain_run_slices("run_a")
    assert calls == ["run_a", "run_a"]  # after the 3rd and the 6th


async def test_a_slow_hook_never_holds_up_publishing_and_runs_once_more_after(fake_redis, hook):
    calls, gate = hook
    gate.clear()
    publisher = pubsub.RunEventPublisher(fake_redis, "run_b")
    await asyncio.wait_for(_publish(publisher, 9), 1)  # three nudges while the first is still slicing
    assert calls == ["run_b"]
    gate.set()
    await pubsub.drain_run_slices("run_b")
    assert calls == ["run_b", "run_b"], "the nudges that came while it ran ask for one more pass, not three"
    assert await fake_redis.xlen("synapse:run:run_b:events") == 9


async def test_a_failing_hook_is_logged_and_publishing_goes_on(fake_redis, monkeypatch):
    async def broken(run_id: str) -> None:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(pubsub.RunEventPublisher, "SLICE_EVERY", 2)
    pubsub.set_run_slice_hook(broken)
    try:
        await _publish(pubsub.RunEventPublisher(fake_redis, "run_c"), 4)
        await pubsub.drain_run_slices("run_c")
    finally:
        pubsub.set_run_slice_hook(None)
    assert await fake_redis.xlen("synapse:run:run_c:events") == 4


async def test_without_a_hook_nothing_is_sliced(fake_redis):
    await _publish(pubsub.RunEventPublisher(fake_redis, "run_d"), 3)
    await pubsub.drain_run_slices("run_d")  # nothing in flight: returns at once
