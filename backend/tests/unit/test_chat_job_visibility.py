"""A chat turn that dies in setup must leave a trace, and must say so.

Two defects with one cause, both backlog items (10a-iii and 10a-ii).

`run_agent_chat_job` used to acquire the MCP pool *first* and write its
`chat_sessions` row second. `mcp_pool.acquire()` builds the tenant's MCP
sessions, and a remote server that accepts a connection and then goes quiet
holds it — so a wedged turn wrote no row and published no event, for up to
`job_timeout` (an hour). It happened: acme's job `25698d0f` sat in-progress for
forty minutes on 2026-08-25 with zero `chat_%` rows for the org, and the only
trace anywhere was `j_ongoing` in a health line.

The `ChatEventPublisher` was built on the line *after* the acquire too, which is
the second half: the `except` had nothing to publish through, so it marked the
row `failed` and re-raised in silence. The API kept sending keepalives because
it was still waiting for `done`, and the browser spun forever.

So the order is the contract, and it is asserted here rather than left to
whoever next edits the function — the failing shape looks entirely reasonable
when you read it.
"""
import pytest

from core.scale import mcp_pool, worker


class _FakeRedis:
    """Records what reached the stream, in order."""

    def __init__(self):
        self.events: list[dict] = []

    async def xadd(self, key, fields, **_kw):
        import json

        self.events.append(json.loads(fields["data"]))

    async def expire(self, *_a, **_kw):
        return True


@pytest.fixture
def wedged_pool(monkeypatch):
    """Everything external stubbed, and a pool acquire that fails.

    Returns the ordered call log, so "before" is a real assertion about
    sequence rather than about which lines happen to appear in the file.
    """
    calls: list[str] = []

    async def fake_load_job_settings():
        return None

    async def fake_history(session_factory, session_id, agent_id):
        calls.append("history")
        return []

    async def fake_upsert(session_factory, session_id, agent_id, status, messages):
        calls.append(f"upsert:{status}")

    async def fake_acquire(*_a, **_kw):
        calls.append("acquire")
        raise RuntimeError("the pool is wedged")

    async def fake_release(*_a, **_kw):
        calls.append("release")

    monkeypatch.setattr(worker, "_load_job_settings", fake_load_job_settings)
    monkeypatch.setattr(worker, "_load_chat_history", fake_history)
    monkeypatch.setattr(worker, "_upsert_chat_session", fake_upsert)
    monkeypatch.setattr(mcp_pool, "acquire", fake_acquire)
    monkeypatch.setattr(mcp_pool, "release", fake_release)
    return calls


class TestAWedgedTurnIsVisible:
    async def test_the_session_row_is_written_before_the_pool_is_acquired(
        self, wedged_pool
    ):
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        with pytest.raises(RuntimeError, match="wedged"):
            await worker.run_agent_chat_job(ctx, "chat_1", None, "hello")

        assert "upsert:running" in wedged_pool, (
            "no chat_sessions row was written — a turn wedged in the pool is "
            "invisible to a staff console and to a support reply (10a-iii)"
        )
        assert wedged_pool.index("upsert:running") < wedged_pool.index("acquire"), (
            f"the row is still written after the acquire: {wedged_pool}"
        )

    async def test_the_pool_is_not_leased_when_the_acquire_itself_failed(
        self, wedged_pool
    ):
        """`leased` is set after the acquire returns, so a failed acquire must
        not release a lease it never took."""
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        with pytest.raises(RuntimeError):
            await worker.run_agent_chat_job(ctx, "chat_1", None, "hello")

        assert "release" not in wedged_pool


class TestAFailedTurnSaysSo:
    async def test_it_publishes_an_error_and_a_done(self, wedged_pool):
        """The stream must close, or the browser waits for a `done` that is
        never coming."""
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        with pytest.raises(RuntimeError):
            await worker.run_agent_chat_job(ctx, "chat_1", None, "hello")

        types = [e.get("type") for e in redis.events]
        assert types == ["error", "done"], types
        assert "wedged" in redis.events[0]["message"]

    async def test_the_row_still_goes_to_failed(self, wedged_pool):
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        with pytest.raises(RuntimeError):
            await worker.run_agent_chat_job(ctx, "chat_1", None, "hello")

        assert "upsert:failed" in wedged_pool

    async def test_a_dead_redis_does_not_replace_the_real_error(
        self, wedged_pool, monkeypatch
    ):
        """Publishing is best-effort. The durable `failed` row is the record,
        and a Redis that is already gone must not mask why the turn died."""

        class _DeadRedis(_FakeRedis):
            async def xadd(self, *_a, **_kw):
                raise ConnectionError("redis is away")

        ctx = {"redis": _DeadRedis(), "session_factory": object()}

        with pytest.raises(RuntimeError, match="wedged"):
            await worker.run_agent_chat_job(ctx, "chat_1", None, "hello")

        assert "upsert:failed" in wedged_pool
