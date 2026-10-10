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

**Answer deltas (U72).** A caller that streams the answer passes `deltas=True`.
A provider then asks `streaming_wanted()`, streams its call, and hands each
piece of text to `emit_delta()` (from the loop or from a worker thread), and
`restart_stream()` when it sends a call again after text already went out. The
waiter wakes on each one and yields what arrived since its last wake as a
single private `_model_delta` event (or `_model_restart`); the engine decides
what of it is the answer. With `deltas=False` — every caller but a streaming
top-level turn — `streaming_wanted()` is False inside the call, so a provider
makes the call it always made.

**Cancellation is the part to be careful with.** The call runs in its own task,
so a step timeout or a user's cancel lands on the waiter, not on the call.
`events()` cancels the task on every exit path; a model call that outlives the
run that asked for it keeps spending after the run was told to stop.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
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


class _DeltaSink:
    """Where a streaming call's text goes, for the `ModelWait` that asked for it.

    Pushes may come from the loop or from a provider's worker thread (the
    synchronous SDKs stream there); both land on the loop, in order.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread = threading.get_ident()
        self._items: list[str | None] = []   # None: the call started over
        self._dirty = False                  # text pushed since the last restart
        self.wake = asyncio.Event()

    def push(self, item: str | None) -> None:
        if item is None:
            if not self._dirty:
                return   # nothing went out to take back
            self._dirty = False
        else:
            self._dirty = True
        if threading.get_ident() == self._thread:
            self._push(item)
        else:
            self._loop.call_soon_threadsafe(self._push, item)

    def _push(self, item: str | None) -> None:
        self._items.append(item)
        self.wake.set()

    def drain(self) -> list[dict[str, Any]]:
        """What arrived since the last drain: text merged, a restart kept in its place."""
        self.wake.clear()
        items, self._items = self._items, []
        events: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                events.append({"type": "_model_restart"})
            elif events and events[-1]["type"] == "_model_delta":
                events[-1]["text"] += item
            else:
                events.append({"type": "_model_delta", "text": item})
        return events


_deltas: ContextVar[_DeltaSink | None] = ContextVar("model_wait_deltas", default=None)


def streaming_wanted() -> bool:
    """True inside a model call whose waiter streams the answer (U72)."""
    return _deltas.get() is not None


def emit_delta(text: str) -> None:
    """Hand a piece of the call's text to whoever streams it. No-op if nobody does."""
    sink = _deltas.get()
    if sink is not None and text:
        sink.push(text)


def restart_stream() -> None:
    """Say the call is being sent again: what it streamed so far is void.

    Safe to call before every attempt: it says something only when text went
    out since the last restart.
    """
    sink = _deltas.get()
    if sink is not None:
        sink.push(None)


class ModelWait:
    """Await `awaitable` while yielding heartbeat and notice events.

    Usage, inside an async generator::

        wait = ModelWait(generate_response(...), label="Decide")
        async for event in wait.events():
            yield event
        response = wait.result

    `extra` is merged into every event — an evaluator passes its
    `orch_step_id`, so the router attributes the line to the step.

    `deltas=True` also yields the call's streamed text, as private
    `_model_delta` / `_model_restart` events (see the module docstring).
    """

    def __init__(
        self,
        awaitable: Awaitable[Any],
        *,
        label: str = "",
        extra: dict[str, Any] | None = None,
        heartbeat: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        deltas: bool = False,
    ) -> None:
        self._awaitable = awaitable
        self._clock = clock
        self._label = label
        self._extra = extra or {}
        self._heartbeat = heartbeat
        self._deltas = deltas
        self.result: Any = None

    def _event(self, kind: str, message: str) -> dict[str, Any]:
        return {"type": kind, "message": message, **self._extra}

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        every = self._heartbeat if self._heartbeat is not None else HEARTBEAT_SECONDS
        loop = asyncio.get_running_loop()
        sink = _DeltaSink(loop) if self._deltas else None

        async def run() -> Any:
            # Set inside the task, so it is this call's context and nobody else's.
            # The sink is always set — to None when this waiter does not stream —
            # so a call nested in this one never streams into an outer waiter.
            _notices.set(queue)
            _deltas.set(sink)
            return await self._awaitable

        task = asyncio.ensure_future(run())
        started = self._clock()
        quiet_since = loop.time()
        wake: asyncio.Future | None = None
        try:
            while True:
                waiting: set[asyncio.Future] = {task}
                if sink is not None:
                    if wake is None or wake.done():
                        wake = asyncio.ensure_future(sink.wake.wait())
                    waiting.add(wake)
                timeout = max(0.0, every - (loop.time() - quiet_since))
                done, _ = await asyncio.wait(waiting, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                while not queue.empty():
                    yield self._event("status", queue.get_nowait())
                if sink is not None:
                    streamed = sink.drain()
                    if streamed:
                        # Text arriving is the call alive: no heartbeat over it.
                        quiet_since = loop.time()
                    for event in streamed:
                        yield event
                if task in done:
                    self.result = task.result()
                    return
                if loop.time() - quiet_since < every:
                    continue
                quiet_since = loop.time()
                elapsed = int(self._clock() - started)
                prefix = f"{self._label} — " if self._label else ""
                yield self._event("thinking", f"{prefix}waiting on the model, {elapsed}s")
        finally:
            if wake is not None and not wake.done():
                wake.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
