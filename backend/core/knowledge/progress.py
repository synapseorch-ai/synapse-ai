"""How far Knowledge's work has got, for a screen to show (PG1, F357; CCR-12).

A working document — and a rebuild's current one — reports its stage, how much of it is done in the stage's own
unit, an estimate of the time left and any rate limit it is waiting out. The deep stages report without being
handed anything: the job opens `tracking(...)`, which puts a `Tracker` in a context variable, and each stage asks
`current()`:

    reading     the parse runner, from the child's page counts (`parse/runner.py`)   pages
    ocr         `parse/ocr.py`, per batch                                              pages
    contexts    `enrich.contextualize`, per passage; its pacer's waits                 passages
    embedding   the pipeline per batch, the embedder per slice; the embedder's waits   passages
    indexing    the pipeline (one write: it cannot count)                              —

**Nothing is written from a hot loop.** A tracker's methods only change memory. `tracking` owns one task that
writes the row: at once when the stage changes or a wait starts or ends, counts at most every `FLUSH_EVERY_S`,
and a keepalive every `KEEPALIVE_S`. A write that fails is logged and never fails the work.

**Where it lives:** engine `collections`, collection `knowledge_progress`, one row per working document
(`<kb_id>:<doc_id>`) or rebuild (`<kb_id>:<snapshot_id>`), upserted by its own key. The row is written when the
work starts — so a run that died never passes its row to the next one — and removed when it rests. Times are
inside the value: a collections row's `updated_at` does not move on an upsert in cloud.

**What a screen reads** (`attach`): the rows of the documents in `parsing` or `indexing` only — a row left by a
worker that died is never shown for a document that has since rested — and from a row nobody has written for
`STALE_AFTER_S`, the stage without an estimate or a wait.

**The estimate** comes from this stage's own pace, counted from where the stage started (passages already cached
are not pace) and leaving out the time spent waiting out a rate limit (the screen says it waits instead), only after
`ETA_MIN_UNITS` units and `ETA_MIN_S` seconds of work; it is rounded (`round_eta`).

Pure: stdlib, sqlalchemy and the store — cloud's API process imports it.
"""
from __future__ import annotations

import asyncio
import contextvars
import itertools
import math
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import DocumentOut, DocumentProgress, ProgressWait

_log = get_logger(__name__)

COLLECTION = "knowledge_progress"
#: Counts are written at most this often; a stage change or a wait at once.
FLUSH_EVERY_S = 2.0
#: A row is written at least this often while the work goes on, so a reader can tell a live one.
KEEPALIVE_S = 30.0
#: A row not written for this long belongs to work that stopped: it is shown without an estimate or a wait.
STALE_AFTER_S = 90.0
#: An estimate needs at least this many units done in the stage, over at least this long.
ETA_MIN_UNITS = 3
ETA_MIN_S = 5.0

#: The statuses whose documents are at work.
WORKING = ("parsing", "indexing")

_CURRENT: contextvars.ContextVar[Tracker | None] = contextvars.ContextVar("knowledge_progress", default=None)


def current() -> Tracker | None:
    """The tracker of the work this code runs for, or None (nothing is tracking it)."""
    return _CURRENT.get()


def detach() -> None:
    """Forget any tracker in this context: a job spawned from inside tracked work (the in-process dispatcher
    copies its caller's context) must never report into its parent's row."""
    _CURRENT.set(None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.isoformat()


def round_eta(seconds: float) -> int:
    """Seconds left, rounded as a person reads them: up to the next 10 s under a minute, to 30 s under ten
    minutes, to the minute beyond."""
    if seconds <= 0:
        return 0
    if seconds < 60:
        return max(10, math.ceil(seconds / 10) * 10)
    if seconds < 600:
        return int(round(seconds / 30) * 30)
    return int(round(seconds / 60) * 60)


class Tracker:
    """One piece of work's progress, in memory. Its methods are synchronous and never raise; after `close()`
    they do nothing."""

    def __init__(self, *, doc_id: str | None = None, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.doc_id = doc_id
        self.stage_name: str | None = None
        self.unit: str | None = None
        self.done: int | None = None
        self.total: int | None = None
        #: The most passages this document has reached in any stage, and the most it is cut into now (a rebuild
        #: counts with them); `held`: the passages a rebuild's document held before, in the serving index.
        self.passages = 0
        self.doc_passages = 0
        self.held: int | None = None
        self._base = 0
        self._t0 = clock()
        self._waits: dict[int, dict[str, Any]] = {}
        #: Time this stage spent waiting out a rate limit (one wait or several at once): not the work's pace.
        self._waited = 0.0
        self._wait_began: float | None = None
        self._ids = itertools.count(1)
        self.closed = False
        #: A change a reader should see now (the stage, a wait); `dirty`: counts moved since the last write.
        self.urgent = asyncio.Event()
        self.dirty = False

    # ── what the stages call ──

    def stage(self, name: str, *, total: int | None = None, unit: str | None = None, done: int | None = None) -> None:
        """A new stage begins. `done` is what is already done at its start (cached passages, kept pages): the
        estimate's pace is counted from there."""
        if self.closed:
            return
        self.stage_name, self.unit, self.total = name, unit, total
        self.done = done if done is not None else (0 if total is not None else None)
        if unit == "passages" and total is not None:
            self.doc_passages = max(self.doc_passages, total)
        self._base = self.done or 0
        self._t0 = self._clock()
        self._waited = 0.0
        self._wait_began = self._t0 if self._waits else None
        self._note_passages()
        self.urgent.set()

    def advance(self, *, done: int | None = None, by: int | None = None) -> None:
        """More of the stage is done: `done` in all (never backwards), or `by` more."""
        if self.closed or self.stage_name is None:
            return
        if done is not None:
            self.done = max(self.done or 0, done)
        elif by:
            self.done = (self.done or 0) + by
        else:
            return
        if self.total is not None and self.done is not None:
            self.done = min(self.done, self.total)
        self._note_passages()
        self.dirty = True

    def document(self, doc_id: str, *, held: int | None = None) -> None:
        """A rebuild moves on to `doc_id`, which held `held` passages in the serving index: a new document's stages
        and passages."""
        if self.closed:
            return
        self.doc_id = doc_id
        self.stage_name = self.unit = None
        self.done = self.total = None
        self.passages = self.doc_passages = 0
        self.held = held
        self._waits.clear()
        self.urgent.set()

    def waiting(self, *, provider_id: str, provider: str, seconds: float) -> int:
        """A provider's rate limit is being waited out for `seconds`. Returns a token for `resumed`; several
        waits at once (concurrent calls) are each their own."""
        token = next(self._ids)
        if self.closed:
            return token
        if not self._waits:
            self._wait_began = self._clock()
        self._waits[token] = {"reason": "rate_limited", "provider": provider, "provider_id": provider_id,
                              "until": _iso(_now() + timedelta(seconds=max(0.0, seconds)))}
        self.urgent.set()
        return token

    def resumed(self, token: int) -> None:
        if self.closed:
            return
        if self._waits.pop(token, None) is not None:
            if not self._waits and self._wait_began is not None:
                self._waited += self._clock() - self._wait_began
                self._wait_began = None
            self.urgent.set()

    def close(self) -> None:
        self.closed = True

    # ── what is written ──

    def _note_passages(self) -> None:
        if self.unit == "passages" and self.done is not None:
            self.passages = max(self.passages, self.done)

    def eta_s(self) -> int | None:
        if self.total is None or self.done is None:
            return None
        now = self._clock()
        waited = self._waited + (now - self._wait_began if self._wait_began is not None else 0.0)
        units, elapsed = self.done - self._base, now - self._t0 - waited
        if units < ETA_MIN_UNITS or elapsed < ETA_MIN_S:
            return None
        return round_eta((self.total - self.done) * elapsed / units)

    def value(self) -> dict[str, Any]:
        """The row: what `DocumentProgress` reads, plus the document a rebuild is on and its passages."""
        wait = max(self._waits.values(), key=lambda w: w["until"]) if self._waits else None
        return {"doc_id": self.doc_id, "stage": self.stage_name, "done": self.done, "total": self.total,
                "unit": self.unit, "eta_s": self.eta_s(), "waiting": dict(wait) if wait else None,
                "passages": self.passages, "doc_passages": self.doc_passages, "held": self.held, "at": _iso(_now())}


# ── the row ──────────────────────────────────────────────────────────────────

def key(kb_id: str, work_id: str) -> str:
    return f"{kb_id}:{work_id}"


async def _write(row_key: str, value: dict[str, Any]) -> None:
    from core.store import session, upsert
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    async with session() as s:
        await upsert(s, CollectionDB, values={"tenant_id": get_tenant(), "collection": COLLECTION, "key": row_key,
                                              "value": value},
                     index_elements=["tenant_id", "collection", "key"], update=["value"])


async def _safe_write(row_key: str, value: dict[str, Any]) -> None:
    try:
        await _write(row_key, value)
    except Exception as exc:  # noqa: BLE001 — progress never fails the work it reports on
        _log.warning(f"knowledge progress {row_key} not written: {exc}")


async def forget(row_keys: Iterable[str]) -> None:
    """Remove rows by key. Never raises."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    keys = list(row_keys)
    if not keys:
        return
    try:
        async with session() as s:
            await s.execute(delete(CollectionDB).where(
                CollectionDB.tenant_id == get_tenant(), CollectionDB.collection == COLLECTION,
                CollectionDB.key.in_(keys)))
    except Exception as exc:  # noqa: BLE001 — a leftover row is never shown for resting work
        _log.warning(f"knowledge progress rows not removed: {exc}")


async def forget_base(kb_id: str) -> None:
    """Remove every row of a knowledge base (it is being deleted). Never raises."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    try:
        async with session() as s:
            await s.execute(delete(CollectionDB).where(
                CollectionDB.tenant_id == get_tenant(), CollectionDB.collection == COLLECTION,
                CollectionDB.key.startswith(f"{kb_id}:", autoescape=True)))
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"knowledge progress rows of {kb_id} not removed: {exc}")


async def _flush(row_key: str, tracker: Tracker) -> None:
    """Write `tracker`'s row whenever a reader should see it (see the module docstring), until cancelled."""
    clock = tracker._clock
    last = clock()
    while True:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(tracker.urgent.wait(), timeout=FLUSH_EVERY_S)
        urgent = tracker.urgent.is_set()
        now = clock()
        if urgent or (tracker.dirty and now - last >= FLUSH_EVERY_S) or now - last >= KEEPALIVE_S:
            tracker.urgent.clear()
            tracker.dirty = False
            last = now
            await _safe_write(row_key, tracker.value())
        if urgent:  # a burst of changes (a stage, then its first counts) is written at most every FLUSH_EVERY_S
            await asyncio.sleep(FLUSH_EVERY_S)


@asynccontextmanager
async def tracking(kb_id: str, work_id: str, *, doc_id: str | None = None) -> AsyncIterator[Tracker]:
    """Track the work inside the block as `<kb_id>:<work_id>`: its row is written now, kept up to date, and
    removed when the block ends, however it ends — except when the work gave its slot back to a provider's rate
    limit (`pacing.Parked`, KL1): then the row stays, `parked`, saying whose limit and until when, until the next
    run's row replaces it."""
    from core.knowledge.pacing import Parked

    tracker = Tracker(doc_id=doc_id)
    row_key = key(kb_id, work_id)
    token = _CURRENT.set(tracker)
    tracker.urgent.clear()
    await _safe_write(row_key, tracker.value())
    flusher = asyncio.create_task(_flush(row_key, tracker))
    parked: Parked | None = None
    try:
        yield tracker
    except Parked as exc:
        parked = exc
        raise
    finally:
        tracker.close()
        flusher.cancel()
        with suppress(asyncio.CancelledError):
            await flusher
        _CURRENT.reset(token)
        if parked is not None and parked.reason == "rate_limited":
            await asyncio.shield(_safe_write(row_key, parked_value(tracker.value(), parked)))
        else:
            await forget([row_key])


def parked_value(value: dict[str, Any], parked: Any) -> dict[str, Any]:
    """A row for work that gave its slot back to a rate limit: where it got to, no estimate, and the wait until it
    runs again."""
    until = datetime.fromtimestamp(parked.until, tz=timezone.utc)
    return {**value, "eta_s": None, "parked": True, "waiting": {
        "reason": "rate_limited", "provider": parked.provider, "provider_id": parked.provider_id,
        "until": _iso(until)}}


# ── what a screen reads ───────────────────────────────────────────────────────

async def load(row_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The rows by key (one query); a row that cannot be read is left out."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    if not row_keys:
        return {}
    try:
        async with session() as s:
            rows = (await s.execute(select(CollectionDB.key, CollectionDB.value).where(
                CollectionDB.tenant_id == get_tenant(), CollectionDB.collection == COLLECTION,
                CollectionDB.key.in_(list(row_keys))))).all()
    except Exception as exc:  # noqa: BLE001 — a screen without progress still lists its documents
        _log.warning(f"knowledge progress not read: {exc}")
        return {}
    return {k: v for k, v in rows if isinstance(v, dict)}


def _stale(value: dict[str, Any], now: datetime) -> bool:
    """A row whose writer stopped. A parked row has no writer: it is live until its wait ends (KL1)."""
    if value.get("parked"):
        return _parked_until(value) is None or _parked_until(value) <= now
    try:
        at = datetime.fromisoformat(str(value.get("at")))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds() > STALE_AFTER_S


def _parked_until(value: dict[str, Any]) -> datetime | None:
    wait = value.get("waiting")
    try:
        until = datetime.fromisoformat(str((wait or {}).get("until")))
    except (TypeError, ValueError):
        return None
    return until if until.tzinfo else until.replace(tzinfo=timezone.utc)


def wait_of(value: dict[str, Any], now: datetime | None = None) -> ProgressWait | None:
    """A row's wait, unless the row is stale."""
    from core.knowledge.api_models import ProgressWait

    wait = value.get("waiting")
    if not isinstance(wait, dict) or _stale(value, now or _now()):
        return None
    try:
        return ProgressWait.model_validate(wait)
    except ValueError:
        return None


def to_progress(value: dict[str, Any], now: datetime | None = None) -> DocumentProgress | None:
    """A row as `DocumentProgress`; None for a row that has no stage yet or does not read."""
    from core.knowledge.api_models import DocumentProgress

    if not value.get("stage"):
        return None
    now = now or _now()
    stale = _stale(value, now)
    try:
        return DocumentProgress(
            stage=value["stage"], done=value.get("done"), total=value.get("total"), unit=value.get("unit"),
            eta_s=None if stale else value.get("eta_s"), waiting=wait_of(value, now), updated_at=value.get("at"))
    except ValueError as exc:
        _log.debug(f"knowledge progress row does not read: {exc}")
        return None


async def attach(outs: Sequence[DocumentOut]) -> list[DocumentOut]:
    """`outs` with `progress` set on each working document that has a row (one query for them all) — and on a
    queued one that gave its slot back to a rate limit, while its wait lasts (CCR-14)."""
    working = [o for o in outs if o.status in WORKING or o.status == "queued"]
    if not working:
        return list(outs)
    rows = await load([key(o.kb_id, o.id) for o in working])
    if not rows:
        return list(outs)
    now = _now()
    out = []
    for o in outs:
        value = rows.get(key(o.kb_id, o.id)) if o.status in WORKING or o.status == "queued" else None
        if value and o.status == "queued" and (not value.get("parked") or _stale(value, now)):
            value = None  # a queued document shows a wait only while it is parked on one
        found = to_progress(value, now) if value else None
        out.append(o.model_copy(update={"progress": found}) if found is not None else o)
    return out
