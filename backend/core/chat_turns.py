"""Chat turns that outlive the request that started them (F49).

A chat turn used to run inside the streaming response that asked for it, so
the reader leaving — a closed tab, a reload, a laptop waking from sleep, an API
caller that stopped reading — cancelled the turn mid-tool and saved nothing.
Leaving is not a request to stop; Stop is its own route.

So a turn is started by the route *handler*, runs in its own task, and writes
every event it produces into an in-memory log with a sequence number. Readers
— the response that started it, and any number of later ones — replay the log
from a cursor and then follow it live. A reader going away ends that reader,
never the turn.

Shape, and why:

* **One live turn per conversation.** A second message while one is running is
  refused (`TurnAlreadyRunning`, a 409 at the routes): two turns on one
  conversation each read the history before the other saved, and the page's old
  fallback re-sent a message whose turn was still running.
* **A finished turn is kept a while** (`RETAIN_SECONDS`) so a reader that lost
  its connection just before the end can still read the ending. That is longer
  than the chat page's 180 s stall watchdog on purpose. The next turn on the
  same conversation replaces it. The answer itself is in the conversation
  store regardless — the log is only how you watch it arrive.
* **The first `done` wins.** A turn ends with exactly one; a Stop that lands
  after the turn has already said `done` adds nothing.
* **The stop flag belongs to the turn**, not to the session id: a Stop that
  arrives after a turn ended used to leave the flag set, and the *next* turn on
  that conversation ended silently at its first boundary.
* **Runs a turn started are its own.** An orchestrator- or builder-type agent
  starts a detached orchestration run; Stop on the turn stops those too.

Keyed by tenant as well as session id: an embedder's tenants share a process.
A caller with no session id gets a private key — nothing can reattach to it or
be refused by it, which is what concurrent session-less API callers need.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterator, Optional

from core.log import get_logger
from core.tenancy import get_tenant

_log = get_logger(__name__)

#: How long a finished turn stays readable. Longer than the chat page's stall
#: watchdog (180 s, `frontend/src/lib/sse.ts`), so a page whose socket died just
#: before the end reattaches to the ending rather than to nothing.
RETAIN_SECONDS = 300

#: The most a turn's log may hold. Older entries are dropped from the front; a
#: reader behind that point starts at the oldest entry still held.
MAX_LOG_BYTES = 4 * 1024 * 1024

#: Finished turns kept across the whole process, newest first.
MAX_RETAINED = 500

#: What a stopped turn's answer is saved as. Never empty: an empty assistant
#: message is refused by some providers when it comes back as history, and
#: silence reads to the model like an answer.
STOPPED_ANSWER = "(Stopped before answering.)"

#: What a turn whose model call failed is saved as (F133). The provider's own
#: words are not: they went back to the model as history, to a widget visitor
#: in its transcript, and out on a channel as the agent's reply. The person in
#: the app sees them as the turn's error; the conversation keeps that the
#: question went unanswered.
FAILED_ANSWER = "(The model failed before answering.)"

HEARTBEAT = ": ping\n\n"


class TurnAlreadyRunning(Exception):
    """This conversation already has a turn running."""

    def __init__(self, turn: "ChatTurn"):
        super().__init__(f"session {turn.session_id!r} has a turn running")
        self.turn = turn


class TurnGone(Exception):
    """A cursor names a turn that is no longer held."""


@dataclass(frozen=True)
class Entry:
    seq: int
    event: dict
    #: The frame exactly as a reader sends it: ``data: {...}\n\n``.
    data: str


#: The turn whose task is running this code. Set by the driver; a task created
#: inside the turn copies it, which is how a run an agent starts finds its turn.
current_turn: contextvars.ContextVar[Optional["ChatTurn"]] = contextvars.ContextVar(
    "synapse_chat_turn", default=None,
)


class ChatTurn:
    def __init__(self, key: tuple[str, str], session_id: str, *,
                 message: str, agent_key: str, reattachable: bool) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.session_id = session_id
        self.message = message
        self.agent_key = agent_key
        self.reattachable = reattachable
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.task: asyncio.Task | None = None
        self.entries: list[Entry] = []
        self.stop_requested = False
        #: Run ids this turn started (an orchestrator or builder agent's run).
        self.runs: set[str] = set()
        #: Called once if the turn is stopped before it answered. Set by the
        #: producer once it knows whose turn this is (`routes/chat.py`).
        self.on_stop: Callable[[], Awaitable[None]] | None = None
        self._next_seq = 1
        self._bytes = 0
        self._ended = False
        self._answered = False
        self._changed = asyncio.Event()

    # ── state ────────────────────────────────────────────────────────────────
    @property
    def live(self) -> bool:
        return self.finished_at is None

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    def cursor(self, seq: int | None = None) -> str:
        return f"{self.id}-{self.last_seq if seq is None else seq}"

    def summary(self) -> dict:
        """What a page needs to show a turn it did not start."""
        return {
            "turn_id": self.id, "message": self.message,
            "started_at": self.started_at, "cursor": self.cursor(0),
        }

    # ── the log ──────────────────────────────────────────────────────────────
    def append(self, event: dict, data: str | None = None) -> None:
        if self._ended:
            return
        if data is None:
            data = f"data: {json.dumps(event, default=str)}\n\n"
        entry = Entry(self._next_seq, event, data)
        self._next_seq += 1
        self.entries.append(entry)
        self._bytes += len(data)
        while self._bytes > MAX_LOG_BYTES and len(self.entries) > 1:
            self._bytes -= len(self.entries.pop(0).data)
        kind = event.get("type")
        if kind == "response":
            self._answered = True
        elif kind == "done":
            self._ended = True
        self._wake()

    def _wake(self) -> None:
        # Swapped rather than cleared: a reader holds the event it is waiting
        # on, and must be woken by this append even if another append follows
        # before it runs.
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def after(self, seq: int) -> list[Entry]:
        return [e for e in self.entries if e.seq > seq]

    # ── running ──────────────────────────────────────────────────────────────
    def start(self, producer: AsyncIterator) -> None:
        """Drive `producer` in a task of its own. Call from the route handler."""
        if self.task is not None:
            raise RuntimeError("a turn is started once")
        self.task = asyncio.create_task(_drive(self, producer), name=f"chat-turn-{self.id}")

    async def wait(self) -> None:
        """Until the turn has finished. Cancelling the waiter leaves the turn."""
        if self.task is not None:
            await asyncio.shield(self.task)


_TURNS: dict[tuple[str, str], ChatTurn] = {}


def _key(session_id: str) -> tuple[str, str]:
    return (get_tenant() or "", session_id)


def _sweep() -> None:
    now = time.time()
    finished = [t for t in _TURNS.values() if not t.live]
    for turn in finished:
        if now - (turn.finished_at or now) > RETAIN_SECONDS:
            _TURNS.pop(turn.key, None)
    finished = sorted((t for t in _TURNS.values() if not t.live),
                      key=lambda t: t.finished_at or 0, reverse=True)
    for turn in finished[MAX_RETAINED:]:
        _TURNS.pop(turn.key, None)


@contextlib.contextmanager
def claim(session_id: str | None, *, message: str = "", agent_key: str = "default") -> Iterator[ChatTurn]:
    """Reserve the conversation for a turn; the body must `start()` it.

    Raises `TurnAlreadyRunning` when a turn is live. A claim whose body raises,
    or ends without starting the turn, is released — so a route can claim,
    then check something (a pending approval), then refuse, without leaving
    the conversation locked.
    """
    _sweep()
    reattachable = bool(session_id)
    if not session_id:
        session_id = f"_anon:{uuid.uuid4().hex}"
    key = _key(session_id)
    current = _TURNS.get(key)
    if current is not None and current.live:
        raise TurnAlreadyRunning(current)
    turn = ChatTurn(key, session_id, message=message, agent_key=agent_key,
                    reattachable=reattachable)
    _TURNS[key] = turn
    try:
        yield turn
    finally:
        if turn.task is None and _TURNS.get(key) is turn:
            _TURNS.pop(key, None)
            if current is not None:
                _TURNS[key] = current  # the finished turn it displaced stays readable


def live(session_id: str) -> ChatTurn | None:
    turn = _TURNS.get(_key(session_id))
    return turn if turn is not None and turn.live and turn.task is not None else None


def find(session_id: str) -> ChatTurn | None:
    """The live turn, or the last finished one while it is still held."""
    _sweep()
    turn = _TURNS.get(_key(session_id))
    if turn is None or not turn.reattachable or turn.task is None:
        return None
    return turn


async def stop(session_id: str) -> bool:
    """Stop this conversation's live turn, and any run it started.

    False when nothing was running — not an error: the turn may simply have
    finished first.
    """
    turn = live(session_id)
    if turn is None:
        return False
    if not turn.stop_requested:
        turn.stop_requested = True
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()
        for run_id in list(turn.runs):
            try:
                from core.orchestration.cancel import cancel_run
                await cancel_run(run_id)
            except Exception as exc:  # noqa: BLE001 — the turn is stopped either way
                _log.warning(f"stopping run {run_id} of chat turn {turn.id}: {exc}")
    return True


def note_run(run_id: str) -> None:
    """Record that the current turn started this run (so Stop reaches it)."""
    turn = current_turn.get()
    if turn is not None and turn.live:
        turn.runs.add(run_id)


def parse_cursor(turn: ChatTurn, cursor: str | None) -> int:
    """The sequence number to read after. Raises `TurnGone` for another turn's."""
    if not cursor or cursor == "0":
        return 0
    turn_id, _, seq = cursor.rpartition("-")
    if turn_id != turn.id:
        raise TurnGone(cursor)
    try:
        return max(0, int(seq))
    except ValueError:
        raise TurnGone(cursor) from None


async def read(turn: ChatTurn, after: int = 0, *,
               heartbeat: float = 10.0) -> AsyncIterator[Entry | str]:
    """Replay the log after `after`, then follow it until the turn has ended.

    Yields `Entry`s and, when nothing arrives for `heartbeat` seconds, the
    `HEARTBEAT` comment. Returns once the turn has finished and every entry
    has been read. Closing this generator affects nobody else.
    """
    seq = after
    while True:
        changed = turn._changed
        for entry in turn.after(seq):
            yield entry
            seq = entry.seq
        if not turn.live and seq >= turn.last_seq:
            return
        try:
            await asyncio.wait_for(changed.wait(), heartbeat)
        except asyncio.TimeoutError:
            yield HEARTBEAT


def frame(turn: ChatTurn, entry: Entry) -> str:
    """An entry as sent to a reader that can reattach: with its cursor."""
    return f"id: {turn.cursor(entry.seq)}\n{entry.data}"


def _frames(chunk) -> Iterator[tuple[dict, str]]:
    """The events in one chunk a producer yielded (a dict, or SSE text)."""
    if isinstance(chunk, dict):
        yield chunk, f"data: {json.dumps(chunk, default=str)}\n\n"
        return
    if isinstance(chunk, bytes):
        chunk = chunk.decode()
    for block in str(chunk).split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue  # a heartbeat — readers send their own
        lines = [ln[6:] for ln in block.split("\n") if ln.startswith("data: ")]
        if not lines:
            continue
        payload = "\n".join(lines)
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event, f"data: {payload}\n\n"


async def _drive(turn: ChatTurn, producer: AsyncIterator) -> None:
    token = current_turn.set(turn)
    try:
        async for chunk in producer:
            for event, data in _frames(chunk):
                turn.append(event, data)
        if not turn._ended:
            turn.append({"type": "done"})
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        if not turn._ended:
            turn.append({"type": "cancelled", "message": "Stopped."})
            turn.append({"type": "done"})
            if not turn._answered and turn.on_stop is not None:
                try:
                    await turn.on_stop()
                except Exception as exc:  # noqa: BLE001
                    _log.warning(f"saving stopped chat turn {turn.id}: {exc}")
    except Exception as exc:  # noqa: BLE001 — a turn always ends with an event
        _log.exception(f"chat turn {turn.id} failed")
        if not turn._ended:
            turn.append({"type": "error", "message": str(exc)})
            turn.append({"type": "done"})
    finally:
        turn.finished_at = time.time()
        turn._wake()
        current_turn.reset(token)
