"""A stopping worker hands its work back instead of cutting it (F424).

A deploy, a scale-in or a host replacement sends the worker SIGTERM. ARQ then
stops taking jobs and gives the running ones `job_completion_wait` (45 s) to
finish before it cancels them — and a cancelled job goes back on the queue at
once, so another worker resumes it. What that wait could not do was stop a run
where it is safe to stop: a run of many steps kept working for the whole wait
and was then cut in the middle of whichever step it had reached, and the next
worker ran that step again from its start — a model call paid twice, a tool
that acts run twice. A chat turn cut the same way said nothing until its next
delivery, and was charged for a reply nobody got.

So the jobs need to know that the worker is stopping, and ARQ does not tell
them: its signal handler is installed on the event loop when the Worker is
built, and nothing a job can reach refers to the Worker. `install()` puts a
Python-level handler in front of the one asyncio left for the signal. That
asyncio handler is a no-op by design (`_sighandler_noop`): the signal reaches
the loop through the wakeup fd, which the interpreter writes for any Python
handler, and the loop then calls ARQ's handler from its own table. Ours runs
first and only sets a flag — it runs between two bytecodes of whatever the
main thread is doing, so it logs nothing and touches nothing else.

What reads the flag:

* `WorkerEngineAdapter` hands a run back before its next top-level step:
  the step that just ended is checkpointed, and the job ends with
  `HandedBack`, which ARQ treats as its own cancel ("will be run again") — the
  claim is dropped and the job is due at once.
* `run_agent_chat_job` tells a turn it was cut at the moment ARQ cuts it,
  instead of on its next delivery.

Installed from a worker's `on_startup`, which ARQ runs after it registered its
loop handlers. Installed earlier, `add_signal_handler` would put asyncio's
no-op back in front of it.
"""
from __future__ import annotations

import asyncio
import signal
import threading
import time

#: When this process was told to stop, or None while it is not stopping.
_since: float | None = None


class HandedBack(asyncio.CancelledError):
    """A run job ending itself because its worker is stopping (F424).

    A `CancelledError` because that is what ARQ requeues at once ("cancelled,
    will be run again"): an `Exception` would be the job failing — the engine's
    jobs dead-letter those — and a bare `BaseException` escapes ARQ's handler
    and stops its poll loop.
    """


def begin() -> None:
    """Mark this process as stopping. Idempotent: the first signal is the one that counts."""
    global _since
    if _since is None:
        _since = time.monotonic()


def draining() -> bool:
    """True once this process has been told to stop."""
    return _since is not None


def reset() -> None:
    """Forget a stop — for tests, where one process plays two workers in turn."""
    global _since
    _since = None


def install() -> bool:
    """Hear SIGTERM and SIGINT ahead of the handler already there. True if installed.

    Only over a handler that is a callable: with `SIG_DFL` or `SIG_IGN` in place,
    or Python's own KeyboardInterrupt handler, nothing asked this process to wind
    down gracefully, and putting ours in front would stop the signal from ending
    it. Twice is once — ARQ's `--watch` runs `on_startup` again.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    installed = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        if getattr(previous, "_hands_back", False):
            installed = True
            continue
        if not callable(previous) or previous is signal.default_int_handler:
            continue

        def chained(signum, frame, _previous=previous):
            begin()
            _previous(signum, frame)

        chained._hands_back = True  # type: ignore[attr-defined]
        signal.signal(sig, chained)
        installed = True
    return installed
