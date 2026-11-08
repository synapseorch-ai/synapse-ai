"""Two writers on one conversation keep both turns (F475, VOX1e, D263; VOX1f, D273).

Measured live on call `CAf597c2…`: a phone call's turn and the fleet's resume of
its approval both wrote the call's conversation within two seconds, and the
call's turn was gone. Every writer read the row, changed the list in Python and
wrote it back with no lock, so the later writer kept only what it had read, and
the fleet's chat job wrote back the whole list it loaded when the turn began.
Not voice-only: any conversation two processes write (an approval resume beside
a live chat, a channel, a schedule).

The writers here are made to read before either writes, the interleaving that
loses a turn: each waits after its read until the other has read too, or a
moment has passed (a writer that holds the conversation alone never lets the
other read first, so it waits out the moment and goes on).
"""
import asyncio
import contextlib

from core.store import sessions as store

S, A = "f475-two-writers", "agent_x"


def _turn(n: int) -> list[dict]:
    return [{"role": "user", "content": f"q{n}"}, {"role": "assistant", "content": f"a{n}"}]


def _read_together(monkeypatch, *, readers: int = 2, wait: float = 0.3):
    """Every store read waits, after reading, for `readers` reads or `wait` seconds."""
    real = store._row
    seen = {"n": 0}
    all_read = asyncio.Event()

    async def row(s, session_id, agent_id, **kw):
        found = await real(s, session_id, agent_id, **kw)
        seen["n"] += 1
        if seen["n"] >= readers:
            all_read.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(all_read.wait(), wait)
        return found
    monkeypatch.setattr(store, "_row", row)


class TestTheStore:
    async def test_two_turns_appended_at_once_are_both_kept(self, monkeypatch):
        await store.append_turn(S, A, _turn(0))
        _read_together(monkeypatch)
        await asyncio.gather(store.append_turn(S, A, _turn(1)), store.append_turn(S, A, _turn(2)))
        said = [m["content"] for m in (await store.get(S, A))["messages"]]
        assert said[:2] == ["q0", "a0"]
        assert sorted(said[2:]) == ["a1", "a2", "q1", "q2"], f"a turn was lost: {said}"

    async def test_two_first_turns_at_once_are_both_kept(self, monkeypatch):
        _read_together(monkeypatch)
        await asyncio.gather(store.append_turn(S, A, _turn(1)), store.append_turn(S, A, _turn(2)))
        said = [m["content"] for m in (await store.get(S, A))["messages"]]
        assert sorted(said) == ["a1", "a2", "q1", "q2"], f"a turn was lost: {said}"

    async def test_a_parked_call_taken_twice_at_once_is_taken_once(self, monkeypatch):
        """F247's promise ("two turns starting at once cannot both settle the same question") was one
        transaction and no lock, so both settled it."""
        await store.set_pending_approval(S, A, {"approval_id": "ap_1", "tool_name": "refund"})
        _read_together(monkeypatch)
        taken = await asyncio.gather(store.take_pending_approval(S, A), store.take_pending_approval(S, A))
        assert sorted(bool(t) for t in taken) == [False, True]

    async def test_an_answer_cut_beside_a_new_turn_keeps_both(self, monkeypatch):
        """The call's own writes (a barge-in's cut) beside another writer's turn."""
        await store.append_turn(S, A, _turn(0))
        _read_together(monkeypatch)
        await asyncio.gather(store.cut_last_answer(S, A, "a"), store.append_turn(S, A, _turn(1)))
        messages = (await store.get(S, A))["messages"]
        assert [m["content"] for m in messages if m["role"] == "user"] == ["q0", "q1"]
        assert messages[1].get("interrupted") is True, f"the cut was lost: {messages}"


class TestTheFleetsChatJob:
    """The fleet's chat job loads the conversation when the turn starts and saves it when it ends; another
    writer's turn in between (the call's own, while the fleet resumed its approval) was written over."""

    async def test_a_turn_written_while_it_ran_is_kept(self, monkeypatch):
        import fakeredis.aioredis

        import core.react_engine as re_mod
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        from core.store.engine import get_store

        await store.append_turn(S, A, _turn(0))

        async def loop(request, server_module, should_stop=None, **_kw):
            # While the fleet's turn runs, the live call saves one of its own.
            await store.append_turn(S, A, _turn(1))
            yield {"type": "final", "response": "the fleet's answer"}

        async def nothing(*_a, **_kw):
            return None

        async def got(*_a, **_kw):
            return object()
        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(mcp_pool, "acquire", got)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        await worker_mod.run_agent_chat_job(
            {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": await get_store(), "job_try": 1,
             "job_id": "f475"}, S, A, "the fleet's question")
        said = [m["content"] for m in (await store.get(S, A))["messages"]]
        assert said == ["q0", "a0", "q1", "a1", "the fleet's question", "the fleet's answer"], said
