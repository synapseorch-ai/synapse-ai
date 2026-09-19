"""Work a route starts and does not wait for.

`asyncio.create_task(...)` with its result thrown away is a task nothing holds:
the event loop keeps only a weak reference, so it can be garbage-collected
before it finishes, and an exception it raises surfaces — if at all — as "Task
exception was never retrieved" whenever the collector gets to it. Both are ways
for work to end by itself, which is the class F49 is about.

`spawn_background` holds the task until it is done and logs how it failed.
"""
from __future__ import annotations

import asyncio
from typing import Coroutine

from core.log import get_logger

_log = get_logger(__name__)

_running: set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error(f"background task {task.get_name()!r} failed: {exc!r}", exc_info=exc)


def spawn_background(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Start `coro` and keep it alive until it finishes."""
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_finished)
    return task
