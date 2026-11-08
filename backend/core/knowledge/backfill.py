"""Re-embedding what was indexed keyword-only (B2a, D163).

A document indexed while no embedding model was configured — every document
before B2a, and any indexed while a model's key or server address was missing —
has chunk rows with no embedding: searchable by keyword, invisible to the vector
leg. `embed_missing(kb)` embeds them in place, into the knowledge base's active
snapshot, with the model that snapshot recorded (or, if it has none yet, the one
configured now; Naveen, D163 #3), and records that model on the snapshot.

**When** (Naveen, D163 #2): at the end of every reconcile pass
(`sync.reconcile`), so OSS's 60-second loop and a reindex job both repair it,
bounded to `PASS_LIMIT` chunks a pass so a large base catches up over several
passes without holding a pass up.

**What it never does:**

* raise into reconcile — every failure is logged, and the next pass tries again;
* embed a document still being indexed (only `indexed` and `partial` ones are
  read), so it never races the pipeline for the same rows;
* spend past a knowledge base's budget, or after the wallet said no — the pass
  stops, and a later one resumes;
* run against an index that keeps its vectors elsewhere. It reads the engine's
  own `knowledge_chunks.embedding`, which only the SQL index writes; under
  cloud's `PgKnowledgeIndex` it is a no-op (cloud has never indexed with
  Knowledge on, so nothing waits there).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

#: The most chunks one pass embeds for one knowledge base.
PASS_LIMIT = 2000
#: Document statuses whose chunks are finished: a document still being indexed is the pipeline's.
SETTLED = ("indexed", "partial")


async def embed_missing(kb: KnowledgeBase, *, settings: Mapping[str, Any] | None = None,
                        limit: int = PASS_LIMIT) -> int:
    """Embed up to `limit` of `kb`'s unembedded chunks in its active snapshot. Returns how many it embedded."""
    try:
        return await _embed_missing(kb, settings=settings, limit=limit)
    except Exception as exc:  # noqa: BLE001 — a reconcile pass never fails because of this
        _log.warning(f"knowledge backfill {kb.id}: {exc}")
        return 0


async def _embed_missing(kb: KnowledgeBase, *, settings: Mapping[str, Any] | None, limit: int) -> int:
    from dataclasses import replace

    from sqlalchemy import or_, select

    from core.knowledge.embedders import ProviderEmbedder, profile_version, snapshot_spec
    from core.knowledge.errors import BudgetExceeded, EmbedderUnavailable, KnowledgeError, WalletExhausted
    from core.knowledge.index_sql import SqlKnowledgeIndex
    from core.knowledge.ledger import CostRow, check_budget, record_costs
    from core.knowledge.search import ChunkView
    from core.knowledge.seams import get_knowledge_embedder, get_knowledge_index
    from core.knowledge.snapshots import active_snapshots, record_embedding
    from core.knowledge.types import Attribution, IndexEntry
    from core.store import session
    from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB
    from core.tenancy import get_tenant

    if kb.status != "active" or limit <= 0:
        return 0
    index = get_knowledge_index()
    if not isinstance(index, SqlKnowledgeIndex):
        return 0
    snapshot_id = (await active_snapshots([kb.id])).get(kb.id)
    if snapshot_id is None:
        return 0
    if settings is None:
        from core.config import load_settings
        settings = load_settings()
    cfg = dict(settings)
    try:
        embedder = get_knowledge_embedder()
    except EmbedderUnavailable:
        return 0
    spec = await snapshot_spec(kb, snapshot_id, cfg)
    if not spec.model and isinstance(embedder.inner, ProviderEmbedder):
        return 0  # no model configured: nothing to embed with, and nothing to ask

    C, D = KnowledgeChunkDB, KnowledgeDocumentDB
    async with session() as s:
        rows = (await s.execute(
            select(C, D.source_url).join(D, D.id == C.doc_id).where(
                C.tenant_id == get_tenant(), C.kb_id == kb.id, C.snapshot_id == snapshot_id, C.embedding.is_(None),
                or_(C.role.is_(None), C.role != "parent"), D.status.in_(SETTLED))
            .order_by(C.id).limit(limit))).all()
    if not rows:
        return 0
    try:
        from core.knowledge.budgets import embed_estimate_usd

        await check_budget(kb, await embed_estimate_usd(spec.model, [
            f"{c.context} {c.content}".strip() if c.context else c.content for c, _url in rows]))
    except BudgetExceeded as exc:
        _log.info(f"knowledge backfill {kb.id}: paused, {exc}")
        return 0

    attribution = Attribution(kb_id=kb.id, snapshot_id=snapshot_id, source="knowledge",
                              session_id=Attribution.ingestion_session(kb.id))
    done = 0
    usage = []
    model = dims = None
    step = max(1, embedder.max_batch)
    try:
        for i in range(0, len(rows), step):
            batch = [ChunkView(chunk, source_url) for chunk, source_url in rows[i:i + step]]
            texts = [f"{v.row.context} {v.row.content}".strip() if v.row.context else v.row.content for v in batch]
            answer = await embedder.embed(texts, kind="document", spec=spec, settings=cfg,
                                          attribution=replace(attribution, doc_id=batch[0].row.doc_id))
            usage.append(answer.usage)
            model, dims = answer.model, answer.dims
            await index.upsert([
                IndexEntry(chunk_id=v.row.chunk_id, doc_id=v.row.doc_id, kb_id=kb.id, snapshot_id=snapshot_id,
                           text=text, lang=v.row.lang, vector=vector, fields=v.fields())
                for v, text, vector in zip(batch, texts, answer.vectors)])
            done += len(batch)
    except (EmbedderUnavailable, WalletExhausted) as exc:
        _log.info(f"knowledge backfill {kb.id}: stopped after {done}, {exc.code}: {exc}")
    except KnowledgeError as exc:
        _log.warning(f"knowledge backfill {kb.id}: stopped after {done}, {exc.code}: {exc}")
    if done and model and dims:
        await record_embedding(snapshot_id, model, dims, profile_version=profile_version(model))
    if usage:
        await record_costs([CostRow(u, attribution) for u in usage])
    if done:
        _log.info(f"knowledge backfill {kb.id}: embedded {done} chunks with {model}")
    return done
