"""Waiting on a condition instead of a clock (F519's rule, BF10).

A test that sleeps a fixed time for something to happen fails when the box is loaded: the thing takes longer than
the sleep. `until` waits for the condition itself, polling often, and gives up only after `timeout`, which is a
backstop for a real hang, not the expected time: raising it never makes a test pass that should fail.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable

#: Long enough that a loaded box never reaches it; a hang still fails, with `what` named.
GIVE_UP = 10.0


async def until(predicate: Callable[[], bool | Awaitable[bool]], *, timeout: float = GIVE_UP,
                what: str = "the condition", every: float = 0.01) -> None:
    """Return as soon as `predicate()` (sync or async) is true; fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        held = predicate()
        if inspect.isawaitable(held):
            held = await held
        if held:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not happen within {timeout:g}s")
        await asyncio.sleep(every)
