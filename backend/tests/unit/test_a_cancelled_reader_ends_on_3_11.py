"""A chat turn's reader cancelled as an entry arrives ends, on 3.11 too (F1435; CI2's 3.11 publish gate, 11-06).

3.11's `asyncio.wait_for` returns the inner result when it is cancelled in the same tick the thing it waits on is
done, so the cancel is lost (bpo-42130; 3.12 rebuilt it on `asyncio.timeout`). A loop that ends only by being
cancelled then runs on: knowledge progress's `_flush` forever (test_knowledge_progress's
test_the_wait_names_the_provider_and_until_when hung), and `chat_turns.read` until its turn ends — a closed tab's
reader kept reading. Both wait with `asyncio.timeout`, which keeps the cancel.
"""
from __future__ import annotations

import asyncio

from core import chat_turns as ct


async def test_a_reader_cancelled_as_an_entry_arrives_ends():
    turn = ct.ChatTurn(("u1", "s1"), "s1", message="q", agent_key="a", reattachable=True)
    got: list = []

    async def follow():
        async for item in ct.read(turn, heartbeat=5.0):
            got.append(item)

    reader = asyncio.create_task(follow())
    await asyncio.sleep(0.02)  # it is waiting on the turn

    async def arrive():
        turn.append({"type": "status"})

    await asyncio.create_task(arrive())
    reader.cancel()  # the same tick: the entry is in, the reader not yet woken
    try:
        done, _ = await asyncio.wait({reader}, timeout=1.0)
        assert reader in done and reader.cancelled(), "the reader's cancel was lost; it is still reading"
        assert turn.live
    finally:
        reader.cancel()
