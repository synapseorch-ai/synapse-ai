"""A queue's depth is the jobs due now, not the ones deferred to later (F351, S1c, D181).

arq keeps a deferred job in the queue at its due time. Knowledge arms passes
days ahead (a month's start, a week's re-crawl), and the fleet's alarm and the
knowledge service's scale-from-zero both read this number.
"""
from __future__ import annotations

import asyncio
import time

from core.telemetry import metrics, queue, state


class _Redis:
    def __init__(self, scores: list[float]):
        self.scores = scores

    async def zcard(self, name):
        return len(self.scores)

    async def zcount(self, name, low, high):
        lo = float("-inf") if low == "-inf" else float(low)
        hi = float("inf") if high == "+inf" else float(high)
        return sum(1 for s in self.scores if lo <= s <= hi)


async def test_a_job_deferred_to_later_is_not_depth(monkeypatch):
    monkeypatch.setattr(state, "metering", True)
    seen: dict[str, int] = {}
    monkeypatch.setattr(metrics, "set_queue_depth", lambda q, d: seen.__setitem__(q, d))
    now = time.time() * 1000
    redis = _Redis([now - 5_000, now - 1, now + 7 * 86_400_000])  # one waiting, one running, one a week out
    task = queue.watch_depth(redis, "synapse:orchestrations", interval=3600)
    try:
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert seen == {"synapse:orchestrations": 2}
