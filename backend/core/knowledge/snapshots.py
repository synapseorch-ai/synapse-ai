"""A knowledge base's index snapshot: which one serves, and the first one's creation (B1a, D158).

A snapshot is one version of a KB's index — what chunker, enrichment and
embedding model made it (`knowledge_snapshots`). Every chunk row names its
snapshot, and every search is pinned to one per KB.

**B1 keeps one snapshot per KB** (Naveen, D158: B1 owns this module until B2).
`serving_snapshot(kb)` returns the KB's `active` snapshot and creates it the
first time a KB is indexed; ingestion (A1) calls it to fill
`IndexContext.snapshot_id`, and search reads `active_snapshots`. B2 adds
blue/green: a settings change that alters `kb_settings.index_signature` builds
a new snapshot beside the serving one and swaps it in when it is complete. Until
then, a changed signature keeps serving (and indexing into) the existing snapshot,
and the snapshot's `stats["language"]` — not the KB's current language — tells
search which keyword configuration its rows were indexed with.

At most one snapshot per KB is active: a unique partial index on the table
(`ux_knowledge_snapshots_tenant_kb_active`) makes that true under a race, and the
loser of a race reads the winner's.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.knowledge.chunker import chunker_version
from core.knowledge.ids import SNAPSHOT_PREFIX, new_id
from core.knowledge.kb_settings import KnowledgeBase, index_signature
from core.store.models import KnowledgeSnapshotDB

#: Bump when the deterministic enrichment prefix changes shape (`pipeline.enrich`).
ENRICHMENT_VERSION = "prefix-1"


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


def _chunking_hash(kb: KnowledgeBase) -> str:
    blob = json.dumps(kb.chunking.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _active(s, kb_id: str) -> KnowledgeSnapshotDB | None:
    return (await s.execute(select(KnowledgeSnapshotDB).where(
        KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.kb_id == kb_id,
        KnowledgeSnapshotDB.status == "active"))).scalars().first()


async def serving_snapshot(kb: KnowledgeBase) -> str:
    """The id of `kb`'s active snapshot, created (and made active) on the KB's first indexing."""
    from core.knowledge.seams import get_knowledge_index
    from core.store import session

    async with session() as s:
        found = await _active(s, kb.id)
        if found is not None:
            return found.id
    now = datetime.now(timezone.utc)
    snapshot = KnowledgeSnapshotDB(
        id=new_id(SNAPSHOT_PREFIX), tenant_id=_tenant(), kb_id=kb.id, status="active",
        signature=index_signature(kb), chunker_version=chunker_version(kb.chunking.mode),
        chunker_settings_hash=_chunking_hash(kb), enrichment_mode="prefix", enrichment_version=ENRICHMENT_VERSION,
        index_kind=get_knowledge_index().capabilities.name, stats={"language": kb.language},
        created_at=now, activated_at=now,
    )
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
