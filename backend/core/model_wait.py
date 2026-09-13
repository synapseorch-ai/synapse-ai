"""Waiting on a model, out loud.

A step that calls a model yields nothing until the call returns. That was fine
while calls took seconds, and it is what made a healthy run read as a stuck
one: an agent step's gateway call hung for 187 seconds, the provider timed out
and retried at `debug` level, the retry answered in two — and the run page
showed "thinking · Analyzing your request..." the entire time. The AI builder's
routing step did the same for its full 120-second timeout before failing.

`ModelWait` runs the call as a task and, while it is outstanding, yields what a
watcher should see:

* a `thinking` heartbeat every `HEARTBEAT_SECONDS`, carrying the elapsed time.
  Every renderer treats `thinking` as the live status line rather than a log
  entry, so a long wait updates one line instead of piling up messages.
* a `status` event for each notice the call `report()`s — a provider retry, in
  practice. `status` is written to the run log, because "the model did not
  answer and was retried" is a fact about the run worth keeping.

Notices travel on a context variable set inside the task, so a provider reports
without knowing who, if anyone, is watching: outside a `ModelWait`, `report` is
a no-op.

**Cancellation is the part to be careful with.** The call runs in its own task,
so a step timeout or a user's cancel lands on the waiter, not on the call.
`events()` cancels the task on every exit path; a model call that outlives the
run that asked for it keeps spending after the run was told to stop.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any

#: How often a still-waiting call says so. Well above a typical call (p50 2.5s,
#: p95 13s on managed models), so most calls never emit one.
HEARTBEAT_SECONDS = 15.0

_notices: ContextVar[asyncio.Queue[str] | None] = ContextVar("model_wait_notices", default=None)


def report(message: str) -> None:
    """Tell whoever is waiting on the current model call. No-op if nobody is."""
    queue = _notices.get()
    if queue is not None:
        queue.put_nowait(message)


class ModelWait:
    """Await `awaitable` while yielding heartbeat and notice events.

    Usage, inside an async generator::

        wait = ModelWait(generate_response(...), label="Decide")
        async for event in wait.events():
            yield event
        response = wait.result

    `extra` is merged into every event — an evaluator passes its
    `orch_step_id`, so the router attributes the line to the step.
    """

    def __init__(
        self,
        awaitable: Awaitable[Any],
        *,
        label: str = "",
        extra: dict[str, Any] | None = None,
        heartbeat: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._awaitable = awaitable
        self._clock = clock
        self._label = label
        self._extra = extra or {}
        self._heartbeat = heartbeat
        self.result: Any = None

    def _event(self, kind: str, message: str) -> dict[str, Any]:
        return {"type": kind, "message": message, **self._extra}

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        every = self._heartbeat if self._heartbeat is not None else HEARTBEAT_SECONDS

        async def run() -> Any:
            # Set inside the task, so it is this call's context and nobody else's.
            _notices.set(queue)
            return await self._awaitable

        task = asyncio.ensure_future(run())
        started = self._clock()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=every)
                while not queue.empty():
                    yield self._event("status", queue.get_nowait())
                if done:
                    self.result = task.result()
                    return
                elapsed = int(self._clock() - started)
                prefix = f"{self._label} — " if self._label else ""
                yield self._event("thinking", f"{prefix}waiting on the model, {elapsed}s")
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
