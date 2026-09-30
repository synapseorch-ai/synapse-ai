"""The knowledge base registry: records in the engine's `collections`, and their live state.

A record is what a person chose (`kb_settings.KnowledgeBase`); its state —
documents by status, the serving snapshot, this month's spend — is read from
the knowledge tables every time, never stored in the record.

**Edits are a compare-and-set.** `collections.save` replaces a whole list, so
this module never uses it: a record is one `collections` row, written alone.
An update names the `revision` it started from; a stale one is refused
(`StaleRevision`, a 409) before anything is written, and the write itself is
`UPDATE … WHERE updated_at = <the value read>`, so two processes racing on the
same record cannot both win either. Creating and re-foldering are serialized
per tenant in this process, because the folder-overlap rule reads every other
record first.

Folder side effects — creating the folder, reconciling it, tombstoning the
documents of a deleted or re-foldered base — are `core.knowledge.sync`'s.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from core.knowledge.errors import KnowledgeError, StaleRevision
from core.knowledge.ids import new_kb_id
from core.knowledge.kb_settings import COLLECTION, KnowledgeBase, validate_folder
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import KnowledgeBaseCreate, KnowledgeBaseState, KnowledgeBaseUpdate

_log = get_logger(__name__)


class UnknownKnowledgeBase(KnowledgeError, LookupError):
    """No knowledge base with that id in this workspace (a 404)."""

    code = "not_found"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.isoformat()


# ── one lock per tenant per event loop ─────────────────────────────────────────

_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    from core.tenancy import get_tenant

    key = (id(asyncio.get_running_loop()), get_tenant())
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


# ── reads ──────────────────────────────────────────────────────────────────────

def _parse(value: Any, key: str) -> KnowledgeBase | None:
    try:
        return KnowledgeBase.model_validate(value)
    except Exception as exc:  # noqa: BLE001 — one bad record never hides the others
        _log.warning(f"knowledge base {key!r} skipped: its record does not validate ({exc})")
        return None


async def list_bases() -> list[KnowledgeBase]:
    """The tenant's knowledge bases, oldest first. A record that fails validation is skipped and logged."""
    from core.store import collections

    out = []
    for item in await collections.load(COLLECTION):
        kb = _parse(item, str(item.get("id") if isinstance(item, dict) else "?"))
        if kb is not None:
            out.append(kb)
    return out


async def _row(kb_id: str) -> tuple[KnowledgeBase, datetime] | None:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    async with session() as s:
        row = (await s.execute(select(CollectionDB.value, CollectionDB.updated_at).where(
            CollectionDB.tenant_id == get_tenant(), CollectionDB.collection == COLLECTION,
            CollectionDB.key == kb_id))).one_or_none()
    if row is None:
        return None
    kb = _parse(row.value, kb_id)
    return None if kb is None else (kb, row.updated_at)


async def get_base(kb_id: str) -> KnowledgeBase | None:
    found = await _row(kb_id)
    return None if found is None else found[0]


async def require_base(kb_id: str) -> KnowledgeBase:
    kb = await get_base(kb_id)
    if kb is None:
        raise UnknownKnowledgeBase(f"No knowledge base {kb_id!r}.")
    return kb


# ── writes ─────────────────────────────────────────────────────────────────────

async def create_base(body: KnowledgeBaseCreate) -> KnowledgeBase:
    """A new knowledge base on `body.folder`. Raises `KnowledgePathError` for a folder that
    is not allowed or overlaps another base's."""
    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    async with _lock():
        folder = validate_folder(body.folder, await list_bases())
        now = _now()
        kb = KnowledgeBase.model_validate({**body.model_dump(), "id": new_kb_id(), "folder": folder,
                                           "revision": 1, "created_at": _iso(now), "updated_at": _iso(now)})
        async with session() as s:
            s.add(CollectionDB(tenant_id=get_tenant(), collection=COLLECTION, key=kb.id,
                               value=kb.model_dump(mode="json"), created_at=now, updated_at=now))
    return kb


async def update_base(kb_id: str, patch: KnowledgeBaseUpdate) -> tuple[KnowledgeBase, KnowledgeBase]:
    """Apply `patch`; returns (before, after). `StaleRevision` when `patch.revision` is not the stored one."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    async with _lock():
        found = await _row(kb_id)
        if found is None:
            raise UnknownKnowledgeBase(f"No knowledge base {kb_id!r}.")
        before, seen = found
        if patch.revision != before.revision:
            raise StaleRevision(f"This knowledge base was changed (revision {before.revision}) after the edit "
                                f"started (revision {patch.revision}). Reload it and apply the change again.")
        fields = {k: v for k, v in patch.model_dump(exclude_unset=True).items()
                  if v is not None and k not in ("revision", "clear_budget")}
        if patch.clear_budget:
            fields["budget_monthly_usd"] = None
        if "folder" in fields:
            others = [kb for kb in await list_bases() if kb.id != kb_id]
            fields["folder"] = validate_folder(fields["folder"], others, self_id=kb_id)
        now = _now()
        after = KnowledgeBase.model_validate({**before.model_dump(), **fields, "revision": before.revision + 1,
                                              "updated_at": _iso(now)})
        stamp = max(now, _aware(seen) + timedelta(microseconds=1))
        # Its own transaction, the UPDATE first: on SQLite a transaction that has
        # read cannot then take the write lock (core/orchestration/state_store.py).
        async with session() as s:
            result = await s.execute(update(CollectionDB).where(
                CollectionDB.tenant_id == get_tenant(), CollectionDB.collection == COLLECTION,
                CollectionDB.key == kb_id, CollectionDB.updated_at == seen,
            ).values(value=after.model_dump(mode="json"), updated_at=stamp))
            if result.rowcount != 1:
                raise StaleRevision("This knowledge base was changed by someone else just now. Reload it.")
    return before, after


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


async def delete_base_record(kb_id: str) -> bool:
    """Forget the record. The documents and index go first (`sync.forget_base`)."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    async with session() as s:
        result = await s.execute(delete(CollectionDB).where(
            CollectionDB.tenant_id == get_tenant(), CollectionDB.collection == COLLECTION,
            CollectionDB.key == kb_id))
        return bool(result.rowcount)


# ── state ──────────────────────────────────────────────────────────────────────

async def states(kbs: list[KnowledgeBase]) -> dict[str, KnowledgeBaseState]:
    """Each base's live state: documents by status, snapshots, this month's spend, why it is paused."""
    from sqlalchemy import func, select

    from core.knowledge.api_models import KnowledgeBaseState
    from core.knowledge.ledger import kb_month_spend
    from core.store import session
    from core.store.models import KnowledgeDocumentDB, KnowledgeSnapshotDB
    from core.tenancy import get_tenant

    ids = [kb.id for kb in kbs]
    if not ids:
        return {}
    tenant = get_tenant()
    counts: dict[str, dict[str, int]] = {i: {} for i in ids}
    active: dict[str, str] = {}
    building: dict[str, str] = {}
    paused: dict[str, str] = {}
    async with session() as s:
        for kb_id, status, n in (await s.execute(
                select(KnowledgeDocumentDB.kb_id, KnowledgeDocumentDB.status, func.count())
                .where(KnowledgeDocumentDB.tenant_id == tenant, KnowledgeDocumentDB.kb_id.in_(ids))
                .group_by(KnowledgeDocumentDB.kb_id, KnowledgeDocumentDB.status))).all():
            counts[kb_id][status] = int(n)
        for kb_id, snap_id, status in (await s.execute(
                select(KnowledgeSnapshotDB.kb_id, KnowledgeSnapshotDB.id, KnowledgeSnapshotDB.status)
                .where(KnowledgeSnapshotDB.tenant_id == tenant, KnowledgeSnapshotDB.kb_id.in_(ids),
                       KnowledgeSnapshotDB.status.in_(("active", "building")))
                .order_by(KnowledgeSnapshotDB.created_at))).all():
            (active if status == "active" else building)[kb_id] = snap_id
        for kb_id, reason in (await s.execute(
                select(KnowledgeDocumentDB.kb_id, KnowledgeDocumentDB.status_reason)
                .where(KnowledgeDocumentDB.tenant_id == tenant, KnowledgeDocumentDB.kb_id.in_(ids),
                       KnowledgeDocumentDB.status == "paused"))).all():
            paused.setdefault(kb_id, reason)
    return {kb_id: KnowledgeBaseState(counts_by_status=counts[kb_id], active_snapshot=active.get(kb_id),
                                      building_snapshot=building.get(kb_id),
                                      month_spend_usd=round(await kb_month_spend(kb_id), 6),
                                      paused_reason=paused.get(kb_id))
            for kb_id in ids}
