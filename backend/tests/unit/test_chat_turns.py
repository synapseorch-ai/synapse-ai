"""The chat turn registry (F49): a turn outlives its readers, and ends once."""
from __future__ import annotations

import asyncio

import pytest

from core import chat_turns as ct


async def _events(agen) -> list[dict]:
    out = []
    async for item in agen:
        if isinstance(item, ct.Entry):
            out.append(item.event)
    return out


def _producer(*events, gate: asyncio.Event | None = None, after_gate=()):
    async def gen():
        for ev in events:
            yield ev
        if gate is not None:
            await gate.wait()
        for ev in after_gate:
            yield ev
    return gen()


class TestAReaderIsNotTheTurn:
    async def test_a_reader_leaving_leaves_the_turn_running(self):
        gate = asyncio.Event()
        with ct.claim("s1", message="q") as turn:
            turn.start(_producer({"type": "status"}, gate=gate,
                                 after_gate=[{"type": "response", "content": "a"}]))
        reader = ct.read(turn)
        first = await reader.__anext__()
        assert first.event["type"] == "status"
        await reader.aclose()                       # the tab closes

        assert turn.live
        gate.set()
        await turn.wait()
        assert [e.event["type"] for e in turn.entries] == ["status", "response", "done"]

    async def test_a_late_reader_replays_then_follows(self):
        gate = asyncio.Event()
        with ct.claim("s1") as turn:
            turn.start(_producer({"type": "status"}, {"type": "thinking"}, gate=gate,
                                 after_gate=[{"type": "response", "content": "a"}]))
        await asyncio.sleep(0.01)
        follow = asyncio.create_task(_events(ct.read(turn, after=1)))
        await asyncio.sleep(0.01)
        gate.set()
        assert [e["type"] for e in await follow] == ["thinking", "response", "done"]

    async def test_a_reader_after_the_end_gets_the_ending_then_stops(self):
        with ct.claim("s1") as turn:
            turn.start(_producer({"type": "response", "content": "a"}))
        await turn.wait()
        assert [e["type"] for e in await _events(ct.read(turn))] == ["response", "done"]

    async def test_frames_carry_a_cursor_for_this_turn(self):
        with ct.claim("s1") as turn:
            turn.start(_producer({"type": "status"}))
        await turn.wait()
        framed = ct.frame(turn, turn.entries[0])
        assert framed.startswith(f"id: {turn.id}-1\ndata: ")
        assert ct.parse_cursor(turn, f"{turn.id}-1") == 1
        with pytest.raises(ct.TurnGone):
            ct.parse_cursor(turn, "someoneelse-3")


class TestOneTurnAtATime:
    async def test_a_second_turn_is_refused_while_one_runs(self):
        gate = asyncio.Event()
        with ct.claim("s1") as turn:
            turn.start(_producer(gate=gate))
        with pytest.raises(ct.TurnAlreadyRunning):
            with ct.claim("s1"):
                pass
        gate.set()
        await turn.wait()
        with ct.claim("s1") as nxt:                 # finished: the next one may start
            nxt.start(_producer())
        await nxt.wait()
        assert ct.find("s1") is nxt

    async def test_a_claim_that_never_starts_is_released(self):
        with ct.claim("s1") as done_turn:
            done_turn.start(_producer())
        await done_turn.wait()
        with pytest.raises(RuntimeError):
            with ct.claim("s1"):
                raise RuntimeError("no pending approval")
        assert ct.find("s1") is done_turn           # the finished turn is readable again
        with ct.claim("s1") as turn:
            turn.start(_producer())
        await turn.wait()

    async def test_callers_with_no_session_do_not_block_each_other(self):
        gate = asyncio.Event()
        with ct.claim(None) as a:
            a.start(_producer(gate=gate))
        with ct.claim(None) as b:                   # not refused
            b.start(_producer(gate=gate))
        assert ct.find(a.session_id) is None        # and nothing can reattach to them
        gate.set()
        await asyncio.gather(a.wait(), b.wait())


class TestStopping:
    async def test_a_stopped_turn_ends_once_and_saves_its_question(self):
        gate, saved = asyncio.Event(), []
        with ct.claim("s1", message="q") as turn:
            turn.start(_producer({"type": "status"}, gate=gate))

            async def on_stop():
                saved.append(turn.message)
            turn.on_stop = on_stop
        await asyncio.sleep(0.01)
        assert await ct.stop("s1") is True
        await turn.wait()
        assert [e.event["type"] for e in turn.entries] == ["status", "cancelled", "done"]
        assert saved == ["q"]

    async def test_a_turn_that_answered_is_not_saved_again_when_stopped(self):
        gate, saved = asyncio.Event(), []
        with ct.claim("s1") as turn:
            turn.start(_producer({"type": "response", "content": "a"}, gate=gate))

            async def on_stop():
                saved.append(1)
            turn.on_stop = on_stop
        await asyncio.sleep(0.01)
        await ct.stop("s1")
        await turn.wait()
        assert saved == []

    async def test_a_stop_after_the_end_does_not_reach_the_next_turn(self):
        with ct.claim("s1") as first:
            first.start(_producer({"type": "response", "content": "a"}))
        await first.wait()
        assert await ct.stop("s1") is False         # nothing running: nothing flagged

        with ct.claim("s1") as second:
            second.start(_producer({"type": "response", "content": "b"}))
        await second.wait()
        assert not second.stop_requested
        assert [e.event["type"] for e in second.entries] == ["response", "done"]

    async def test_a_failing_producer_still_ends_the_turn(self):
        async def boom():
            yield {"type": "status"}
            raise RuntimeError("model exploded")
        with ct.claim("s1") as turn:
            turn.start(boom())
        await turn.wait()
        assert [e.event["type"] for e in turn.entries] == ["status", "error", "done"]


class TestTheLogIsBounded:
    async def test_old_entries_go_first_and_a_reader_starts_at_what_is_left(self, monkeypatch):
        monkeypatch.setattr(ct, "MAX_LOG_BYTES", 400)
        with ct.claim("s1") as turn:
            turn.start(_producer(*({"type": "status", "message": "x" * 60} for _ in range(20))))
        await turn.wait()
        assert turn.entries[0].seq > 1
        replay = await _events(ct.read(turn))
        assert replay[-1]["type"] == "done"

    async def test_a_finished_turn_is_forgotten_after_the_retention_window(self, monkeypatch):
        with ct.claim("s1") as turn:
            turn.start(_producer())
        await turn.wait()
        turn.finished_at -= ct.RETAIN_SECONDS + 1
        assert ct.find("s1") is None


class TestSseChunksAreSplitIntoEvents:
    def test_heartbeats_are_dropped_and_frames_kept_verbatim(self):
        chunk = ': ping\n\ndata: {"type": "status", "message": "hi"}\n\n'
        assert list(ct._frames(chunk)) == [
            ({"type": "status", "message": "hi"}, 'data: {"type": "status", "message": "hi"}\n\n'),
        ]
