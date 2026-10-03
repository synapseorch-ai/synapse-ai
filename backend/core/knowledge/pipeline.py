"""Indexing: one parsed document version turned into searchable chunks (B1a, D158).

`index_document` is `pipeline_contract.INDEX_ENTRYPOINT`, the one call ingestion
(A1) makes into indexing:

    chunk → enrich → embed → write the chunk rows → index → record the costs

**It never sets a document's status** (the contract's rule: one owner each).
It returns an `IndexOutcome` — "indexed", or "partial" with the reason the
parse gave (pages cut, pages with no text layer, attachments skipped) — and
raises a typed `KnowledgeError` for anything else, which ingestion maps to a
status: `BudgetExceeded` and `WalletExhausted` pause, `EmbeddingError` and
`KnowledgeIndexError` fail.

**No search ever sees the document missing.** The new version's rows are
written (replacing any half-written by an earlier attempt at the same version:
chunk ids are derived from the version, so a retry lands on the same ids), the
index is given them, and only then are older versions' chunks removed — from
the index and from the rows.

**Enrichment** is the deterministic prefix (D153 #3): the document's title, date
and section, versioned `snapshots.ENRICHMENT_VERSION`. A snapshot built with
Contextual Retrieval (CR1, D180) also gives each chunk a sentence a model wrote
about where the passage sits in its document (`enrich.contextualize`), between
the prefix and the chunker's own context; a passage the model gave nothing
usable for keeps the prefix alone, and the stage says how many.

**Embedding** uses the snapshot's own model once it has recorded one, else the
knowledge base's, else the deployment's (`embedders.snapshot_spec`, D163 #3), so
one snapshot never holds two models' vectors. With no model configured, or a key
or server address the model needs not saved (`EmbedderUnavailable`), the stage is
skipped with that reason and the chunks are searchable by keyword only, until
`backfill.embed_missing` embeds them on a later reconcile. A provider's rate limit
is waited out, not failed (`pacing.paced`, PG1). When embedding stops part-way —
an empty wallet, a limit outlasting its wait, a cancel — the passages answered so
far are kept (`embed_cache`) and the next attempt sends only the rest (F345).

**Progress** (PG1, F357): the contexts, embedding and indexing stages are told to
the document's tracker (`progress.current()`), passages counted as they are answered.
"""
from __future__ import annotations

import asyncio
import posixpath
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError

from core.knowledge import embed_cache, pacing, progress
from core.knowledge.chunker import chunk_document, chunker_version
from core.knowledge.chunks import Chunk
from core.knowledge.embedders import profile_version, snapshot_spec
from core.knowledge.enrich import Enriched, built_with, contextualize
from core.knowledge.errors import EmbedderUnavailable, KnowledgeIndexError
from core.knowledge.filters import BUILTINS
from core.knowledge.ledger import CostRow, check_budget, record_costs
from core.knowledge.parsed import ParsedDocument, ParseWarning
from core.knowledge.pipeline_contract import IndexContext, IndexOutcome
from core.knowledge.snapshots import ENRICHMENT_VERSION, record_embedding
from core.knowledge.status import REASONS, StageReport
from core.knowledge.telemetry import (
    KNOWLEDGE_DOC_ID, KNOWLEDGE_KB_ID, KNOWLEDGE_SNAPSHOT_ID, SPAN_INDEX,
)
from core.knowledge.types import IndexEntry, Scope, Usage
from core.log import get_logger
from core.store.models import KnowledgeChunkDB
from core.telemetry import spans as _spans

_log = get_logger(__name__)

_T = KnowledgeChunkDB.__table__
_IN_BATCH = 500


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Stage:
    """Times one stage and makes its `StageReport`."""

    def __init__(self, name: str):
        self.name = name
        self.started_at = _now_iso()
        self._t0 = time.monotonic()

    def report(self, status: str = "ok", **detail: Any) -> StageReport:
        return StageReport(stage=self.name, status=status, started_at=self.started_at,  # type: ignore[arg-type]
                           duration_ms=int((time.monotonic() - self._t0) * 1000), detail=detail)


# ── enrichment ────────────────────────────────────────────────────────────────

def enrich_prefix(title: str | None, doc_date: str | None, heading_path: Sequence[str]) -> str:
    """The deterministic context a chunk is searched with: which document, when, which section."""
    parts = []
    if title:
        parts.append(f"Document: {title}")
    if doc_date:
        parts.append(doc_date)
    if heading_path:
        parts.append("Section: " + " › ".join(heading_path))
    return " · ".join(parts)


def compose_context(prefix: str, sentence: str | None, own: str) -> str:
    """A chunk's searched context: the prefix, the model's sentence (Contextual Retrieval), the chunker's own
    (a split table's header) — whichever are there, in that order."""
    return " · ".join(part for part in (prefix, sentence, own) if part)


def _title(ctx: IndexContext) -> str:
    return str(ctx.metadata.get("title") or ctx.parsed.title or posixpath.basename(ctx.path) or ctx.path)


def _doc_date(ctx: IndexContext) -> str | None:
    value = ctx.metadata.get("doc_date") or ctx.parsed.doc_date
    return str(value) if value else None


# ── the outcome ───────────────────────────────────────────────────────────────

def _all_warnings(doc: ParsedDocument) -> list[ParseWarning]:
    found = list(doc.warnings)
    for attachment in doc.attachments:
        if attachment.skipped_reason:
            found.append(ParseWarning(code="attachment_skipped",
                                      message=f"{attachment.name}: {attachment.skipped_reason}"))
        if attachment.parsed is not None:
            found.extend(_all_warnings(attachment.parsed))
    return found


def partial_reason(doc: ParsedDocument) -> str | None:
    """Why a parsed document is only partly indexed, or None. The first of `status.REASONS["partial"]`
    the parse reports: something cut, pages with no text layer, OCR that failed, attachments skipped."""
    codes = {w.code for w in _all_warnings(doc)}
    if doc.stats.truncated and not codes & {"truncated_pages", "truncated_size"}:
        codes.add("truncated_size")
    if doc.stats.pages_no_text_layer > doc.stats.pages_ocr:
        codes.add("no_text_layer")
    for reason in REASONS["partial"]:
        if reason in codes:
            return reason
    return None


# ── rows ──────────────────────────────────────────────────────────────────────

def _meta(ctx: IndexContext, chunk: Chunk) -> dict[str, Any]:
    """Document metadata (the built-ins are columns, not metadata) ∪ the chunk's own."""
    meta = {k: v for k, v in ctx.metadata.items() if k not in BUILTINS}
    meta.update(chunk.metadata)
    return meta


def _row(ctx: IndexContext, chunk: Chunk, title: str, doc_date: str | None, indexed_at: datetime) -> dict:
    return {
        "chunk_id": chunk.id, "tenant_id": _tenant(), "kb_id": chunk.kb_id, "doc_id": chunk.doc_id,
        "snapshot_id": chunk.snapshot_id, "doc_version": chunk.doc_version, "doc_sha256": chunk.doc_sha256,
        "ordinal": chunk.ordinal, "kind": chunk.kind, "role": chunk.role, "parent_id": chunk.parent_id,
        "content": chunk.content, "context": chunk.context, "heading_path": list(chunk.heading_path),
        "page_start": chunk.page_start, "page_end": chunk.page_end, "char_start": chunk.char_start,
        "char_end": chunk.char_end, "bbox": [b.model_dump() for b in chunk.bbox] if chunk.bbox else None,
        "tokens": chunk.tokens, "lang": chunk.lang, "block_ids": list(chunk.block_ids), "path": ctx.path,
        "source": ctx.source, "mime": ctx.parsed.source.mime, "title": title, "doc_date": doc_date,
        "indexed_at": indexed_at, "meta": _meta(ctx, chunk),
    }


def _fields(row: dict) -> dict[str, Any]:
    fields = {name: row[name] for name in BUILTINS}
    fields["indexed_at"] = row["indexed_at"].isoformat() if row["indexed_at"] else None
    fields.update(row["meta"])
    return fields


async def _write_rows(ctx: IndexContext, rows: list[dict]) -> list[str]:
    """This version's rows in, replacing a half-written earlier attempt; the older versions' chunk ids out."""
    from core.store import session

    tenant = _tenant()
    try:
        async with session() as s:
            older = list((await s.execute(select(_T.c.chunk_id).where(
                _T.c.tenant_id == tenant, _T.c.doc_id == ctx.doc_id, _T.c.snapshot_id == ctx.snapshot_id,
                _T.c.doc_version != ctx.doc_version))).scalars())
            await s.execute(delete(_T).where(
                _T.c.tenant_id == tenant, _T.c.doc_id == ctx.doc_id, _T.c.snapshot_id == ctx.snapshot_id,
                _T.c.doc_version == ctx.doc_version))
            if rows:
                await s.execute(insert(_T), rows)
    except IntegrityError as exc:
        raise KnowledgeIndexError("The document was deleted while it was being indexed.",
                                  retryable=False) from exc
    return older


async def _keep_answered(ctx: IndexContext, sig: str | None, model: str, texts: dict[str, str],
                         embedded: dict[str, list[float]], usage: list[Usage], attribution) -> None:
    """What a stopped embed stage paid for: its cost rows (F329) and its vectors, for the next attempt (F345)."""
    if usage:
        await record_costs([CostRow(u, attribution) for u in usage])
    if sig is not None and embedded:
        await embed_cache.save(ctx.sha256, sig, {embed_cache.entry_key(texts[cid]): v for cid, v in embedded.items()
                                                 if cid in texts}, model=model)


async def _drop_rows(chunk_ids: list[str]) -> None:
    from core.store import session

    async with session() as s:
        for i in range(0, len(chunk_ids), _IN_BATCH):
            await s.execute(delete(_T).where(_T.c.tenant_id == _tenant(),
                                             _T.c.chunk_id.in_(chunk_ids[i:i + _IN_BATCH])))


# ── the entry point ───────────────────────────────────────────────────────────

def _span_attributes(ctx: IndexContext) -> dict:
    return {KNOWLEDGE_KB_ID: ctx.kb.id, KNOWLEDGE_DOC_ID: ctx.doc_id, KNOWLEDGE_SNAPSHOT_ID: ctx.snapshot_id}


@_spans.traced_coroutine(SPAN_INDEX, _span_attributes)
async def index_document(ctx: IndexContext) -> IndexOutcome:
    """Index one document version into `ctx.snapshot_id`. See the module docstring."""
    from core.knowledge.seams import get_knowledge_embedder, get_knowledge_index

    kb = ctx.kb
    stages: list[StageReport] = []

    stage = _Stage("chunk")
    result = chunk_document(ctx.parsed, doc_id=ctx.doc_id, kb_id=kb.id, snapshot_id=ctx.snapshot_id,
                            doc_version=ctx.doc_version, settings=kb.chunking, lang=kb.language)
    chunks = result.chunks
    by_kind: dict[str, int] = {}
    for c in chunks:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    stages.append(stage.report(chunks=len(chunks), by_kind=by_kind, mode=kb.chunking.mode,
                               chunker_version=chunker_version(kb.chunking.mode)))

    stage = _Stage("enrich")
    title, doc_date = _title(ctx), _doc_date(ctx)
    warnings = list(result.warnings)
    enriched: Enriched | None = None
    mode, model = ("prefix", "")
    if kb.enrichment.mode == "contextual":
        mode, model = await built_with(kb, ctx.snapshot_id, ctx.settings)
    if mode == "contextual":
        enriched = await contextualize(ctx, chunks, model=model, title=title)
        warnings.extend(enriched.warnings)
    sentences = enriched.sentences if enriched is not None else {}
    for c in chunks:
        c.context = compose_context(enrich_prefix(title, doc_date, c.heading_path), sentences.get(c.id), c.context)
    if enriched is not None:
        stages.append(stage.report(enriched.status, **enriched.detail))
    else:
        detail: dict[str, Any] = {"mode": "prefix", "version": ENRICHMENT_VERSION}
        if kb.enrichment.mode != "prefix":  # a snapshot made with the prefix before Contextual Retrieval was built
            detail["requested"] = kb.enrichment.mode
            detail["note"] = "this index was made with the deterministic prefix; a rebuild uses Contextual Retrieval"
        if kb.enrichment.extract_metadata:
            detail["extract_metadata"] = "not built yet"
        stages.append(stage.report(**detail))
    # Contextual Retrieval's calls are recorded by `contextualize` as they finish; they join the outcome's cost here.
    enrich_usage: list[Usage] = list(enriched.usage) if enriched is not None else []

    stage = _Stage("embed")
    embedded: dict[str, list[float]] = {}
    usage: list[Usage] = []
    settings = dict(ctx.settings)
    tracker = progress.current()
    kept_sig: str | None = None
    kept: dict[str, list[float]] = {}
    try:
        embedder = get_knowledge_embedder()
        spec = await snapshot_spec(kb, ctx.snapshot_id, settings)
    except EmbedderUnavailable as exc:
        stages.append(stage.report("skipped", reason=exc.code))
    else:
        from core.knowledge.budgets import embed_estimate_usd

        texts = {c.id: c.search_text() for c in chunks}
        # Passages answered by an attempt that stopped part-way — a 402, a limit outlasting its wait, a cancel —
        # are not bought again (F345).
        kept_sig = embed_cache.signature(spec.model, spec.dims, profile_version(spec.model))
        kept = await embed_cache.load(ctx.sha256, kept_sig)
        for c in chunks:
            found = kept.get(embed_cache.entry_key(texts[c.id]))
            if found is not None and (not spec.dims or len(found) == spec.dims):
                embedded[c.id] = found
        todo = [c for c in chunks if c.id not in embedded]
        cached = len(chunks) - len(todo)
        if tracker is not None:
            tracker.stage("embed", total=len(chunks), unit="passages", done=cached)
        await check_budget(kb, await embed_estimate_usd(spec.model, [texts[c.id] for c in todo]))
        attribution = replace(ctx.attribution, snapshot_id=ctx.snapshot_id)
        model, batches = spec.model, 0
        dims = spec.dims or (len(next(iter(embedded.values()))) if embedded else None)
        step = max(1, embedder.max_batch)
        unavailable: EmbedderUnavailable | None = None
        batch: list[Chunk] = []
        try:
            with pacing.paced():  # a provider's rate limit is waited out, slice by slice (PG1)
                for i in range(0, len(todo), step):
                    batch = todo[i:i + step]
                    answer = await embedder.embed([texts[c.id] for c in batch], kind="document", spec=spec,
                                                  settings=settings, attribution=attribution)
                    for c, vector in zip(batch, answer.vectors):
                        embedded[c.id] = vector
                    usage.append(answer.usage)
                    model, dims, batches = answer.model, answer.dims, batches + 1
                    if tracker is not None:
                        tracker.advance(done=cached + i + len(batch))
        except EmbedderUnavailable as exc:  # a key or a server address the model needs is not saved
            unavailable = exc
        except BaseException as exc:
            # The batches already embedded were billed by their provider (settled by our gateway): their cost is
            # recorded before the error goes up, though nothing is indexed (F329) — and their vectors, with what
            # the failing call answered first, are kept for the next attempt (F345). Shielded: a cancelled or
            # timed-out job keeps them too.
            for c, vector in zip(batch, getattr(exc, "answered_vectors", None) or ()):
                embedded.setdefault(c.id, vector)
            await asyncio.shield(_keep_answered(ctx, kept_sig, spec.model, texts, embedded, usage, attribution))
            raise
        if embedded and model and dims:
            await record_embedding(ctx.snapshot_id, model, dims, profile_version=profile_version(model))
        extra = {"cached": cached} if cached else {}
        if unavailable is not None and not embedded:
            stages.append(stage.report("skipped", reason=unavailable.code))
        elif unavailable is not None:
            stages.append(stage.report(model=model, dims=dims, batches=batches, vectors=len(embedded),
                                       reason=unavailable.code, **extra))
        else:
            stages.append(stage.report(model=model, dims=dims, batches=batches, vectors=len(embedded), **extra))

    stage = _Stage("index")
    if tracker is not None:
        tracker.stage("index")
    index = get_knowledge_index()
    indexed_at = datetime.now(timezone.utc)
    rows = [_row(ctx, c, title, doc_date, indexed_at) for c in chunks]
    older = await _write_rows(ctx, rows)
    if rows:
        await index.upsert([IndexEntry(chunk_id=r["chunk_id"], doc_id=ctx.doc_id, kb_id=kb.id,
                                       snapshot_id=ctx.snapshot_id, text=c.search_text(), lang=c.lang,
                                       vector=embedded.get(c.id), fields=_fields(r))
                            for r, c in zip(rows, chunks)])
    if older:
        await index.delete_chunks(Scope(kb.id, ctx.snapshot_id), older)
        await _drop_rows(older)
    stages.append(stage.report(index=index.capabilities.name, chunks=len(rows), replaced=len(older)))
    if kept and kept_sig is not None:  # the rows hold them now
        await embed_cache.drop(ctx.sha256, kept_sig)

    if usage:
        attribution = replace(ctx.attribution, snapshot_id=ctx.snapshot_id)
        await record_costs([CostRow(u, attribution) for u in usage])

    reason = partial_reason(ctx.parsed)
    return IndexOutcome(status="partial" if reason else "indexed", chunks=len(chunks),
                        tokens=sum(c.tokens for c in chunks), status_reason=reason, stages=stages,
                        usage=[*enrich_usage, *usage], warnings=warnings)
