"""Append-only per-run event journal + in-process broker for live tailing.

Every event the engine emits for a run is journaled as one JSONL line in
`backend/logs/orchestration_events/{run_id}.jsonl`:

    {"id": <monotonic seq>, "ts": <epoch seconds>, "event": {...}}

The id is embedded in the line (not derived from the byte offset) so a torn
final line — a crash mid-write — costs at most that one event: `read()` skips
unparseable lines, and the next pump recovers the sequence from the last valid
line and continues it. Run → resume → human-input pumps for the same run share
one journal instance (see `get_journal`), so ids stay monotonic across the
whole lifetime of a run.

This is the standalone-mode counterpart of the Redis Stream published by scale
workers (`core.scale.pubsub.RunEventPublisher`): the read endpoints in
`routes/orchestrations.py` serve whichever of the two exists for a run.

Builder flows (`builder_*` run ids) are intentionally NOT journaled: they are
ephemeral UI conversations driven by `routes/builder.py`, which bypasses the
runner pump on purpose.

`append` is synchronous and returns the event's id at once — the broker needs
it — but the file write happens on a single writer thread shared by every
journal in the process. One thread, so writes land in submission order; off
the loop, so a `write` + `flush` per event is not charged to whichever
tenant's run happens to be executing. The line is serialised on the loop,
once: the terminal events reference the run's live shared_state, and a thread
reading that dict while the engine can still touch it is a race.

The pump wraps every append in try/except — a journal failure must never kill
a run — and a write that fails on the writer thread is logged, not raised.
"""
import asyncio
import json
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from core.log import get_logger

_log = get_logger(__name__)

_writer: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    """The one writer thread. Single so FIFO holds: the default executor has
    several workers and two appends could land out of order."""
    global _writer
    if _writer is None:
        _writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synapse-journal")
    return _writer


_dir_cache: dict[tuple, Path] = {}


def _events_dir() -> Path:
    """Where a run's event journal is appended.

    Scratch, and deliberately not published to the blob store on close: the
    journal is read by the live-tail endpoint while a run is in flight, and in
    scale mode the Redis stream is already the cross-process answer. Publishing
    it afterwards would be write amplification for data nothing reads.

    Cached per tenant and per configured location: `scratch_dir` does a
    `mkdir` on every call, and this used to be called for every event.
    """
    from core.storage.scratch import scratch_dir
    from core.tenancy import get_tenant
    key = (get_tenant(), os.getenv("SYNAPSE_SCRATCH_DIR"), os.getenv("SYNAPSE_BLOB_DIR"))
    path = _dir_cache.get(key)
    if path is None:
        path = scratch_dir("run_events")
        _dir_cache[key] = path
    return path

# A single serialized event line larger than this gets its heavy fields
# truncated (the UI fetches full run state from the checkpoint endpoint).
MAX_EVENT_BYTES = 32 * 1024

# Keys that carry potentially unbounded payloads, shrunk deepest-first when a
# line exceeds MAX_EVENT_BYTES.
_HEAVY_KEYS = ("final_state", "shared_state", "data", "preview", "response",
               "reasoning", "thought", "output", "result", "agent_context")

# After this many events a run journals only structural events — enough to
# reconstruct the run's shape without letting a chatty agent grow the file
# unboundedly.
MAX_EVENTS_PER_RUN = 20_000

STRUCTURAL_EVENT_TYPES = frozenset({
    "orchestration_start", "step_start", "step_complete", "step_error",
    "loop_iteration", "loop_complete", "loop_limit_reached",
    "parallel_start", "parallel_complete", "branch_start", "merge_complete",
    "routing_decision", "if_decision", "switch_decision",
    "human_input_required", "orchestration_complete", "orchestration_error",
    "orchestration_end", "final", "run_stream_end", "journal_truncated",
    # A task list is the run's shape: past the event cap a long run must keep
    # its plan exactly when the plan matters most.
    "tasks_updated", "tasks_incomplete",
})


def _truncate_strings(value, limit: int):
    """Recursively cap every string in `value` to `limit` chars."""
    if isinstance(value, str):
        return value[:limit] + "…[truncated]" if len(value) > limit else value
    if isinstance(value, dict):
        return {k: _truncate_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(v, limit) for v in value]
    return value


def _serialise(seq: int, ts: float, event: dict) -> str:
    """The journal line for `event`, bounded to roughly MAX_EVENT_BYTES.

    One `json.dumps` in the common case. The old path serialised to measure,
    serialised again if oversized, and then serialised the wrapped entry — three
    times per event, on the loop.
    """
    try:
        line = json.dumps({"id": seq, "ts": ts, "event": event}, default=str)
    except Exception:
        return json.dumps({"id": seq, "ts": ts, "event": {
            "type": event.get("type", "unknown"), "truncated": True,
            "error": "unserializable event"}})
    if len(line) <= MAX_EVENT_BYTES:
        return line
    return json.dumps({"id": seq, "ts": ts, "event": _shrink(event)}, default=str)


def _shrink(event: dict) -> dict:
    """Bound an event to roughly MAX_EVENT_BYTES by truncating heavy fields.

    The full payload still reaches the live SSE consumer untouched — only the
    journaled copy is shrunk, and it is marked `"truncated": true` so replay
    UIs can say so.
    """
    try:
        if len(json.dumps(event, default=str)) <= MAX_EVENT_BYTES:
            return event
    except Exception:
        return {"type": event.get("type", "unknown"), "truncated": True,
                "error": "unserializable event"}
    shrunk = dict(event)
    for key in _HEAVY_KEYS:
        if key in shrunk:
            shrunk[key] = _truncate_strings(shrunk[key], 2_000)
    shrunk["truncated"] = True
    # Last resort: whole-event string cap if still oversized (e.g. many
    # unlisted large fields).
    if len(json.dumps(shrunk, default=str)) > MAX_EVENT_BYTES:
        shrunk = {"type": event.get("type", "unknown"), "truncated": True}
    return shrunk


class FileRunJournal:
    """JSONL journal for one run. Instances are shared via `get_journal`."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._path = _events_dir() / f"{run_id}.jsonl"
        self._fh = None            # opened, written and closed on the writer thread only
        self._pending: Future | None = None
        self._seq = self._recover_seq()
        self._capped = self._seq >= MAX_EVENTS_PER_RUN
        self._cap_marked = self._capped

    @property
    def path(self) -> Path:
        return self._path

    def _recover_seq(self) -> int:
        """Continue the id sequence from the last valid line of the file."""
        if not self._path.exists():
            return 0
        try:
            with open(self._path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return 0
        for line in reversed(tail.splitlines()):
            try:
                entry = json.loads(line)
                return int(entry["id"])
            except Exception:
                continue  # torn/partial line — keep scanning backwards
        return 0

    def append(self, event: dict) -> int:
        """Append one event; returns its journal id (0 = skipped by the cap).

        The id is assigned here, synchronously; the line is queued for the
        writer thread. `event` is serialised before this returns, so a caller
        that mutates it afterwards changes nothing in the journal.
        """
        etype = event.get("type", "")
        if self._capped and etype not in STRUCTURAL_EVENT_TYPES:
            return 0
        if not self._capped and self._seq >= MAX_EVENTS_PER_RUN:
            self._capped = True
            if not self._cap_marked:
                self._cap_marked = True
                self._seq += 1
                self._submit(self._write_line, json.dumps({
                    "id": self._seq, "ts": time.time(),
                    "event": {"type": "journal_truncated",
                              "message": "event cap reached; only structural events are journaled from here"},
                }))
            if etype not in STRUCTURAL_EVENT_TYPES:
                return 0
        self._seq += 1
        self._submit(self._write_line, _serialise(self._seq, time.time(), event))
        return self._seq

    # ── writer thread ────────────────────────────────────────────────

    def _submit(self, fn, *args) -> Future:
        def guarded():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 — never raise on the writer thread
                _log.warning(f"journal write failed for '{self.run_id}': {e}")
        self._pending = _executor().submit(guarded)
        return self._pending

    def _write_line(self, line: str) -> None:
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8")
        self._fh.write(line + "\n")
        self._fh.flush()

    def _flush_fh(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def _close_fh(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    async def drain(self) -> None:
        """Wait until every line appended so far is on disk.

        A barrier on the single writer thread: when it runs, everything
        submitted before it has run too.
        """
        if self._pending is None:
            return
        await asyncio.get_running_loop().run_in_executor(_executor(), self._flush_fh)

    def _drain_sync(self) -> None:
        if self._pending is not None:
            _executor().submit(self._flush_fh).result()

    def read(self, after_id: int = 0, limit: int = 5000) -> list[dict]:
        """Return entries with id > after_id, oldest first, skipping torn lines.

        Synchronous, for the throwaway readers the routes build and for tests.
        On an instance that has appended, it waits for those appends first —
        a route reading a live run's registry instance should `await drain()`
        instead (see routes/orchestrations.py).
        """
        self._drain_sync()
        if not self._path.exists():
            return []
        entries = []
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue  # torn line (crash mid-write)
                    if entry.get("id", 0) > after_id:
                        entries.append(entry)
                        if len(entries) >= limit:
                            break
        except OSError:
            return []
        return entries

    def last_id(self) -> int:
        return self._seq

    def close(self):
        """Close the file after every queued write has landed. Blocks the
        caller for that long — microseconds at a run's end — so the pump uses
        `aclose`; this is for synchronous callers (delete, the startup sweep,
        tests)."""
        if self._pending is None and self._fh is None:
            return
        _executor().submit(self._close_fh).result()
        self._pending = None

    async def aclose(self) -> None:
        if self._pending is None and self._fh is None:
            return
        await asyncio.get_running_loop().run_in_executor(_executor(), self._close_fh)
        self._pending = None

    @classmethod
    def exists(cls, run_id: str) -> bool:
        return (_events_dir() / f"{run_id}.jsonl").exists()

    @classmethod
    def delete(cls, run_id: str) -> bool:
        close_journal(run_id)
        path = _events_dir() / f"{run_id}.jsonl"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


# ── Per-run journal registry ─────────────────────────────────────────
# Run/resume/human-input pumps for one run share an instance so the id
# sequence and file handle are continuous. Evicted when the pump ends.

_journals: dict[str, FileRunJournal] = {}


def get_journal(run_id: str) -> FileRunJournal:
    journal = _journals.get(run_id)
    if journal is None:
        journal = FileRunJournal(run_id)
        _journals[run_id] = journal
    return journal


def open_journal(run_id: str) -> "FileRunJournal | None":
    """The registry instance for a run still being pumped, or None. A reader
    that finds one should `await drain()` on it before opening its own."""
    return _journals.get(run_id)


def close_journal(run_id: str):
    journal = _journals.pop(run_id, None)
    if journal is not None:
        journal.close()


async def aclose_journal(run_id: str) -> None:
    """`close_journal` for the pump: waits for the writer without blocking the loop."""
    journal = _journals.pop(run_id, None)
    if journal is not None:
        await journal.aclose()


class RunEventBroker:
    """In-process pub/sub keyed by run_id, for live-tailing SSE endpoints.

    Subscribers get `{"id": n, "event": {...}}` entries. Queues are unbounded:
    entries are small (journal-shrunk) and subscribers are SSE writers that
    drain continuously; dropping events would break the replay→live dedupe
    contract (monotonic ids with no gaps after the subscription point).
    """

    def __init__(self):
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def publish(self, run_id: str, entry: dict):
        for queue in self._subs.get(run_id, ()):
            queue.put_nowait(entry)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue):
        subs = self._subs.get(run_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._subs.pop(run_id, None)


broker = RunEventBroker()
