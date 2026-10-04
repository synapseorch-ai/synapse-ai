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
* `enrich` — only when the draft asks for Contextual Retrieval (CR1, D180): one chat call per chunk, on the
  model a rebuild would use, priced by `enrich.estimate_usd` — each document whole, or a long one in windows,
  as the calls will see it, with a cache read only where a cache point holds. The embedding tokens then count
  the contexts too.

Parsing is local in OSS and has no rate. `budget_remaining_usd` is the draft's cap less this month's spend.

**What a deployment charges** (F334, W1, D179). A model our metering gateway serves (`synapse.*`, cost source
`platform`) is charged at the deployment's rate, not the list price a profile or a route carries: cloud's gateway
charges the platform rate × the plan's markup. A deployment says so with `set_platform_pricer` (cloud installs one
that reads its catalogue and the org's plan; OSS installs none), and every quote of a platform cost asks
`platform_rate` first: the preview's lines, the budget checks before embedding and OCR, search's cap check, the
eval estimates, and the embedder's own `Usage.est_usd`. `ocr_usd_per_page`, `rerank_usd_per_search` and
`chat_usd` are those quotes; anything that prices a Knowledge cost calls them rather than a rate of its own. With
no pricer, or a model it does not price, every quote is the list price it was.

**The spend** (`GET /knowledge/bases/{kb_id}/costs`) groups `knowledge_costs` by day (UTC), stage,
document (`"search"` for what searches spent) or model, from the month's start unless told otherwise.

Imports are function-local, as in the rest of the package.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import CostGroup, CostPreview
    from core.knowledge.kb_settings import Embedding, Enrichment, KnowledgeBase, Rerank

#: Settings a draft may change for a preview: what the settings screen edits.
EDITABLE: frozenset[str] = frozenset({
    "name", "description", "language", "visitor_access", "budget_monthly_usd", "chunking", "embedding", "rerank",
    "fusion", "retrieval", "enrichment", "web",
})
GROUPS: tuple[str, ...] = ("day", "stage", "document", "model")
MAX_GROUPS = 1000
SEARCH_KEY = "search"


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


# ── what a deployment charges (F334) ───────────────────────────────────────────

@dataclass(frozen=True)
class Rate:
    """What a deployment charges for one model, in USD: per 1M input and output tokens, or per `unit` (a search
    unit, a page).

    A chat model its gateway caches a prompt for (F349) also has a cache-read price, and maybe a cache-write price
    (None: a write costs what input does; Nova's is 0) and the prompt a cache point needs (`cache_min_tokens`).
    No cache-read price: the gateway caches nothing for it."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    per_unit: float | None = None
    unit: str | None = None
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    cache_min_tokens: int | None = None


#: `model` → what the deployment charges for it, or None when it does not price it.
PlatformPricer = Callable[[str], Awaitable["Rate | None"]]

_pricer: PlatformPricer | None = None


def set_platform_pricer(pricer: PlatformPricer | None) -> None:
    """Install what prices a platform model (cloud: its catalogue × the org's plan). None removes it."""
    global _pricer
    _pricer = pricer


async def platform_rate(model: str) -> Rate | None:
    """What the deployment charges for `model`, or None: no pricer (OSS), a model it does not price, or a pricer
    that failed (logged). A price never fails the work it quotes. Callers ask only for a platform cost."""
    if _pricer is None or not model:
        return None
    try:
        return await _pricer(model)
    except Exception as exc:  # noqa: BLE001 — the list price stands
        from core.log import get_logger

        get_logger(__name__).warning(f"knowledge: no deployment rate for {model}: {exc}")
        return None


def _is_platform_model(model: str) -> bool:
    from core.knowledge.embedders import PLATFORM_PROVIDERS
    from core.providers.registry import provider_for

    return bool(model) and provider_for(model).id in PLATFORM_PROVIDERS


async def ocr_usd_per_page(provider: Any) -> float:
    """What one page read by `provider` costs: the deployment's rate for a platform engine, else the provider's
    `price_per_page_usd` (0 when it has none)."""
    if provider is None:
        return 0.0
    if str(getattr(provider, "cost_source", "") or "") == "platform":
        rate = await platform_rate(str(getattr(provider, "engine", "") or ""))
        if rate is not None and rate.per_unit is not None:
            return rate.per_unit
    return float(getattr(provider, "price_per_page_usd", 0.0) or 0.0)


async def rerank_usd_per_search(model: str) -> float | None:
    """What one search unit of `model` costs: the deployment's rate for a platform reranker, else its route's;
    None for a model no route prices (or prices by the token)."""
    from core.knowledge.rerankers import route_for

    route = route_for(model or "")
    if route is None:
        return None
    if route.cost_source == "platform":
        rate = await platform_rate(model)
        if rate is not None and rate.per_unit is not None:
            return rate.per_unit
    return route.usd_per_search


async def chat_usd(model: str, input_tokens: int, output_tokens: int, *, cache_read: int = 0,
                   cache_write: int = 0) -> float | None:
    """What `model` charges for a chat call's tokens, or None when nothing prices it.

    A platform model at the deployment's rate — its cache reads and writes each at their own price, as our gateway
    settles them (F349), at the input price where the rate has none; any other by the rate card (`usage_tracker`).
    `input_tokens` are the uncached ones."""
    if _is_platform_model(model):
        rate = await platform_rate(model)
        if rate is not None:
            read = rate.input_per_mtok if rate.cache_read_per_mtok is None else rate.cache_read_per_mtok
            write = rate.input_per_mtok if rate.cache_write_per_mtok is None else rate.cache_write_per_mtok
            return round((input_tokens * rate.input_per_mtok + cache_read * read + cache_write * write
                          + output_tokens * rate.output_per_mtok) / 1_000_000, 8)
    from core import usage_tracker

    await usage_tracker._ensure_pricing()
    if usage_tracker._resolve_pricing_entry(model) is None:
        return None
    return usage_tracker.calculate_cost(model, input_tokens, output_tokens, cache_read_tokens=cache_read,
                                        cache_write_tokens=cache_write)


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


# ── the settings check ─────────────────────────────────────────────────────────

def check_settings(*, embedding: Embedding | None = None, rerank: Rerank | None = None,
                   settings: Mapping[str, Any] | None = None, enrichment: Enrichment | None = None) -> None:
    """Raise `ValueError` (a 422) for a width the embedding model cannot answer, a rerank model Synapse
    cannot call, or Contextual Retrieval with no model that can write contexts — before anything is saved or
    indexed. Only the blocks given are checked."""
    from core.knowledge import enrich, profiles, rerankers
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
    enrich.check_settings(enrichment, settings)


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
    from core.knowledge import budgets, enrich
    from core.knowledge.api_models import CostPreview, StageEstimate
    from core.knowledge.config import knowledge_settings
    from core.knowledge.kb_settings import Chunking
    from core.knowledge.ledger import kb_month_spend
    from core.knowledge.profiles import CHARS_PER_TOKEN
    from core.knowledge.seams import get_ocr_provider
    from core.knowledge.snapshots import active_snapshots
    from core.store import session
    from core.store.models import KnowledgeSnapshotDB

    target = draft(kb, overrides)
    cfg = dict(settings if settings is not None else load_settings())
    check_settings(embedding=target.embedding, rerank=target.rerank, settings=cfg, enrichment=target.enrichment)

    docs = await _served(kb.id)
    active = (await active_snapshots([kb.id])).get(kb.id)
    measured = await _chunk_totals(kb.id, active)
    current = kb.chunking
    serving_contextual = False
    if active is not None:
        async with session() as s:
            stats, serving_mode = (await s.execute(select(
                KnowledgeSnapshotDB.stats, KnowledgeSnapshotDB.enrichment_mode).where(
                KnowledgeSnapshotDB.tenant_id == _tenant(), KnowledgeSnapshotDB.id == active))).first() or (None, None)
        serving_contextual = serving_mode == "contextual"
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
    price = await ocr_usd_per_page(provider)
    if provider is not None and unread_pages:
        stages.append(StageEstimate(stage="ocr", model=str(getattr(provider, "engine", "") or "ocr"),
                                    units=float(unread_pages), unit="pages", est_usd=round(unread_pages * price, 8),
                                    cost_source=str(getattr(provider, "cost_source", "") or
                                                    ("byok" if price else "local"))))

    model = target.embedding.model or knowledge_settings(cfg).embedding_model
    if model and per_doc:
        tokens = sum(content + prefix for _, content, prefix in per_doc)
        # The measured prefixes hold the serving index's contexts: a draft that adds Contextual Retrieval adds a
        # written context to every chunk, one that drops it takes them away (CR1, D180).
        contexts = enrich.CONTEXT_OUTPUT_TOKENS * sum(n for n, _, _ in per_doc)
        if (target.enrichment.mode == "contextual") != serving_contextual:
            tokens = max(0, tokens + (contexts if not serving_contextual else -contexts))
        rate, source = await budgets.embed_rate(model)
        stages.append(StageEstimate(stage="embed", model=model, units=float(tokens), unit="tokens",
                                    est_usd=round(tokens * (rate or 0.0) / 1_000_000, 8), cost_source=source))

    contextual = await enrich.preview_stage(target.enrichment, per_doc, cfg)
    if contextual is not None:
        stages.append(contextual)

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
