"""The fleet's chat job tells the pool what its turn can call (VOX-MCP, D262).

`run_agent_chat_job` acquires the tenant's module before the engine reads the
agent, so it used to get every MCP server the org has. A phone call's agent that
used none of acme's three waited ~10 s for them. The job now works the agent's
needs out first (`mcp_needs.for_turn`) and acquires inside `mcp_pool.needing`.

The acquire is faked to record what it was told and to stop the turn there,
which is all this asserts: the rules themselves are `test_mcp_needs.py`'s.
"""
import pytest

from core.scale import mcp_pool, worker


class _FakeRedis:
    async def xadd(self, *_a, **_kw):
        return None

    async def expire(self, *_a, **_kw):
        return True


class _Stop(Exception):
    pass


@pytest.fixture
def told(monkeypatch):
    seen: list = []

    async def nothing(*_a, **_kw):
        return None

    async def history(*_a, **_kw):
        return []

    async def acquire(*_a, **_kw):
        seen.append(mcp_pool.needed())
        raise _Stop()

    async def resolve(agent_id):
        return {"id": agent_id, "type": "conversational", "tools": ["deepwiki__ask", "google__search"]}

    monkeypatch.setattr(worker, "_load_job_settings", nothing)
    monkeypatch.setattr(worker, "_load_chat_history", history)
    monkeypatch.setattr(worker, "_upsert_chat_session", nothing)
    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr("core.scale.context.resolve_agent", resolve)
    return seen


async def _turn(**kw):
    ctx = {"redis": _FakeRedis(), "session_factory": object()}
    with pytest.raises(_Stop):
        await worker.run_agent_chat_job(ctx, "chat_1", "harbor", "hello", **kw)


async def test_a_members_turn_names_its_agents_tools(told):
    await _turn()
    assert len(told) == 1
    assert {"deepwiki__ask", "google__search"} <= told[0]


async def test_a_visitors_turn_names_only_what_both_allow(told):
    await _turn(visitor={"surface": "channel", "tools": ["google__search", "vercel__deploy"]})
    assert told == [frozenset({"google__search"})]


async def test_the_context_ends_with_the_acquire(told):
    await _turn()
    assert mcp_pool.needed() is None, "the turn's needs leaked into the job's context"
