"""What a knowledge base costs: the preview before a rebuild, the spend grouped, and the settings check that
keeps an impossible model choice from reaching indexing (B2b, D167).

**The preview** (`POST /knowledge/bases/{kb_id}/cost-preview`, `api_models.CostPreview`) prices
re-indexing the base's current documents (indexed and partly indexed) under its stored settings with
`overrides` laid over them — what saving that draft would cost, since an index change rebuilds every
document (`snapshots`). It is an estimate and says what it priced:

* `ocr` — pages no OCR has read yet (scanned pages in the documents' reports), at the OCR provider's
  `price_per_page_usd`. A page OCR already read comes back from the parse cache at no cost. Absent when no
  OCR provider is set.
* `embed` — the chunks' tokens (their text and their context prefix), measured from the serving snapshot
  and re-estimated when the draft changes chunking, at the rate of the model a rebuild would use (the
  draft's, else the deployment's). No model: keyword only, no stage.
* `enrich` — only when the draft asks for Contextual Retrieval (not built yet, D167 #1, but priced so the
  choice can be weighed): one chat call per chunk with its document cached, on the contextual model or the
  workspace's chat model, priced by `usage_tracker.calculate_cost` with cache reads and writes.

Parsing is local in OSS and has no rate. `budget_remaining_usd` is the draft's cap less this month's spend.

**The spend** (`GET /knowledge/bases/{kb_id}/costs`) groups `knowledge_costs` by day (UTC), stage,
document (`"search"` for what searches spent) or model, from the month's start unless told otherwise.

Imports are function-local, as in the rest of the package.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import CostGroup, CostPreview
    from core.knowledge.kb_settings import Embedding, KnowledgeBase, Rerank

#: Settings a draft may change for a preview: what the settings screen edits.
EDITABLE: frozenset[str] = frozenset({
    "name", "description", "language", "visitor_access", "budget_monthly_usd", "chunking", "embedding", "rerank",
    "fusion", "retrieval", "enrichment", "web",
})
GROUPS: tuple[str, ...] = ("day", "stage", "document", "model")
MAX_GROUPS = 1000
#: Contextual Retrieval, per chunk: the instruction around the document and chunk, and the context written.
CONTEXT_PROMPT_TOKENS = 100
CONTEXT_OUTPUT_TOKENS = 75
SEARCH_KEY = "search"


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


# ── the settings check ─────────────────────────────────────────────────────────

def check_settings(*, embedding: Embedding | None = None, rerank: Rerank | None = None,
                   settings: Mapping[str, Any] | None = None) -> None:
    """Raise `ValueError` (a 422) for a width the embedding model cannot answer, or a rerank model Synapse
    cannot call — before anything is saved or indexed. Only the blocks given are checked."""
    from core.knowledge import profiles, rerankers
    from core.knowledge.config import knowledge_settings
    from core.knowledge.errors import EmbeddingError

    if embedding is not None and embedding.dims is not None:
        model = embedding.model or knowledge_settings(settings).embedding_model
        profile = profiles.profile_for(model) if model else None
        if profile is not None:
            try:
                profile.request_dims(embedding.dims)
            except EmbeddingError as exc:
                raise ValueError(str(exc)) from exc
    if rerank is not None and rerank.model and rerankers.route_for(rerank.model) is None:
        prefixes = ", ".join(route.prefix for route in rerankers.ROUTES)
        raise ValueError(f"{rerank.model!r} is not a rerank model Synapse can call: name it with its provider "
                         f"({prefixes}).")


def draft(kb: KnowledgeBase, overrides: Mapping[str, Any] | None) -> KnowledgeBase:
    """`kb` with `overrides` laid over it, each settings block replaced whole (as a PATCH does). `ValueError`
    for a field a draft cannot change or a value that does not validate."""
    from core.knowledge.kb_settings import KnowledgeBase

    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - EDITABLE)
    if unknown:
        raise ValueError(f"A cost preview cannot change {', '.join(unknown)}.")
    return KnowledgeBase.model_validate({**kb.model_dump(), **overrides})


# ── the preview ────────────────────────────────────────────────────────────────

async def _served(kb_id: str) -> list[Any]:
    from sqlalchemy import select

    from core.knowledge.snapshots import SERVED
    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D

    async with session() as s:
        return list((await s.execute(select(D.id, D.pages, D.tokens, D.report).where(
            D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(SERVED)))).all())


async def _chunk_totals(kb_id: str, snapshot_id: str | None) -> dict[str, tuple[int, int, int]]:
    """doc_id → (chunks, content tokens, context characters) in the snapshot."""
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import KnowledgeChunkDB as C

    if snapshot_id is None:
        return {}
    async with session() as s:
        rows = (await s.execute(select(
            C.doc_id, func.count(), func.coalesce(func.sum(C.tokens), 0),
            func.coalesce(func.sum(func.length(C.context)), 0),
        ).where(C.tenant_id == _tenant(), C.kb_id == kb_id, C.snapshot_id == snapshot_id)
            .group_by(C.doc_id))).all()
    return {doc_id: (int(n), int(tokens), int(chars)) for doc_id, n, tokens, chars in rows}


def _rechunked(n: int, content: int, *, before: Any, after: Any, pages: int) -> tuple[int, int]:
    """(chunks, content tokens) one document's measured `n` chunks and `content` tokens become under the
    draft's chunking: overlap scales the tokens, the target size the count."""
    if before == after:
        return n, content
    tokens = round(content * (1 + after.overlap_pct) / (1 + before.overlap_pct))
    if after.mode == "none":
        return 1, tokens
    if after.mode == "page":
        return max(1, pages), tokens
    if before.mode in ("structure", "fixed") and n:
        count = math.ceil(n * before.target_tokens / after.target_tokens * (1 + after.overlap_pct)
                          / (1 + before.overlap_pct))
    else:
        count = math.ceil(tokens / after.target_tokens) if tokens else 0
    return max(1 if tokens else 0, count), tokens


async def preview(kb: KnowledgeBase, overrides: Mapping[str, Any] | None = None, *,
                  settings: Mapping[str, Any] | None = None) -> CostPreview:
    """What re-indexing `kb`'s documents under its settings with `overrides` laid over them would cost."""
    from sqlalchemy import select

    from core.config import load_settings
    from core.knowledge import budgets, embedders
    from core.knowledge.api_models import CostPreview, StageEstimate
    from core.knowledge.config import knowledge_settings
    from core.knowledge.kb_settings import Chunking
    from core.knowledge.ledger import kb_month_spend
    from core.knowledge.profiles import CHARS_PER_TOKEN
    from core.knowledge.seams import get_ocr_provider
    from core.knowledge.snapshots import active_snapshots
    from core.providers.registry import provider_for
    from core.store import session
    from core.store.models import KnowledgeSnapshotDB

    target = draft(kb, overrides)
    cfg = dict(settings if settings is not None else load_settings())
    check_settings(embedding=target.embedding, rerank=target.rerank, settings=cfg)

    docs = await _served(kb.id)
    active = (await active_snapshots([kb.id])).get(kb.id)
    measured = await _chunk_totals(kb.id, active)
    current = kb.chunking
    if active is not None:
        async with session() as s:
            stats = (await s.execute(select(KnowledgeSnapshotDB.stats).where(
                KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == active))).scalar_one_or_none()
        recorded = ((stats or {}).get("settings") or {}).get("chunking") if isinstance(stats, dict) else None
        if recorded:
            current = Chunking.model_validate(recorded)

    stages: list[StageEstimate] = []
    per_doc: list[tuple[int, int, int]] = []  # (chunks, content tokens, prefix tokens) under the draft
    pages = 0
    unread_pages = 0
    for doc_id, doc_pages, doc_tokens, report in docs:
        pages += int(doc_pages or 0)
        n, content, chars = measured.get(doc_id, (0, int(doc_tokens or 0), 0))
        prefix_each = (chars / CHARS_PER_TOKEN / n) if n else 0.0
        n2, content2 = _rechunked(n, content, before=current, after=target.chunking, pages=int(doc_pages or 0))
        per_doc.append((n2, content2, round(prefix_each * n2)))
        quality = (report or {}).get("pages") or {} if isinstance(report, dict) else {}
        unread_pages += max(0, int(quality.get("no_text_layer") or 0) - int(quality.get("ocr_applied") or 0))

    provider = get_ocr_provider()
    price = float(getattr(provider, "price_per_page_usd", 0.0) or 0.0) if provider is not None else 0.0
    if provider is not None and unread_pages:
        stages.append(StageEstimate(stage="ocr", model=str(getattr(provider, "engine", "") or "ocr"),
                                    units=float(unread_pages), unit="pages", est_usd=round(unread_pages * price, 8),
                                    cost_source=str(getattr(provider, "cost_source", "") or
                                                    ("byok" if price else "local"))))

    model = target.embedding.model or knowledge_settings(cfg).embedding_model
    if model and per_doc:
        tokens = sum(content + prefix for _, content, prefix in per_doc)
        rate, source = await budgets.embed_rate(model)
        stages.append(StageEstimate(stage="embed", model=model, units=float(tokens), unit="tokens",
                                    est_usd=round(tokens * (rate or 0.0) / 1_000_000, 8), cost_source=source))

    if target.enrichment.mode == "contextual" and per_doc:
        chat = target.enrichment.contextual_model or str(cfg.get("model") or "")
        if chat:
            from core.usage_tracker import _ensure_pricing, calculate_cost

            await _ensure_pricing()  # calculate_cost reads the rate card loaded here
            calls = sum(n for n, _, _ in per_doc)
            usd = 0.0
            for n, content, _ in per_doc:
                if not n:
                    continue
                each = content / n
                usd += calculate_cost(chat, input_tokens=round(n * (each + CONTEXT_PROMPT_TOKENS)),
                                      output_tokens=n * CONTEXT_OUTPUT_TOKENS,
                                      cache_write_tokens=content, cache_read_tokens=content * (n - 1))
            stages.append(StageEstimate(stage="enrich", model=chat, units=float(calls), unit="requests",
                                        est_usd=round(usd, 8),
                                        cost_source=embedders.cost_source(provider_for(chat).id)))

    remaining = None
    if target.budget_monthly_usd is not None:
        remaining = round(max(0.0, target.budget_monthly_usd - await kb_month_spend(kb.id)), 6)
    return CostPreview(docs=len(docs), pages=pages, stages=stages,
                       total_est_usd=round(sum(s.est_usd for s in stages), 8), budget_remaining_usd=remaining)


# ── the spend ──────────────────────────────────────────────────────────────────

async def report(kb_id: str, *, group_by: str = "day", since: datetime | None = None,
                 until: datetime | None = None, limit: int = MAX_GROUPS) -> list[CostGroup]:
    """`kb_id`'s Knowledge costs from `since` (the month's start) to `until` (now), grouped. A day is UTC."""
    from sqlalchemy import func, select

    from core.knowledge.api_models import CostGroup
    from core.knowledge.ledger import month_start
    from core.store import session
    from core.store.models import KnowledgeCostDB as C

    if group_by not in GROUPS:
        raise ValueError(f"group_by is one of {', '.join(GROUPS)}.")
    since = _aware(since) if since else month_start()
    until = _aware(until) if until else datetime.now(timezone.utc)
    limit = max(1, min(int(limit), MAX_GROUPS))
    where = (C.tenant_id == _tenant(), C.kb_id == kb_id, C.at >= since, C.at < until)

    if group_by == "day":  # by the UTC day, here: Postgres's date() would follow the session's time zone
        sums: dict[str, list[Any]] = {}
        async with session() as s:
            for at, est, charged, tokens, units in (await s.execute(select(
                    C.at, C.est_usd, C.charged_microusd, C.input_tokens, C.units).where(*where))).all():
                day = _aware(at).astimezone(timezone.utc).date().isoformat()
                row = sums.setdefault(day, [0.0, None, 0, 0.0])
                row[0] += est or 0.0
                if charged is not None:
                    row[1] = (row[1] or 0) + int(charged)
                row[2] += int(tokens or 0)
                row[3] += float(units or 0.0)
        return [CostGroup(key=day, est_usd=round(est, 8), charged_microusd=charged, input_tokens=tokens,
                          units=units)
                for day, (est, charged, tokens, units) in sorted(sums.items())][:limit]

    column = {"stage": C.stage, "document": C.doc_id, "model": C.model}[group_by]
    async with session() as s:
        rows = (await s.execute(select(
            column, func.coalesce(func.sum(C.est_usd), 0.0), func.sum(C.charged_microusd),
            func.coalesce(func.sum(C.input_tokens), 0), func.coalesce(func.sum(C.units), 0.0),
        ).where(*where).group_by(column))).all()

    def key(value: Any) -> str:
        if group_by == "document":
            return value or SEARCH_KEY
        if group_by == "model":
            return value or "unknown"
        return str(value)

    groups = [CostGroup(key=key(value), est_usd=round(float(est), 8),
                        charged_microusd=int(charged) if charged is not None else None,
                        input_tokens=int(tokens), units=float(units))
              for value, est, charged, tokens, units in rows]
    groups.sort(key=lambda g: (-g.est_usd, g.key))
    return groups[:limit]
