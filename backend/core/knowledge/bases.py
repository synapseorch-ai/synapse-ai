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

def _refuse_shared_sources(sources: list[Any], others: list[KnowledgeBase], *, had: set[str]) -> None:
    """F354: a web source's crawl state is one row per id in the workspace (`web._state_name`), so a source added
    under an id another base holds — or one id named twice — would share it: one base's crawl would take over the
    other's row, and removing the source from either would delete the other's pages. `…/urls` mints every new id; an
    edit may re-send its own base's (`had`). A `ValueError`: a 422 with these words."""
    held = {source.id: kb for kb in others for source in kb.web.sources}
    seen: set[str] = set()
    for source in sources:
        if source.id in seen:
            raise ValueError(f"Web source {source.id} is named twice: each source needs its own id.")
        seen.add(source.id)
        owner = held.get(source.id)
        if owner is not None and source.id not in had:
            raise ValueError(f"Web source {source.id} belongs to knowledge base {owner.name!r} ({owner.id}): each "
                             f"source needs its own id. Add web pages with POST …/bases/{{kb_id}}/urls, which "
                             f"names each source.")


#: F348 (CCR-13, Naveen's call 2 in D190): extracting metadata is not built, and turning it on rebuilt the index for
#: nothing, so no base turns it on. A base stored with it keeps it.
_NOT_BUILT = ("Extracting metadata (enrichment.extract_metadata) is not built yet: leave it false. A document's "
              "metadata is what its upload, record or web page carries.")


async def create_base(body: KnowledgeBaseCreate) -> KnowledgeBase:
    """A new knowledge base on `body.folder`. Raises `KnowledgePathError` for a folder that
    is not allowed or overlaps another base's, and `ValueError` for a web source id another
    base holds or named twice (F354), or for `extract_metadata` turned on (F348)."""
    from core.store import session
    from core.store.models import CollectionDB
    from core.tenancy import get_tenant

    if body.enrichment.extract_metadata:
        raise ValueError(_NOT_BUILT)
    async with _lock():
        existing = await list_bases()
        folder = validate_folder(body.folder, existing)
        _refuse_shared_sources(body.web.sources, existing, had=set())
        now = _now()
        kb = KnowledgeBase.model_validate({**body.model_dump(), "id": new_kb_id(), "folder": folder,
                                           "revision": 1, "created_at": _iso(now), "updated_at": _iso(now)})
        async with session() as s:
            s.add(CollectionDB(tenant_id=get_tenant(), collection=COLLECTION, key=kb.id,
                               value=kb.model_dump(mode="json"), created_at=now, updated_at=now))
    return kb


async def make_base(body: KnowledgeBaseCreate) -> KnowledgeBase:
    """A new knowledge base as the Knowledge screen makes one (F1200): its settings checked before anything is saved,
    the base on its folder (made if missing), and what the folder already holds queued. The route and the builder's
    `create_knowledge_base` both make one here."""
    from core.knowledge import costs, dispatch, sync

    costs.check_settings(embedding=body.embedding, rerank=body.rerank, enrichment=body.enrichment)
    kb = await create_base(body)
    await sync.ensure_folder(kb)
    await dispatch.enqueue(kb.id, "reindex", "reconcile")
    return kb


async def apply_update(kb_id: str, body: KnowledgeBaseUpdate) -> tuple[KnowledgeBase, KnowledgeBase, bool]:
    """An edit as the Knowledge screen saves one (F1218): from the revision it started at, then what follows from it
    — a new folder moves the documents, a base back on is reconciled, a source changed is crawled or dropped, a raised
    budget resumes, a change to how the index is built rebuilds it beside the serving one. (before, after, whether a
    rebuild started.) The route and the builder's tools edit a base here."""
    from core.knowledge import budgets, costs, dispatch, snapshots, sync

    costs.check_settings(embedding=body.embedding, rerank=body.rerank,
                         enrichment=body.enrichment)  # only the blocks the edit sends
    before, after = await update_base(kb_id, body)
    if after.folder != before.folder:
        await sync.rehome(before, after)
    elif after.status == "active" and before.status != "active":
        await dispatch.enqueue(after.id, "reindex", "reconcile")
    if after.web.sources != before.web.sources:  # S1: a removed source's pages go; an added one is crawled
        from core.knowledge import web

        await web.sources_changed(before, after)
    if budgets.cap_raised(before, after):  # what the budget paused is queued again (B2b)
        await budgets.resume(after, reason="budget_raised")
    started = await snapshots.after_save(before, after)
    if not started:  # V1: a search setting changed with no rebuild; a switch re-runs on its own event
        from core.knowledge import evals_auto

        await evals_auto.settings_changed(before, after)
    return before, after, started


async def forget(kb_id: str) -> None:
    """A base forgotten as the Knowledge screen forgets one (F1218): its index, documents, snapshots and question sets
    go; its Vault folder and files stay."""
    from core.knowledge import evals
    from core.knowledge.jobs import drop_base

    await require_base(kb_id)
    await drop_base(kb_id)
    await evals.forget_base(kb_id)  # V1: its question sets and runs go with it


async def used_by(kb_id: str) -> dict[str, list[dict[str, Any]]]:
    """What searches or fills a base (F1218, as `recordings.used_by` names a recording's): workflow steps (a Retrieve
    step that searches it, an Ingest step that fills it) and agents that answer from it."""
    from core.store.resources import load_agents, load_orchestrations

    def names(step: dict) -> bool:
        retrieve, ingest = step.get("retrieve") or {}, step.get("ingest") or {}
        return kb_id in (retrieve.get("kb_ids") or []) or ingest.get("kb_id") == kb_id

    workflows = [{"id": flow.get("id"), "name": flow.get("name"), "step": s.get("id")}
                 for flow in await load_orchestrations() if not (flow or {}).get("system")
                 for s in (flow or {}).get("steps") or [] if isinstance(s, dict) and names(s)]
    agents = [{"id": a.get("id"), "name": a.get("name")} for a in await load_agents()
              if kb_id in ((a or {}).get("knowledge") or [])]
    return {"workflows": workflows, "agents": agents}


async def update_base(kb_id: str, patch: KnowledgeBaseUpdate) -> tuple[KnowledgeBase, KnowledgeBase]:
    """Apply `patch`; returns (before, after). `StaleRevision` when `patch.revision` is not the stored one, and
    `ValueError` for a patch that turns `extract_metadata` on (F348; a base that has it may send it back)."""
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
        if patch.enrichment is not None and patch.enrichment.extract_metadata \
                and not before.enrichment.extract_metadata:
            raise ValueError(_NOT_BUILT)
        fields = {k: v for k, v in patch.model_dump(exclude_unset=True).items()
                  if v is not None and k not in ("revision", "clear_budget")}
        if patch.clear_budget:
            fields["budget_monthly_usd"] = None
        if "folder" in fields or "web" in fields:
            others = [kb for kb in await list_bases() if kb.id != kb_id]
            if "folder" in fields:
                fields["folder"] = validate_folder(fields["folder"], others, self_id=kb_id)
            if patch.web is not None:  # F354: only an added id is checked against the others
                _refuse_shared_sources(patch.web.sources, others, had={s.id for s in before.web.sources})
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
    from core.knowledge.snapshots import build_states

    builds = await build_states(kbs)  # CCR-5 (B2b): a rebuild's progress, pause or failure
    return {kb_id: KnowledgeBaseState(counts_by_status=counts[kb_id], active_snapshot=active.get(kb_id),
                                      building_snapshot=building.get(kb_id),
                                      month_spend_usd=round(await kb_month_spend(kb_id), 6),
                                      paused_reason=paused.get(kb_id) or getattr(builds.get(kb_id), "paused_reason",
                                                                                 None),
                                      build=builds.get(kb_id))
            for kb_id in ids}
