"""A knowledge base's index snapshots: which one serves, and how a new one replaces it (B1a, D158; B2b, D167).

A snapshot is one version of a KB's index — what chunker, enrichment and
embedding model made it (`knowledge_snapshots`). Every chunk row names its
snapshot (a chunk id includes it, so one document version can live in two), and
every search is pinned to one per KB: the `active` one unless a caller names
another. At most one snapshot per KB is active: a unique partial index
(`ux_knowledge_snapshots_tenant_kb_active`) makes that true under a race.

**Each snapshot records the settings it was built with** (`stats["settings"]`:
chunking, embedding, enrichment, language — `kb_settings.INDEX_AFFECTING`), and
a document indexed into it is indexed *as built* (`as_built`), so a snapshot never
mixes two configurations, even while the knowledge base's own settings have
moved on.

**Blue/green** (B2b). An edit that changes `kb_settings.index_signature`
(`after_save`, called by the PATCH) builds a new snapshot beside the serving
one:

    building ── every indexed document, again, from its parse ──▶ active   (the old one: retired)
        └─ paused (budget, wallet, a missing key)    └─ failed (the old one keeps serving)

* `build_step` works through the documents in short steps (25 documents or
  about two minutes), each queued as a `reindex` / `snapshot_build` job that
  carries the snapshot's id, holding a lease (`stats["lease_until"]`) so two
  workers never build the same snapshot at once. A document is re-read from the
  Vault and parsed through the parse cache (`parse.service.parse_document`: a
  cached parse costs nothing and starts no process), then indexed into the new
  snapshot at its current version. Documents written while the build runs keep
  being indexed into the serving snapshot; the build catches up on them before
  it switches (at most `CATCHUP_ROUNDS` passes), and any that moved after the
  switch are queued again.
* **It switches only when every document the old snapshot served is in the new
  one** (Naveen, D167 #2). A document that still fails after `DOC_ATTEMPTS`
  tries fails the build: the new snapshot is dropped, the old one keeps serving,
  and the screen offers a retry (any PATCH re-checks the signature, so an empty
  one retries). A spent budget, an empty wallet or a missing key **pauses** it
  instead: a raised cap, a new month or a saved key (checked every
  `KEY_RETRY`) resumes it.
* **The switch** is one transaction: the old active snapshot is retired, the new
  one activated, and each document's `snapshot_id` moved to it where it holds
  the document's current version. The retired snapshot stays searchable for a
  pinned caller (`snapshot_retired`) for `knowledge_snapshot_grace_days`, then
  `tend` drops its rows and forgets it (a pin to it is then `snapshot_not_found`).
* An edit back to the serving settings cancels a build; an edit to other
  index settings supersedes it. A superseded build is forgotten at once.

`tend(kb)` runs at the end of each reconcile pass (OSS's loop; cloud's job): it
restarts a build whose worker died, resumes paused ones when they can go on,
retires what is past its grace, and queues again any document left on a snapshot
that no longer serves.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from core.knowledge.chunker import chunker_version
from core.knowledge.ids import SNAPSHOT_PREFIX, new_id
from core.knowledge.kb_settings import INDEX_AFFECTING, KnowledgeBase, index_signature
from core.log import get_logger
from core.store.models import KnowledgeSnapshotDB

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import SnapshotBuild
    from core.store.models import KnowledgeDocumentDB

_log = get_logger(__name__)

#: Bump when the deterministic enrichment prefix changes shape (`pipeline.enrich`).
ENRICHMENT_VERSION = "prefix-1"

#: Documents a knowledge base serves: what a rebuild must carry over.
SERVED = ("indexed", "partial")
#: A build step's size: this many documents, or this long, then the next step is queued.
STEP_DOCS = 25
STEP_SECONDS = 120.0
#: How long a step's lease holds without a beat. A step beats after every document and, while a
#: document parses, every minute; a lease this old belongs to a worker that died.
LEASE = timedelta(minutes=15)
#: Passes over the documents written while a build ran, before it switches anyway.
CATCHUP_ROUNDS = 3
#: Tries per document before it fails the build, and the waits between them.
DOC_ATTEMPTS = 3
BACKOFF_S: tuple[float, ...] = (2.0, 8.0)
#: A build paused for a missing key tries again this often.
KEY_RETRY = timedelta(minutes=10)
#: Documents left on a retired snapshot that one `tend` pass queues again.
REQUEUE_PER_PASS = 500
FAILED_DOCS_KEPT = 20

#: Why a build may wait, and what resumes it.
PAUSE_BUDGET = "budget_cap"
PAUSE_WALLET = "wallet_402"
PAUSE_KEY = "embedder_unavailable"


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _iso(at: datetime | None) -> str | None:
    at = _aware(at)
    return at.isoformat() if at else None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _chunking_hash(kb: KnowledgeBase) -> str:
    blob = json.dumps(kb.chunking.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def index_settings(kb: KnowledgeBase) -> dict[str, Any]:
    """The settings a snapshot is built with (`INDEX_AFFECTING`), as JSON."""
    dumped = kb.model_dump(mode="json")
    return {name: dumped[name] for name in INDEX_AFFECTING}


def _snapshot(kb: KnowledgeBase, *, status: str, now: datetime, **stats: Any) -> KnowledgeSnapshotDB:
    from core.knowledge.seams import get_knowledge_index

    return KnowledgeSnapshotDB(
        id=new_id(SNAPSHOT_PREFIX), tenant_id=_tenant(), kb_id=kb.id, status=status,
        signature=index_signature(kb), chunker_version=chunker_version(kb.chunking.mode),
        chunker_settings_hash=_chunking_hash(kb), enrichment_mode="prefix", enrichment_version=ENRICHMENT_VERSION,
        index_kind=get_knowledge_index().capabilities.name,
        stats={"language": kb.language, "settings": index_settings(kb), **stats},
        created_at=now, activated_at=now if status == "active" else None,
    )


# ── which snapshot serves ──────────────────────────────────────────────────────

async def _active(s, kb_id: str) -> KnowledgeSnapshotDB | None:
    return (await s.execute(select(KnowledgeSnapshotDB).where(
        KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.kb_id == kb_id,
        KnowledgeSnapshotDB.status == "active"))).scalars().first()


async def serving_snapshot(kb: KnowledgeBase) -> str:
    """The id of `kb`'s active snapshot, created (and made active) on the KB's first indexing."""
    from core.store import session

    async with session() as s:
        found = await _active(s, kb.id)
        if found is not None:
            return found.id
    snapshot = _snapshot(kb, status="active", now=_now())
    try:
        async with session() as s:
            s.add(snapshot)
        return snapshot.id
    except IntegrityError:  # another worker made the KB's first snapshot a moment ago
        async with session() as s:
            found = await _active(s, kb.id)
        if found is None:
            raise
        return found.id


async def active_snapshots(kb_ids: Sequence[str]) -> dict[str, str]:
    """kb_id → its active snapshot's id, for the KBs that have one."""
    from core.store import session

    if not kb_ids:
        return {}
    async with session() as s:
        rows = (await s.execute(select(KnowledgeSnapshotDB.kb_id, KnowledgeSnapshotDB.id).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.kb_id.in_(list(kb_ids)),
            KnowledgeSnapshotDB.status == "active"))).all()
    return {kb_id: snap_id for kb_id, snap_id in rows}


async def snapshot_info(snapshot_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """What a search records about each snapshot it ran against, by id (unknown ids are absent)."""
    from core.store import session

    if not snapshot_ids:
        return {}
    async with session() as s:
        rows = (await s.execute(select(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(),
            KnowledgeSnapshotDB.id.in_(list(snapshot_ids))))).scalars().all()
    return {r.id: {"kb_id": r.kb_id, "status": r.status, "embed_model": r.embed_model, "embed_dims": r.embed_dims,
                   "chunker_version": r.chunker_version, "language": (r.stats or {}).get("language")}
            for r in rows}


async def record_embedding(snapshot_id: str, model: str, dims: int, *, profile_version: str | None = None) -> None:
    """Note the model that embedded a snapshot's first vectors, and its profile's version (B2a,
    `core.knowledge.profiles`). Set once; later calls change nothing."""
    from core.store import session

    async with session() as s:
        await s.execute(update(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot_id,
            KnowledgeSnapshotDB.embed_model.is_(None),
        ).values(embed_model=model, embed_dims=dims, embed_profile_version=profile_version))


async def as_built(kb: KnowledgeBase, snapshot_id: str) -> KnowledgeBase:
    """`kb` with the index settings `snapshot_id` was built with, so a document indexed into it matches the
    rest of it. `kb` itself for a snapshot that recorded none (one made before B2b) or is unknown."""
    from core.store import session

    async with session() as s:
        stats = (await s.execute(select(KnowledgeSnapshotDB.stats).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot_id))).scalar_one_or_none()
    recorded = (stats or {}).get("settings") if isinstance(stats, dict) else None
    if not recorded:
        return kb
    try:
        return KnowledgeBase.model_validate({**kb.model_dump(), **recorded})
    except Exception as exc:  # noqa: BLE001 — a record that no longer validates: index with the base's own
        _log.warning(f"snapshot {snapshot_id}: its recorded settings do not validate ({exc}); using the base's")
        return kb


# ── starting, superseding and cancelling a rebuild ─────────────────────────────

async def _kb_snapshots(kb_id: str, *statuses: str) -> list[KnowledgeSnapshotDB]:
    from core.store import session

    stmt = select(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.tenant_id == _tenant(),
                                             KnowledgeSnapshotDB.kb_id == kb_id)
    if statuses:
        stmt = stmt.where(KnowledgeSnapshotDB.status.in_(statuses))
    async with session() as s:
        return list((await s.execute(stmt.order_by(KnowledgeSnapshotDB.created_at))).scalars().all())


async def _served_count(kb_id: str) -> int:
    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D

    async with session() as s:
        return int((await s.execute(select(func.count()).where(
            D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(SERVED)))).scalar_one())


async def _drop(kb_id: str, snapshot_id: str) -> None:
    """A snapshot's chunks out of the index and the table."""
    from core.knowledge.errors import KnowledgeUnavailable
    from core.knowledge.seams import get_knowledge_index
    from core.knowledge.types import Scope
    from core.store import session
    from core.store.models import KnowledgeChunkDB as C

    try:
        await get_knowledge_index().drop_snapshot(Scope(kb_id, snapshot_id))
    except KnowledgeUnavailable:
        pass
    async with session() as s:  # an index that keeps its rows elsewhere leaves ours
        await s.execute(delete(C).where(C.tenant_id == _tenant(), C.kb_id == kb_id, C.snapshot_id == snapshot_id))


async def _forget(snapshot: KnowledgeSnapshotDB) -> None:
    """A snapshot that never served (superseded, cancelled) or is past its grace: its rows, then its record."""
    from core.store import session

    await _drop(snapshot.kb_id, snapshot.id)
    async with session() as s:
        await s.execute(delete(KnowledgeSnapshotDB).where(KnowledgeSnapshotDB.tenant_id == _tenant(),
                                                          KnowledgeSnapshotDB.id == snapshot.id))


async def _enqueue(kb_id: str, snapshot_id: str) -> None:
    from core.knowledge import dispatch

    await dispatch.enqueue(kb_id, "reindex", "snapshot_build", snapshot_id=snapshot_id)


async def after_save(before: KnowledgeBase, after: KnowledgeBase) -> bool:
    """Bring `after`'s index in line with its settings after an edit; whether a rebuild is under way.

    * never indexed (no active snapshot): nothing to rebuild — the first document builds with `after`;
    * `after` matches the serving snapshot: nothing to do, and a rebuild for other settings is cancelled;
    * a rebuild for exactly these settings is already running (or waiting): it goes on;
    * otherwise a new snapshot is started, superseding any other rebuild. That includes an edit that
      changes nothing after a failed rebuild: the retry.
    """
    from core.store import session

    want = index_signature(after)
    snapshots = await _kb_snapshots(after.id, "active", "building")
    active = next((s for s in snapshots if s.status == "active"), None)
    building = [s for s in snapshots if s.status == "building"]
    if active is None:
        return False
    if active.signature == want:
        for old in building:
            await _forget(old)
        return False
    if building and building[-1].signature == want:
        for old in building[:-1]:
            await _forget(old)
        await _enqueue(after.id, building[-1].id)
        return True
    for old in building:
        await _forget(old)
    if not (active.stats or {}).get("settings") and active.signature == index_signature(before):
        # Made before snapshots recorded their settings: `before` is what built it.
        async with session() as s:
            await s.execute(update(KnowledgeSnapshotDB).where(
                KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == active.id,
            ).values(stats={**(active.stats or {}), "settings": index_settings(before)}))
    snapshot = _snapshot(after, status="building", now=_now(), cursor="", catchup=0)
    snapshot.docs_total = await _served_count(after.id)
    async with session() as s:
        s.add(snapshot)
    _log.info(f"knowledge base {after.id}: rebuilding its index as {snapshot.id} "
              f"({snapshot.docs_total} documents); {active.id} serves meanwhile")
    await _enqueue(after.id, snapshot.id)
    return True


# ── the lease ──────────────────────────────────────────────────────────────────

async def _row(snapshot_id: str) -> KnowledgeSnapshotDB | None:
    from core.store import session

    async with session() as s:
        return (await s.execute(select(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot_id))).scalars().first()


async def _write_stats(snapshot: KnowledgeSnapshotDB, stats: dict[str, Any], **columns: Any) -> bool:
    """Replace a building snapshot's stats if nobody wrote it since `snapshot` was read (compare-and-set on
    `updated_at`). Whether it was written; `snapshot` carries the new values when it was."""
    from core.store import session

    stamp = max(_now(), (_aware(snapshot.updated_at) or _now()) + timedelta(microseconds=1))
    async with session() as s:
        result = await s.execute(update(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot.id,
            KnowledgeSnapshotDB.status == "building", KnowledgeSnapshotDB.updated_at == snapshot.updated_at,
        ).values(stats=stats, updated_at=stamp, **columns).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        return False
    snapshot.stats, snapshot.updated_at = stats, stamp
    for name, value in columns.items():
        setattr(snapshot, name, value)
    return True


def _leased(stats: dict[str, Any], now: datetime) -> bool:
    until = _parse_iso(stats.get("lease_until"))
    return until is not None and until > now


async def _claim(snapshot_id: str) -> KnowledgeSnapshotDB | None:
    """The building snapshot, leased to this step; None when it is not building, waits, or another step holds it."""
    snapshot = await _row(snapshot_id)
    if snapshot is None or snapshot.status != "building":
        return None
    stats = dict(snapshot.stats or {})
    now = _now()
    if stats.get("paused_reason") or _leased(stats, now):
        return None
    stats["lease_until"] = _iso(now + LEASE)
    return snapshot if await _write_stats(snapshot, stats) else None


async def _beat(snapshot: KnowledgeSnapshotDB, **changes: Any) -> bool:
    """Renew the lease (and write `changes` into stats). False: the build was cancelled or superseded meanwhile."""
    stats = {**(snapshot.stats or {}), **changes, "lease_until": _iso(_now() + LEASE)}
    if await _write_stats(snapshot, stats):
        return True
    fresh = await _row(snapshot.id)  # a concurrent write that was not a cancel: read and try once more
    if fresh is None or fresh.status != "building":
        return False
    snapshot.stats, snapshot.updated_at = fresh.stats, fresh.updated_at
    stats = {**(fresh.stats or {}), **changes, "lease_until": _iso(_now() + LEASE)}
    return await _write_stats(snapshot, stats)


async def _release(snapshot: KnowledgeSnapshotDB, **changes: Any) -> bool:
    stats = {**(snapshot.stats or {}), **changes}
    stats.pop("lease_until", None)
    return await _write_stats(snapshot, stats)


# ── one step of a rebuild ──────────────────────────────────────────────────────

class _Pause(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class _Fail(Exception):
    def __init__(self, message: str, doc: KnowledgeDocumentDB | None = None, code: str = "internal"):
        super().__init__(message)
        self.doc = doc
        self.code = code


def _built_clause(snapshot_id: str):
    """Correlated: the snapshot holds the document's current version."""
    from core.store.models import KnowledgeChunkDB as C
    from core.store.models import KnowledgeDocumentDB as D

    return select(C.id).where(C.tenant_id == D.tenant_id, C.snapshot_id == snapshot_id, C.doc_id == D.id,
                              C.doc_version == D.version).exists()


async def _to_build(kb_id: str, snapshot_id: str, cursor: str, limit: int) -> list[KnowledgeDocumentDB]:
    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D

    async with session() as s:
        return list((await s.execute(select(D).where(
            D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(SERVED), D.id > cursor,
            ~_built_clause(snapshot_id)).order_by(D.id).limit(limit))).scalars().all())


async def _progress(kb_id: str, snapshot_id: str) -> tuple[int, int, int]:
    """(documents served now, how many of them the snapshot holds, its chunks)."""
    from core.store import session
    from core.store.models import KnowledgeChunkDB as C
    from core.store.models import KnowledgeDocumentDB as D

    async with session() as s:
        total = (await s.execute(select(func.count()).where(
            D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(SERVED)))).scalar_one()
        done = (await s.execute(select(func.count()).where(
            D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(SERVED), _built_clause(snapshot_id)))).scalar_one()
        chunks = (await s.execute(select(func.count()).where(
            C.tenant_id == _tenant(), C.kb_id == kb_id, C.snapshot_id == snapshot_id))).scalar_one()
    return int(total), int(done), int(chunks)


async def _still_there(doc_id: str) -> bool:
    from core.knowledge import documents

    row = await documents.get(doc_id)
    return row is not None and row.status != "deleting"


async def _build_document(kb: KnowledgeBase, snapshot: KnowledgeSnapshotDB, row: KnowledgeDocumentDB) -> bool:
    """Index `row`'s current version into the building snapshot. False when it is no longer there to build
    (deleted, or its file changed: ingestion has it). Raises `_Pause` or `_Fail`."""
    from core.config import load_settings
    from core.knowledge.embedders import snapshot_spec
    from core.knowledge.errors import (
        BudgetExceeded, EmbedderUnavailable, KnowledgeError, WalletExhausted,
    )
    from core.knowledge.parse import UnsupportedFormat
    from core.knowledge.parse.limits import ParseRefused
    from core.knowledge.parse.service import parse_document
    from core.knowledge.pipeline_contract import IndexContext, resolve_index_entrypoint
    from core.knowledge.seams import get_knowledge_index
    from core.knowledge.types import Attribution
    from core.storage import get_blob_store

    data = await asyncio.to_thread(get_blob_store().get_bytes, f"vault/{row.path}")
    if data is None or (row.sha256 and hashlib.sha256(data).hexdigest() != row.sha256):
        return False  # gone or changed since it was indexed: its ingestion job (or reconcile) has it
    built_kb = await as_built(kb, snapshot.id)
    settings = load_settings()
    asked = (await snapshot_spec(built_kb, snapshot.id, dict(settings))).model
    attribution = Attribution(kb_id=kb.id, doc_id=row.id, snapshot_id=snapshot.id, source="knowledge",
                              session_id=Attribution.ingestion_session(kb.id))

    async def heartbeat() -> None:
        await _beat(snapshot)

    last: Exception | None = None
    for attempt in range(DOC_ATTEMPTS):
        if attempt:
            await asyncio.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
        try:
            parsed = (await parse_document(
                data, path=row.path, source=row.source, sha256=row.sha256 or hashlib.sha256(data).hexdigest(),
                mime=row.mime or "", url=row.source_url,
                modified_at=row.source_modified_at.isoformat() if row.source_modified_at else None,
                kb=built_kb, attribution=attribution, heartbeat=heartbeat)).parsed
            outcome = await resolve_index_entrypoint()(IndexContext(
                kb=built_kb, doc_id=row.id, doc_version=row.version or 1, sha256=parsed.source.sha256,
                path=row.path, source=row.source, parsed=parsed, snapshot_id=snapshot.id, attribution=attribution,
                settings=settings, metadata=dict(row.meta or {})))
        except BudgetExceeded as exc:
            raise _Pause(PAUSE_BUDGET, str(exc)) from exc
        except WalletExhausted as exc:
            raise _Pause(PAUSE_WALLET, str(exc)) from exc
        except EmbedderUnavailable as exc:
            raise _Pause(PAUSE_KEY, str(exc)) from exc
        except (UnsupportedFormat, ParseRefused) as exc:
            raise _Fail(f"{row.path}: it can no longer be read ({exc})", row, "parse_failed") from exc
        except KnowledgeError as exc:
            if not await _still_there(row.id):
                return False
            if not exc.retryable:
                raise _Fail(f"{row.path}: {exc}", row, exc.code) from exc
            last = exc
            continue
        except Exception as exc:  # noqa: BLE001 — a crash is tried again, then fails the build with its name
            if not await _still_there(row.id):
                return False
            last = exc
            continue
        embed = next((s for s in outcome.stages if s.stage == "embed"), None)
        if asked and embed is not None and embed.detail.get("reason") == PAUSE_KEY:
            # The model these settings name cannot be reached (a key or address not saved): never switch to
            # a keyword-only index in its place. What was written for this document goes; the build waits.
            try:
                await get_knowledge_index().delete_document(kb.id, row.id, snapshot_id=snapshot.id)
            except Exception as exc:  # noqa: BLE001 — our rows go below; an index's own copy goes with the snapshot
                _log.warning(f"rebuild {snapshot.id}: {row.id} not taken out of the index: {exc}")
            await _drop_document(kb.id, row.id, snapshot.id)
            raise _Pause(PAUSE_KEY, f"{asked} cannot be used: a key or server address it needs is not saved.")
        return True
    raise _Fail(f"{row.path}: {last} (after {DOC_ATTEMPTS} tries)", row,
                getattr(last, "code", None) or "internal")


async def _drop_document(kb_id: str, doc_id: str, snapshot_id: str) -> None:
    from core.store import session
    from core.store.models import KnowledgeChunkDB as C

    async with session() as s:
        await s.execute(delete(C).where(C.tenant_id == _tenant(), C.kb_id == kb_id, C.doc_id == doc_id,
                                        C.snapshot_id == snapshot_id))


async def build_step(kb: KnowledgeBase, snapshot_id: str) -> str:
    """One step of a rebuild (see the module docstring): "continued", "switched", "paused", "failed", or "left"
    when there was nothing for this step to do (not building, waiting, leased elsewhere, superseded)."""
    snapshot = await _claim(snapshot_id)
    if snapshot is None:
        return "left"
    if kb.status != "active" or snapshot.signature != index_signature(kb):
        await _release(snapshot)
        return "left"  # disabled (re-enabling resumes it), or an edit started another rebuild
    stats = dict(snapshot.stats or {})
    deadline = time.monotonic() + STEP_SECONDS
    cursor = str(stats.get("cursor") or "")
    batch = await _to_build(kb.id, snapshot.id, cursor, STEP_DOCS)
    for row in batch:
        try:
            await _build_document(kb, snapshot, row)
        except _Pause as pause:
            return await _pause(kb, snapshot, pause.reason, str(pause))
        except _Fail as failure:
            await _fail(kb, snapshot, str(failure), failure.doc, failure.code)
            return "failed"
        cursor = row.id
        if not await _beat(snapshot, cursor=cursor):
            await _drop(kb.id, snapshot.id)  # cancelled meanwhile: what this step wrote goes too
            return "left"
        if time.monotonic() > deadline:
            break
    total, done, chunks = await _progress(kb.id, snapshot.id)
    if len(batch) == STEP_DOCS or (batch and cursor != batch[-1].id):
        return await _next(kb, snapshot, cursor=cursor, docs_total=total, docs_done=done, chunks=chunks)
    rounds = int(stats.get("catchup") or 0)
    if done < total and rounds < CATCHUP_ROUNDS:  # written while the build ran: another pass from the start
        return await _next(kb, snapshot, cursor="", catchup=rounds + 1, docs_total=total, docs_done=done,
                           chunks=chunks)
    return await _switch(kb, snapshot, docs_total=total, docs_done=done, chunks=chunks)


async def _next(kb: KnowledgeBase, snapshot: KnowledgeSnapshotDB, *, docs_total: int, docs_done: int, chunks: int,
                **changes: Any) -> str:
    if not await _release(snapshot, **changes):
        return "left"
    await _counts(snapshot.id, docs_total=docs_total, docs_done=docs_done, chunks=chunks)
    await _enqueue(kb.id, snapshot.id)
    return "continued"


async def _counts(snapshot_id: str, **columns: int) -> None:
    from core.store import session

    async with session() as s:
        await s.execute(update(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot_id,
        ).values(**columns).execution_options(synchronize_session=False))


async def _pause(kb: KnowledgeBase, snapshot: KnowledgeSnapshotDB, reason: str, message: str) -> str:
    from core.knowledge.budgets import announce

    if not await _release(snapshot, paused_reason=reason, paused_at=_iso(_now()), paused_message=message[:500]):
        return "left"
    _log.info(f"knowledge base {kb.id}: rebuild {snapshot.id} paused ({reason}): {message}")
    await announce("knowledge.base.paused", kb.id, status="paused", status_reason=reason, snapshot_id=snapshot.id)
    return "paused"


async def _fail(kb: KnowledgeBase, snapshot: KnowledgeSnapshotDB, message: str, doc: KnowledgeDocumentDB | None,
                code: str) -> None:
    from core.knowledge.budgets import announce
    from core.store import session

    stats = {**(snapshot.stats or {}), "error": message[:2000], "error_code": code}
    stats.pop("lease_until", None)
    if doc is not None:
        stats["failed_docs"] = [{"doc_id": doc.id, "path": doc.path, "code": code, "message": message[:500]}]
    async with session() as s:
        result = await s.execute(update(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot.id,
            KnowledgeSnapshotDB.status == "building",
        ).values(status="failed", stats=stats, docs_failed=1 if doc is not None else 0,
                 retired_at=_now()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        return
    await _drop(kb.id, snapshot.id)
    _log.warning(f"knowledge base {kb.id}: rebuild {snapshot.id} failed; the serving snapshot stays: {message}")
    await announce("knowledge.snapshot.failed", kb.id, status="failed", status_reason=code, snapshot_id=snapshot.id)


async def _switch(kb: KnowledgeBase, snapshot: KnowledgeSnapshotDB, *, docs_total: int, docs_done: int,
                  chunks: int) -> str:
    """The new snapshot serves: one transaction retires the old, activates the new and moves each document it
    holds; then documents it does not hold at their current version are queued again."""
    from core.knowledge import dispatch, documents
    from core.knowledge.budgets import announce
    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D

    now = _now()
    stats = dict(snapshot.stats or {})
    for key in ("lease_until", "cursor", "catchup", "paused_reason", "paused_at", "paused_message"):
        stats.pop(key, None)

    class _Lost(Exception):
        pass

    try:
        async with session() as s:
            await s.execute(update(KnowledgeSnapshotDB).where(
                KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.kb_id == kb.id,
                KnowledgeSnapshotDB.status == "active",
            ).values(status="retired", retired_at=now).execution_options(synchronize_session=False))
            made = await s.execute(update(KnowledgeSnapshotDB).where(
                KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == snapshot.id,
                KnowledgeSnapshotDB.status == "building", KnowledgeSnapshotDB.updated_at == snapshot.updated_at,
            ).values(status="active", activated_at=now, stats=stats, docs_total=docs_total, docs_done=docs_done,
                     chunks=chunks).execution_options(synchronize_session=False))
            if made.rowcount != 1:
                raise _Lost()  # cancelled or superseded at the last moment: nothing changes
            await s.execute(update(D).where(
                D.tenant_id == _tenant(), D.kb_id == kb.id, D.status.in_(SERVED), _built_clause(snapshot.id),
            ).values(snapshot_id=snapshot.id).execution_options(synchronize_session=False))
    except _Lost:
        return "left"
    left = await _left_behind(kb.id, snapshot.id, limit=None)
    for row in left:
        again = await documents.transition(row.id, "queued", previous=row.status, queued_at=_now())
        if again is not None:
            await dispatch.enqueue(kb.id, "upsert", "snapshot_build", path=again.path, doc_id=again.id)
    _log.info(f"knowledge base {kb.id}: {snapshot.id} now serves ({docs_done} of {docs_total} documents, "
              f"{chunks} chunks); {len(left)} queued again")
    await announce("knowledge.base.reindexed", kb.id, status="active", snapshot_id=snapshot.id)
    return "switched"


async def _left_behind(kb_id: str, snapshot_id: str, *, limit: int | None) -> list[KnowledgeDocumentDB]:
    """Served documents not on `snapshot_id` (the active one): indexed into the old one after the switch began."""
    from sqlalchemy import or_

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D

    stmt = select(D).where(D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(SERVED),
                           or_(D.snapshot_id.is_(None), D.snapshot_id != snapshot_id)).order_by(D.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    async with session() as s:
        return list((await s.execute(stmt)).scalars().all())


# ── resuming and tending ───────────────────────────────────────────────────────

async def resume_build(kb: KnowledgeBase, *, reasons: Sequence[str], paused_before: datetime | None = None) -> bool:
    """Let a rebuild paused for one of `reasons` (before `paused_before`, when given) go on. Whether it did."""
    for snapshot in await _kb_snapshots(kb.id, "building"):
        stats = dict(snapshot.stats or {})
        if stats.get("paused_reason") not in reasons:
            continue
        at = _parse_iso(stats.get("paused_at"))
        if paused_before is not None and at is not None and at >= paused_before:
            continue
        for key in ("paused_reason", "paused_at", "paused_message"):
            stats.pop(key, None)
        if await _write_stats(snapshot, stats):
            await _enqueue(kb.id, snapshot.id)
            return True
    return False


async def tend(kb: KnowledgeBase, *, startup: bool = False, now: datetime | None = None) -> None:
    """Keep `kb`'s snapshots in order; called at the end of each reconcile pass. Never raises."""
    try:
        await _tend(kb, startup=startup, now=now or _now())
    except Exception as exc:  # noqa: BLE001 — reconcile goes on
        _log.warning(f"knowledge base {kb.id}: snapshot tending failed: {exc}")


async def _tend(kb: KnowledgeBase, *, startup: bool, now: datetime) -> None:
    from core.knowledge import dispatch, documents
    from core.knowledge.config import knowledge_settings

    if kb.status != "active":
        return
    snapshots = await _kb_snapshots(kb.id)
    building = [s for s in snapshots if s.status == "building"]
    active = next((s for s in snapshots if s.status == "active"), None)

    # 1–2. A rebuild whose worker died goes on; one waiting for a key tries again. (One waiting for budget
    # resumes with the base's paused documents: `budgets.tend` at a month's start, `budgets.resume` on a raise.)
    for snapshot in building:
        stats = snapshot.stats or {}
        reason = stats.get("paused_reason")
        paused = _parse_iso(stats.get("paused_at"))
        if reason == PAUSE_KEY and (paused is None or now - paused >= KEY_RETRY):
            await resume_build(kb, reasons=(PAUSE_KEY,))
        elif not reason and startup:
            # A process starting: a lease it finds was held by a process that is gone (OSS runs one).
            if _leased(stats, now):
                await _release(snapshot)
            await _enqueue(kb.id, snapshot.id)
        elif not reason and not _leased(stats, now):
            beat = _aware(snapshot.updated_at)
            if beat is None or now - beat >= LEASE:
                await _enqueue(kb.id, snapshot.id)

    # 3. Past their grace: retired snapshots go; failed ones are forgotten.
    grace = timedelta(days=knowledge_settings().snapshot_grace_days)
    for snapshot in snapshots:
        if snapshot.status in ("retired", "failed") and (_aware(snapshot.retired_at) or now) <= now - grace:
            await _forget(snapshot)

    # 4. Documents left on a snapshot that no longer serves are queued again (the switch's backstop).
    if active is not None and not building:
        for row in await _left_behind(kb.id, active.id, limit=REQUEUE_PER_PASS):
            again = await documents.transition(row.id, "queued", previous=row.status, queued_at=_now())
            if again is not None:
                await dispatch.enqueue(kb.id, "upsert", "snapshot_build", path=again.path, doc_id=again.id)

    # 5. Chunks of a snapshot nobody records (a cancelled build a step was still writing to) go.
    await _drop_orphans(kb.id)


async def _drop_orphans(kb_id: str) -> None:
    from core.store import session
    from core.store.models import KnowledgeChunkDB as C

    async with session() as s:
        held = set((await s.execute(select(C.snapshot_id).where(
            C.tenant_id == _tenant(), C.kb_id == kb_id).distinct())).scalars().all())
        known = set((await s.execute(select(KnowledgeSnapshotDB.id).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.kb_id == kb_id))).scalars().all())
    for snapshot_id in sorted(held - known):
        await _drop(kb_id, snapshot_id)


# ── what a screen shows ────────────────────────────────────────────────────────

async def build_states(kbs: Sequence[KnowledgeBase]) -> dict[str, SnapshotBuild]:
    """kb_id → its rebuild: the one under way, else the newest failed one while the base's settings still
    differ from its serving snapshot (CCR-5)."""
    from core.knowledge.api_models import SnapshotBuild
    from core.store import session

    by_id = {kb.id: kb for kb in kbs}
    if not by_id:
        return {}
    async with session() as s:
        rows = (await s.execute(select(KnowledgeSnapshotDB).where(
            KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.kb_id.in_(list(by_id)),
            KnowledgeSnapshotDB.status.in_(("active", "building", "failed")),
        ).order_by(KnowledgeSnapshotDB.created_at))).scalars().all()
    out: dict[str, SnapshotBuild] = {}
    for kb_id, kb in by_id.items():
        mine = [r for r in rows if r.kb_id == kb_id]
        active = next((r for r in mine if r.status == "active"), None)
        building = [r for r in mine if r.status == "building"]
        pick = building[-1] if building else None
        if pick is None and active is not None and active.signature != index_signature(kb):
            failed = [r for r in mine if r.status == "failed" and r.signature == index_signature(kb)
                      and (_aware(r.created_at) or _now()) >= (_aware(active.activated_at) or _now())]
            pick = failed[-1] if failed else None
        if pick is None:
            continue
        stats = pick.stats or {}
        out[kb_id] = SnapshotBuild(
            snapshot_id=pick.id, status=pick.status, docs_total=pick.docs_total or 0,
            docs_done=pick.docs_done or 0, docs_failed=pick.docs_failed or 0, started_at=_iso(pick.created_at),
            paused_reason=stats.get("paused_reason"),
            error=stats.get("error") or stats.get("paused_message"))
    return out
