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

Writes are synchronous: a flushed append of a bounded line is microseconds,
which is noise next to the LLM/tool latency between events, and it keeps this
module free of extra dependencies. The pump wraps every append in try/except —
a journal failure must never kill a run.
"""
import json
import time
from pathlib import Path

import asyncio

def _events_dir() -> Path:
    """Where a run's event journal is appended.

    Scratch, and deliberately not published to the blob store on close: the
    journal is read by the live-tail endpoint while a run is in flight, and in
    scale mode the Redis stream is already the cross-process answer. Publishing
    it afterwards would be write amplification for data nothing reads.
    """
    from core.storage.scratch import scratch_dir
    return scratch_dir("run_events")

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
        self._fh = None
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
        """Append one event; returns its journal id (0 = skipped by the cap)."""
        etype = event.get("type", "")
        if self._capped and etype not in STRUCTURAL_EVENT_TYPES:
            return 0
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8")
        if not self._capped and self._seq >= MAX_EVENTS_PER_RUN:
            self._capped = True
            if not self._cap_marked:
                self._cap_marked = True
                self._seq += 1
                self._fh.write(json.dumps({
                    "id": self._seq, "ts": time.time(),
                    "event": {"type": "journal_truncated",
                              "message": "event cap reached; only structural events are journaled from here"},
                }) + "\n")
            if etype not in STRUCTURAL_EVENT_TYPES:
                self._fh.flush()
                return 0
        self._seq += 1
        line = json.dumps(
            {"id": self._seq, "ts": time.time(), "event": _shrink(event)},
            default=str,
        )
        self._fh.write(line + "\n")
        self._fh.flush()
        return self._seq

    def read(self, after_id: int = 0, limit: int = 5000) -> list[dict]:
        """Return entries with id > after_id, oldest first, skipping torn lines."""
        if self._fh is not None:
            self._fh.flush()
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
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

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


def close_journal(run_id: str):
    journal = _journals.pop(run_id, None)
    if journal is not None:
        journal.close()


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
