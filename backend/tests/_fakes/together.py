"""Proof that calls ran at the same time, without the wall clock (F519).

The parallel tests used to prove overlap with time windows: three 0.3 s branches
"took under 0.7 s", a branch "started before 0.15 s". A loaded machine — several
sessions' suites at once — stretches every gap, and those windows failed while
the engine was right.

`Together(n)` is a gate the fakes pass through instead. Each call that reaches it
waits until `n` are waiting at once, and then all go; once open it stays open, so
later calls (a fifth branch after the first four, a loop's next iteration) pass
straight through. However slow the machine, the gate opens the moment the n-th
call arrives — and calls that run one after another can never fill it: the first
waits for company that cannot come until it is done. `GIVE_UP` bounds that wait
so a broken engine fails rather than hangs; it is never what a passing run waits
on.
"""
from __future__ import annotations

import asyncio

#: How long a call waits for the others before it gives up and the test fails.
GIVE_UP = 10.0


class Together:
    def __init__(self, n: int) -> None:
        self.n = n
        self.waiting = 0
        self.gave_up = 0
        self.opened = asyncio.Event()

    async def arrive(self) -> None:
        """Wait here until `n` calls are waiting together (or the gate is already open)."""
        if self.opened.is_set():
            return
        self.waiting += 1
        try:
            if self.waiting >= self.n:
                self.opened.set()
                return
            try:
                await asyncio.wait_for(self.opened.wait(), GIVE_UP)
            except asyncio.TimeoutError:
                self.gave_up += 1
        finally:
            self.waiting -= 1

    @property
    def held(self) -> bool:
        """Whether `n` calls were in flight together, with none giving up first."""
        return self.opened.is_set() and not self.gave_up


def through(fake_llm, gate: Together, monkeypatch) -> Together:
    """Send every call of the shared fake LLM through `gate`, before its configured delay."""
    delay = fake_llm._maybe_delay

    async def gated() -> None:
        await gate.arrive()
        await delay()

    monkeypatch.setattr(fake_llm, "_maybe_delay", gated)
    return gate
