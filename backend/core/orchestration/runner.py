"""Single-task runner for orchestration engine streams.

The engine holds anyio cancel scopes across `yield`s (the per-step timeout at
`engine._execute_loop`). anyio binds a cancel scope to the task that entered
it, so the engine's generator must be advanced -- and closed -- from ONE task,
whatever the HTTP layer does with the events. A consumer that pulls each event
in a fresh task raises "Attempted to exit cancel scope in a different task"
(issue #356).

Every orchestration entry point goes through here, so the launch path can no
longer change how the engine is driven. That makes the pump the single choke
point for run observability: every event is appended to the run's journal
(`journal.FileRunJournal`) and fanned out to live tails (`journal.broker`)
before it reaches the SSE queue. A journal/broker/observer failure must never
kill a run -- all three are best-effort.
"""
import asyncio
import inspect
from typing import AsyncGenerator

from core.orchestration.journal import aclose_journal, broker, get_journal

# Live in-process runs, keyed by run_id. The cancel endpoint cancels the task
# to interrupt an in-progress await; the run-status endpoint consults it to
# report a run that is still inside its first step (no checkpoint yet).
_active_tasks: dict[str, asyncio.Task] = {}

# Queue marker meaning "the engine generator is exhausted".
SENTINEL = object()

# Observers called as fn(run_id, event) for every pumped event (best-effort).
# core.notifications registers here to derive user-facing notifications from
# lifecycle events without the pump knowing about notification semantics.
event_observers: list = []


async def _observe(run_id: str, event: dict):
    """Journal + broadcast + notify for one event. Never raises.

    Async because an observer may need to read run state, which now lives in
    the store rather than on local disk. Observers are awaited in turn rather
    than gathered: they are cheap, and ordering keeps a notification's cause
    ahead of its resolution.
    """
    entry_id = 0
    try:
        entry_id = get_journal(run_id).append(event)
    except Exception as e:
        print(f"WARNING: journal append failed for '{run_id}': {e}", flush=True)
    try:
        broker.publish(run_id, {"id": entry_id, "event": event})
    except Exception:
        pass
    for observer in event_observers:
        try:
            result = observer(run_id, event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass


def spawn_engine_run(agen, run_id: str) -> tuple[asyncio.Task, asyncio.Queue]:
    """Advance `agen` in one dedicated task, returning (task, queue).

    The task is detached: it runs to completion even if nobody drains the
    queue, so a client disconnect never kills a run. Only the cancel endpoint
    stops it.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _pump():
        last_type = None
        try:
            async for event in agen:
                await _observe(run_id, event)
                last_type = event.get("type")
                await queue.put(event)
                # Yield so the SSE consumer can dequeue and flush this event
                # to the HTTP response before the next one is enqueued.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            event = {"type": "orchestration_error", "error": "Cancelled"}
            await _observe(run_id, event)
            last_type = event["type"]
            await queue.put(event)
        except FileNotFoundError:
            event = {"type": "orchestration_error", "error": "Run not found"}
            await _observe(run_id, event)
            last_type = event["type"]
            await queue.put(event)
        except Exception as e:
            event = {"type": "orchestration_error", "error": str(e)}
            await _observe(run_id, event)
            last_type = event["type"]
            await queue.put(event)
        finally:
            # Journal/broker-only terminator (never sent on the POST SSE):
            # tells tailing GET clients whether to stay open for a resume pump
            # (paused on human input) or close (terminal).
            await _observe(run_id, {
                "type": "run_stream_end",
                "paused": last_type == "human_input_required",
            })
            # The run is over once its terminator is observed; closing the
            # journal waits on the writer thread, and nothing that consults
            # the registry should wait with it.
            _active_tasks.pop(run_id, None)
            await aclose_journal(run_id)
            print(f"DEBUG SSE QUEUE: sentinel sent for '{run_id}', stream closing", flush=True)
            await queue.put(SENTINEL)

    task = asyncio.create_task(_pump())
    _active_tasks[run_id] = task
    return task, queue


async def stream_engine_events(agen, run_id: str) -> AsyncGenerator[dict, None]:
    """Yield the engine's event dicts while a dedicated task advances it.

    Deliberately does NOT cancel that task on close: the run is detached, so
    abandoning this generator (client disconnect, GC finalization) must leave
    it running -- the run still checkpoints and stays cancellable and
    resumable.
    """
    _task, queue = spawn_engine_run(agen, run_id)
    while True:
        event = await queue.get()
        if event is SENTINEL:
            return
        yield event
