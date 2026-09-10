"""
A chat turn can be stopped, and a turn that ends badly keeps its conversation.

Chat was the one job type with no way out. Orchestration runs have had the
whole stack for a while — a Redis key, a watcher that cancels the task driving
the engine, an endpoint that publishes it — and chat had a `/cancel` route that
wrote a key nothing read, so it returned 200 and did nothing.

The B1 half is a bug these tests would have caught: the failure path passed
`[]` as the message list, and `_upsert_chat_session` lists `messages` among the
columns it updates, so **every failed turn erased the whole conversation**.
"""
import asyncio
import json

import pytest

from core.scale import mcp_pool, worker


class _FakeRedis:
    """Records the stream, and answers the cancel key from a settable flag."""

    def __init__(self, cancelled_after: int | None = None):
        self.events: list[dict] = []
        self.keys: dict[str, str] = {}
        #: Report "cancelled" only from the Nth poll, so a test can let a turn
        #: get going before stopping it.
        self._cancelled_after = cancelled_after
        self.polls = 0

    async def xadd(self, key, fields, **_kw):
        self.events.append(json.loads(fields["data"]))

    async def expire(self, *_a, **_kw):
        return True

    async def set(self, key, value, **_kw):
        self.keys[key] = value

    async def get(self, key):
        if key.startswith("synapse:cancel:chat:"):
            self.polls += 1
            if self._cancelled_after is not None and self.polls >= self._cancelled_after:
                return "1"
        return self.keys.get(key)

    async def delete(self, key):
        self.keys.pop(key, None)


@pytest.fixture
def chat_job(monkeypatch):
    """A chat job with everything external stubbed.

    Hands back the recorded upserts so a test can assert what was persisted,
    which is the half B1 got wrong.
    """
    upserts: list[tuple[str, list]] = []
    history: list[dict] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]

    async def fake_load_job_settings():
        return None

    async def fake_history(session_factory, session_id, agent_id):
        return list(history)

    async def fake_upsert(session_factory, session_id, agent_id, status, messages):
        upserts.append((status, list(messages)))

    async def fake_acquire(*_a, **_kw):
        return object()

    async def fake_release(*_a, **_kw):
        return None

    monkeypatch.setattr(worker, "_load_job_settings", fake_load_job_settings)
    monkeypatch.setattr(worker, "_load_chat_history", fake_history)
    monkeypatch.setattr(worker, "_upsert_chat_session", fake_upsert)
    monkeypatch.setattr(mcp_pool, "acquire", fake_acquire)
    monkeypatch.setattr(mcp_pool, "release", fake_release)
    # Faster than the real 2s so the tests do not sit waiting for a poll.
    monkeypatch.setattr(worker, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return upserts


def _loop_that(events, hang_forever=False):
    """A fake `run_react_loop` yielding `events`, then optionally never ending
    — which is what a turn stuck inside one long provider call looks like."""
    async def fake_loop(request, server_module, should_stop=None, **_kw):
        for ev in events:
            yield ev
            await asyncio.sleep(0)
        if hang_forever:
            await asyncio.sleep(3600)
    return fake_loop


def _patch_loop(monkeypatch, loop):
    import core.react_engine as re_mod
    monkeypatch.setattr(re_mod, "run_react_loop", loop)


class TestStopping:
    async def test_a_wedged_turn_is_actually_interrupted(self, chat_job, monkeypatch):
        """The case a between-turns flag cannot reach.

        The loop yields one event and then blocks for an hour, which is a turn
        inside a single long provider call. Only cancelling the task driving it
        gets the person their screen back.
        """
        _patch_loop(monkeypatch, _loop_that([{"type": "status", "message": "working"}],
                                            hang_forever=True))
        redis = _FakeRedis(cancelled_after=1)
        ctx = {"redis": redis, "session_factory": object()}

        result = await asyncio.wait_for(
            worker.run_agent_chat_job(ctx, "chat_1", None, "hello"), timeout=5)

        assert result["status"] == "cancelled"

    async def test_the_stream_is_told_why_it_ended(self, chat_job, monkeypatch):
        """A server that stops talking without saying why is a server bug: the
        API goes on sending keepalives and the browser spins forever."""
        _patch_loop(monkeypatch, _loop_that([{"type": "status", "message": "working"}],
                                            hang_forever=True))
        redis = _FakeRedis(cancelled_after=1)
        ctx = {"redis": redis, "session_factory": object()}

        await asyncio.wait_for(worker.run_agent_chat_job(ctx, "chat_1", None, "hi"), timeout=5)

        types = [e.get("type") for e in redis.events]
        assert types[-2:] == ["cancelled", "done"], types

    async def test_a_stopped_turn_keeps_what_the_agent_had_already_said(
        self, chat_job, monkeypatch
    ):
        """Stop must end the exchange, not destroy it — Retry has to have
        something to retry from."""
        _patch_loop(monkeypatch, _loop_that([
            {"type": "response", "response": "half an answer"},
        ], hang_forever=True))
        redis = _FakeRedis(cancelled_after=1)
        ctx = {"redis": redis, "session_factory": object()}

        await asyncio.wait_for(worker.run_agent_chat_job(ctx, "chat_1", None, "hi"), timeout=5)

        status, messages = chat_job[-1]
        assert status == "cancelled"
        assert messages[-1] == {"role": "assistant", "content": "half an answer"}
        # And the turns from before this one are still there.
        assert messages[0] == {"role": "user", "content": "first"}

    async def test_the_signal_is_cleared_so_the_next_turn_runs(self, chat_job, monkeypatch):
        """A cancel key that outlives its turn stops the following one too."""
        _patch_loop(monkeypatch, _loop_that([{"type": "status", "message": "x"}],
                                            hang_forever=True))
        redis = _FakeRedis(cancelled_after=1)
        redis.keys["synapse:cancel:chat:chat_1"] = "1"
        ctx = {"redis": redis, "session_factory": object()}

        await asyncio.wait_for(worker.run_agent_chat_job(ctx, "chat_1", None, "hi"), timeout=5)

        assert "synapse:cancel:chat:chat_1" not in redis.keys

    async def test_an_uncancelled_turn_finishes_normally(self, chat_job, monkeypatch):
        _patch_loop(monkeypatch, _loop_that([{"type": "response", "response": "done!"}]))
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        result = await worker.run_agent_chat_job(ctx, "chat_1", None, "hi")

        assert result["status"] == "completed"
        assert [e.get("type") for e in redis.events][-1] == "done"
        assert "cancelled" not in [e.get("type") for e in redis.events]
        assert chat_job[-1][0] == "completed"


class TestTheKeyIsOneString:
    async def test_writer_and_reader_agree(self):
        """They agreed by coincidence before, and nothing read it."""
        from core.scale.pubsub import (is_chat_cancelled, publish_chat_cancellation)
        redis = _FakeRedis()
        assert not await is_chat_cancelled(redis, "s1")
        await publish_chat_cancellation(redis, "s1")
        assert await is_chat_cancelled(redis, "s1")

    async def test_a_chat_session_cannot_collide_with_a_run(self):
        """Both ids are opaque strings; sharing a key space is a collision
        waiting for the day someone names a session after a run."""
        from core.scale.pubsub import is_cancelled, publish_chat_cancellation
        redis = _FakeRedis()
        await publish_chat_cancellation(redis, "abc")
        assert not await is_cancelled(redis, "abc")


class TestAFailedTurnKeepsItsConversation:
    async def test_the_history_survives_the_failure(self, chat_job, monkeypatch):
        """B1. This passed `[]`, and `messages` is in the update list — so a
        failure did not merely mark the row, it emptied it. The row was
        described in the code as "the durable record"; it was the opposite."""
        async def exploding_loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "status", "message": "working"}
            raise RuntimeError("the provider fell over")

        _patch_loop(monkeypatch, exploding_loop)
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        with pytest.raises(RuntimeError, match="fell over"):
            await worker.run_agent_chat_job(ctx, "chat_1", None, "hi")

        status, messages = chat_job[-1]
        assert status == "failed"
        assert messages, "a failed turn wiped the whole conversation"
        assert messages[0] == {"role": "user", "content": "first"}
        assert messages[1] == {"role": "assistant", "content": "first answer"}
