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
and section, versioned `snapshots.ENRICHMENT_VERSION`. A KB that opted into
Contextual Retrieval still gets the prefix and the stage says so, until that
mode is built.

**Embedding** uses the snapshot's own model once it has recorded one, else the
knowledge base's, else the deployment's (`embedders.snapshot_spec`, D163 #3), so
one snapshot never holds two models' vectors. With no model configured, or a key
or server address the model needs not saved (`EmbedderUnavailable`), the stage is
skipped with that reason and the chunks are searchable by keyword only, until
`backfill.embed_missing` embeds them on a later reconcile.
"""
from __future__ import annotations

import posixpath
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError

from core.knowledge.chunker import chunk_document, chunker_version
from core.knowledge.chunks import Chunk
from core.knowledge.embedders import profile_version, snapshot_spec
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
    for c in chunks:
        prefix = enrich_prefix(title, doc_date, c.heading_path)
        c.context = f"{prefix} · {c.context}" if prefix and c.context else (prefix or c.context)
    detail: dict[str, Any] = {"mode": "prefix", "version": ENRICHMENT_VERSION}
    if kb.enrichment.mode != "prefix":
        detail["requested"] = kb.enrichment.mode
        detail["note"] = "contextual enrichment is not built yet; the deterministic prefix was used"
    stages.append(stage.report(**detail))

    stage = _Stage("embed")
    embedded: dict[str, list[float]] = {}
    usage: list[Usage] = []
    settings = dict(ctx.settings)
    try:
        embedder = get_knowledge_embedder()
        spec = await snapshot_spec(kb, ctx.snapshot_id, settings)
    except EmbedderUnavailable as exc:
        stages.append(stage.report("skipped", reason=exc.code))
    else:
        from core.knowledge.budgets import embed_estimate_usd

        await check_budget(kb, await embed_estimate_usd(spec.model, [c.search_text() for c in chunks]))
        attribution = replace(ctx.attribution, snapshot_id=ctx.snapshot_id)
        model, dims, batches = spec.model, spec.dims, 0
        step = max(1, embedder.max_batch)
        unavailable: EmbedderUnavailable | None = None
        try:
            for i in range(0, len(chunks), step):
                batch = chunks[i:i + step]
                answer = await embedder.embed([c.search_text() for c in batch], kind="document", spec=spec,
                                              settings=settings, attribution=attribution)
                for c, vector in zip(batch, answer.vectors):
                    embedded[c.id] = vector
                usage.append(answer.usage)
                model, dims, batches = answer.model, answer.dims, batches + 1
        except EmbedderUnavailable as exc:  # a key or a server address the model needs is not saved
            unavailable = exc
        if embedded and model and dims:
            await record_embedding(ctx.snapshot_id, model, dims, profile_version=profile_version(model))
        if unavailable is not None and not embedded:
            stages.append(stage.report("skipped", reason=unavailable.code))
        elif unavailable is not None:
            stages.append(stage.report(model=model, dims=dims, batches=batches, vectors=len(embedded),
                                       reason=unavailable.code))
        else:
            stages.append(stage.report(model=model, dims=dims, batches=batches, vectors=len(embedded)))

    stage = _Stage("index")
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

    if usage:
        attribution = replace(ctx.attribution, snapshot_id=ctx.snapshot_id)
        await record_costs([CostRow(u, attribution) for u in usage])

    reason = partial_reason(ctx.parsed)
    return IndexOutcome(status="partial" if reason else "indexed", chunks=len(chunks),
                        tokens=sum(c.tokens for c in chunks), status_reason=reason, stages=stages,
                        usage=usage, warnings=result.warnings)
