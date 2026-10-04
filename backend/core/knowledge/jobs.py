"""The ingestion job's entry point — what every dispatcher runs.

`INGEST_ENTRYPOINT` (`core.knowledge.seams`) names `run_ingest_job`: OSS's
in-process dispatcher awaits it; cloud enqueues a job that calls it in a
worker, with the tenant already bound. Only the signature is contract (KB0);
the rest is A1's (D157).

For one document (`upsert`, `retry`):

    queued → parsing → indexing → indexed | partial
                  ↘ skipped (over a cap, unsupported, empty)
                            ↘ failed | paused (a typed error from indexing)

Every step is a compare-and-set, so a job that loses a race — to a delete, or
to a newer write that queued the document again — stops without writing over
the winner. Ingestion owns the document's status and its report; indexing
(`pipeline_contract`, B1) owns chunks and never touches the status. Until
indexing is installed, a document ends `failed` / `internal`, saying so.

`index_document` gets `doc_version = stored version + 1` on every attempt; the
stored version moves only on success. So a retry after a failure writes the
same derived chunk ids the failed attempt did, and a success always supersedes
what it replaces.

Imports are function-local: cloud's API process imports this module, and the
contract's import-hygiene test holds it to the standard library and the pure
Knowledge modules.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.knowledge.status import IngestionReport
from core.knowledge.types import IngestJob

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase
    from core.store.models import KnowledgeDocumentDB

MIB = 1024 * 1024
SNAPSHOT_ENTRYPOINT = "core.knowledge.snapshots:serving_snapshot"


async def run_ingest_job(job: IngestJob) -> IngestionReport | None:
    """Do `job`. Returns the document's report, or None for work that is not one document
    (a reindex, a KB delete)."""
    from core.tenancy import get_tenant, tenant_scope

    if job.tenant != get_tenant():
        with tenant_scope(job.tenant):
            return await _run(job)
    return await _run(job)


async def _run(job: IngestJob) -> IngestionReport | None:
    from core.knowledge import bases, dispatch, documents, progress
    from core.knowledge.config import knowledge_enabled
    from core.log import get_logger

    # A job the in-process dispatcher spawned from inside another's work starts with a copy of that work's
    # context: it must never report into the other's progress (PG1).
    progress.detach()
    dispatch.started(job)
    if not knowledge_enabled():
        get_logger(__name__).info(f"knowledge is off; job {job.dedup_key()} left for reconcile")
        return None
    if job.op == "delete" and job.path is None and job.doc_id is None:
        await drop_base(job.kb_id)
        return None
    kb = await bases.get_base(job.kb_id)
    if kb is None:
        return None
    if job.op == "reindex" and job.snapshot_id:  # one step of a blue/green rebuild (B2b)
        from core.knowledge import snapshots

        await snapshots.build_step(kb, job.snapshot_id)
        return None
    if job.op == "reindex":
        from core.knowledge import sync

        await sync.reconcile(kb)
        return None
    if job.op == "resume":
        await _resume(kb)
        return None
    row = await (documents.get(job.doc_id) if job.doc_id else documents.get_by_path(kb.id, job.path or ""))
    if row is None or row.kb_id != kb.id:
        return None
    if job.op == "delete" or row.status == "deleting":
        await delete_document(kb, row)
        return None
    if kb.status != "active":
        return None  # a disabled base is not followed; switching it back on reconciles what waits
    return await _ingest(kb, row)


# ── one document ───────────────────────────────────────────────────────────────

async def _ingest(kb: KnowledgeBase, row: KnowledgeDocumentDB) -> IngestionReport | None:
    import asyncio
    import hashlib
    from datetime import datetime, timezone

    from core.knowledge import documents
    from core.knowledge.report import ReportBuilder
    from core.storage import get_blob_store

    if row.status != "queued":
        return None  # another job has it, or it is resting: nothing asked for
    data = await asyncio.to_thread(get_blob_store().get_bytes, f"vault/{row.path}")
    if data is None and row.source == "web" and row.sha256 is None:
        # A web page that never had a file (S1) — refused or failed, then queued again by Retry or a
        # restart: fetch it. It comes back written (and queued for indexing) or resting with why.
        from core.knowledge.web import fetch_document

        await fetch_document(kb, row)
        return None
    if data is None:  # the file went away while the document waited
        gone = await documents.tombstone(row.id)
        if gone is not None:
            await delete_document(kb, gone)
        return None
    sha = hashlib.sha256(data).hexdigest()
    attempt = (row.attempts or 0) + 1
    builder = ReportBuilder(doc_id=row.id, path=row.path, attempt=attempt, sha256=sha)
    now = datetime.now(timezone.utc)
    parsing = await documents.transition(row.id, "parsing", previous="queued", attempts=attempt, started_at=now,
                                         sha256=sha, size_bytes=len(data))
    if parsing is None:
        return None
    from core.knowledge import progress

    # From here the document is at work: a screen follows it stage by stage (PG1, F357).
    async with progress.tracking(kb.id, row.id, doc_id=row.id):
        return await _parse_and_index(kb, row, parsing, data, sha, builder)


async def _parse_and_index(kb: KnowledgeBase, row: KnowledgeDocumentDB, parsing: KnowledgeDocumentDB, data: bytes,
                           sha: str, builder) -> IngestionReport | None:
    from core.knowledge import documents
    from core.knowledge.config import knowledge_settings
    from core.knowledge.errors import BudgetExceeded, WalletExhausted
    from core.knowledge.parse import UnsupportedFormat
    from core.knowledge.parse.limits import ParseRefused
    from core.knowledge.parse.service import parse_document
    from core.knowledge.report import retry_hint
    from core.knowledge.status import ErrorInfo, PagesReport
    from core.knowledge.types import Attribution

    settings = knowledge_settings()

    async def rest(status: str, reason: str | None, error: str | None = None, **fields) -> IngestionReport | None:
        report = builder.finish(status, reason, **fields)
        done = await documents.transition(row.id, status, reason=reason, error=error, previous=parsing.status,
                                          report=report.model_dump(mode="json"))
        if done is not None and status == "paused":
            await _paused(kb.id, reason)
        return report if done is not None else None

    if len(data) > settings.max_file_mb * MIB:
        builder.add(_skipped_stage("parse", size_bytes=len(data), cap_mb=settings.max_file_mb))
        return await rest("skipped", "over_size_cap",
                          f"The file is {len(data) / MIB:.1f} MB; the limit is {settings.max_file_mb} MB.")

    attribution = Attribution(kb_id=kb.id, doc_id=row.id, source="knowledge",
                              session_id=Attribution.ingestion_session(kb.id))
    try:
        with builder.stage("parse") as stage:
            result = await parse_document(
                data, path=row.path, source=row.source, sha256=sha, mime=row.mime or "", url=row.source_url,
                modified_at=row.source_modified_at.isoformat() if row.source_modified_at else None,
                kb=kb, attribution=attribution, heartbeat=lambda: documents.heartbeat(row.id))
            parsed = result.parsed
            stage.detail.update(parser=parsed.parser.name, chars=parsed.stats.chars, blocks=len(parsed.blocks),
                                **result.detail)
            if parsed.stats.truncated:
                stage.partial(truncated=parsed.stats.truncated_reason)
    except UnsupportedFormat as exc:
        builder.report.stages[-1] = builder.report.stages[-1].model_copy(update={"status": "skipped", "error": None,
                                                                                 "detail": {"reason": str(exc)}})
        return await rest("skipped", "unsupported_format", str(exc))
    except ParseRefused as exc:  # encrypted, over the page cap, a zip bomb, a timeout or crash with nothing read
        builder.report.stages[-1] = builder.report.stages[-1].model_copy(update={
            "status": "skipped" if exc.status == "skipped" else "failed",
            "error": ErrorInfo(code=exc.reason, message=str(exc)[:2000], retryable=retry_hint(exc.reason).allowed)})
        return await rest(exc.status, exc.reason, str(exc))
    except (BudgetExceeded, WalletExhausted) as exc:  # OCR found the budget spent or the wallet empty
        status, reason = failure_status(exc)
        return await rest(status, reason, str(exc))
    except Exception as exc:  # noqa: BLE001 — a parser that crashes fails its document, not the worker
        return await rest("failed", "parser_crashed", f"{type(exc).__name__}: {exc}")
    if result.ocr_stage is not None:
        builder.add(result.ocr_stage)

    removed = {w.code: w.count for w in parsed.warnings
               if w.code in ("zero_width_removed", "html_comments_removed", "hidden_text_removed")}
    if row.source == "web":  # a fetched page's extraction left these out before it was written (S1)
        from core.knowledge.web import fetched_counts

        removed.update(fetched_counts(parsed.text))
    builder.add(_ok_stage("sanitize", **removed))
    pages = PagesReport(total=len(parsed.pages), with_text=len(parsed.pages) - parsed.stats.pages_no_text_layer,
                        no_text_layer=parsed.stats.pages_no_text_layer, ocr_applied=parsed.stats.pages_ocr,
                        ocr_failed=sum(w.count for w in parsed.warnings if w.code == "ocr_failed"))
    common = dict(pages=pages, warnings=parsed.warnings, parser=parsed.parser)
    if len(parsed.pages) > settings.max_file_pages:
        return await rest("skipped", "over_page_cap",
                          f"The file has {len(parsed.pages)} pages; the limit is {settings.max_file_pages}.", **common)
    if not parsed.blocks:
        scanned = parsed.stats.pages_no_text_layer - parsed.stats.pages_ocr
        if not scanned:
            return await rest("skipped", "empty_document", "The file has no text to index.", **common)
        from core.knowledge.seams import get_ocr_provider

        why = "OCR did not read them" if get_ocr_provider() is not None else "this install has no OCR to read them"
        return await rest("skipped", "empty_document",
                          f"All {scanned} page(s) are images with no text layer; {why}.", **common)

    indexing = await documents.transition(row.id, "indexing", previous="parsing")
    if indexing is None:
        return None
    return await _index(kb, indexing, parsed, sha, builder, common, parse_usage=result.usage)


async def _index(kb, row, parsed, sha, builder, common, *, parse_usage=()) -> IngestionReport | None:
    from datetime import datetime, timezone

    from core.config import load_settings
    from core.knowledge import documents
    from core.knowledge.parse.service import _beating
    from core.knowledge.pipeline_contract import IndexContext, resolve_index_entrypoint
    from core.knowledge.report import error_info
    from core.knowledge.status import REASONS, ChunksReport, CostReport, StageReport
    from core.knowledge.types import Attribution
    from core.log import get_logger

    version = (row.version or 0) + 1
    metadata = _record_metadata(row, parsed)
    snapshot_id = None
    try:
        entry = resolve_index_entrypoint()
        snapshot_id = await _serving_snapshot(kb)
        ctx = IndexContext(
            kb=await _as_built(kb, snapshot_id), doc_id=row.id, doc_version=version, sha256=sha, path=row.path, source=row.source, parsed=parsed,
            snapshot_id=snapshot_id, settings=load_settings(), metadata=metadata,
            attribution=Attribution(kb_id=kb.id, doc_id=row.id, snapshot_id=snapshot_id, source="knowledge",
                                    session_id=Attribution.ingestion_session(kb.id)))
        # Indexing can take a while — Contextual Retrieval on a long document, a provider's rate limit waited out,
        # a large write: the whole call keeps the document from looking stuck to reconcile (PG1).
        async with _beating(lambda: documents.heartbeat(row.id, status="indexing")):
            outcome = await entry(ctx)
    except Exception as exc:  # noqa: BLE001 — every failure ends the document somewhere, with its reason
        builder.add(StageReport(stage="index", status="failed", error=error_info(exc)))
        status, reason = failure_status(exc)
        report = builder.finish(status, reason, snapshot_id=snapshot_id, **common)
        done = await documents.transition(row.id, status, reason=reason, error=str(exc)[:2000], previous="indexing",
                                          report=report.model_dump(mode="json"))
        if done is not None and status == "paused":
            await _paused(kb.id, reason)
        return report

    reason = outcome.status_reason
    if reason is not None and reason not in REASONS.get(outcome.status, ()):
        get_logger(__name__).warning(f"indexing gave {outcome.status!r} an unknown reason {reason!r}; dropped")
        reason = None
    for stage in outcome.stages:
        builder.add(stage)
    usage = [*parse_usage, *outcome.usage]  # OCR's cost, then indexing's
    charged = [u.charged_microusd for u in usage if u.charged_microusd is not None]
    by_stage: dict[str, float] = {}
    for u in usage:
        by_stage[u.stage] = by_stage.get(u.stage, 0.0) + u.est_usd
    report = builder.finish(
        outcome.status, reason, snapshot_id=snapshot_id,
        chunks=ChunksReport(count=outcome.chunks, tokens=outcome.tokens),
        cost=CostReport(est_usd=round(sum(u.est_usd for u in usage), 8),
                        charged_microusd=sum(charged) if charged else None, by_stage=by_stage),
        **{**common, "warnings": [*common["warnings"], *outcome.warnings]})
    await documents.transition(
        row.id, outcome.status, reason=reason, previous="indexing", version=version, chunks=outcome.chunks,
        tokens=outcome.tokens, pages=len(parsed.pages) or None, snapshot_id=snapshot_id,
        indexed_at=datetime.now(timezone.utc), title=row.title or parsed.title, doc_date=row.doc_date or parsed.doc_date,
        meta=metadata, parser=f"{parsed.parser.name}/{parsed.parser.version}", report=report.model_dump(mode="json"))
    return report


def failure_status(exc: Exception) -> tuple[str, str]:
    """The status and reason a failed indexing call leaves its document in.

    An empty wallet or a spent budget pauses — never a retry loop; a
    `KnowledgeUnavailable` (a part not installed) and anything unexpected are
    `internal`, with the message saying what.
    """
    from core.knowledge.errors import (
        BudgetExceeded, EmbeddingError, EmbeddingInvalid, EnrichmentError, KnowledgeIndexError, WalletExhausted,
    )

    if isinstance(exc, WalletExhausted):
        return "paused", "wallet_402"
    if isinstance(exc, BudgetExceeded):
        return "paused", "budget_cap"
    if isinstance(exc, EmbeddingInvalid):
        return "failed", "embedding_invalid"
    if isinstance(exc, EmbeddingError):
        return "failed", "embed_failed"
    if isinstance(exc, KnowledgeIndexError):
        return "failed", "index_failed"
    if isinstance(exc, EnrichmentError):
        return "failed", "enrich_failed"
    return "failed", "internal"


async def _paused(kb_id: str, reason: str | None) -> None:
    """A document paused on a budget or an empty wallet: the knowledge base's first pause is announced (B2b)."""
    from core.knowledge.budgets import on_document_paused

    await on_document_paused(kb_id, reason)


def _record_metadata(row, parsed) -> dict:
    """The document's metadata: what the API validated, else a record file's own (validated here),
    since a record can also be written straight into the Vault."""
    from core.knowledge.errors import FilterError
    from core.knowledge.filters import check_metadata
    from core.log import get_logger

    if row.meta or parsed.parser.name != "record" or not parsed.metadata:
        return dict(row.meta or {})
    try:
        return check_metadata({k: v for k, v in parsed.metadata.items() if k not in ("title", "doc_date")})
    except FilterError as exc:
        get_logger(__name__).warning(f"record {row.path}: its metadata is not used ({exc})")
        return {}


async def _serving_snapshot(kb: KnowledgeBase) -> str:
    """The snapshot a new version is indexed into (B1's `snapshots.serving_snapshot`)."""
    import inspect

    from core.knowledge.errors import KnowledgeUnavailable
    from core.knowledge.seams import _load

    serving = _load(SNAPSHOT_ENTRYPOINT)
    if serving is None:
        raise KnowledgeUnavailable("Knowledge snapshots are not built yet (lane B1).")
    result = serving(kb)
    return await result if inspect.isawaitable(result) else result


async def _as_built(kb: KnowledgeBase, snapshot_id: str) -> KnowledgeBase:
    """`kb` with the index settings its serving snapshot was built with: while a rebuild for new settings
    runs, a new version still joins the serving snapshot the way the rest of it was made (B2b)."""
    from core.knowledge.snapshots import as_built

    return await as_built(kb, snapshot_id)


def _ok_stage(name: str, **detail):
    from core.knowledge.status import StageReport

    return StageReport(stage=name, status="ok", detail=detail)


def _skipped_stage(name: str, **detail):
    from core.knowledge.status import StageReport

    return StageReport(stage=name, status="skipped", detail=detail)


# ── deleting ───────────────────────────────────────────────────────────────────

async def delete_document(kb: KnowledgeBase, row: KnowledgeDocumentDB) -> bool:
    """Take a document out of the index, then forget it. Keeps the tombstone if the index refuses,
    so reconcile tries again; a file written again meanwhile is queued afresh."""
    from core.knowledge import dispatch, documents
    from core.knowledge.errors import KnowledgeUnavailable
    from core.knowledge.seams import get_knowledge_index
    from core.log import get_logger

    if row.status != "deleting":
        row = await documents.tombstone(row.id)
        if row is None:
            return False
    try:
        await get_knowledge_index().delete_document(kb.id, row.id)
    except KnowledgeUnavailable:
        pass  # no index is installed: nothing holds this document but our own rows, which go below
    except Exception as exc:  # noqa: BLE001 — the tombstone stays; reconcile retries the delete
        get_logger(__name__).warning(f"index delete of {row.id} failed; kept as a tombstone: {exc}")
        return False
    if not await documents.delete_row(row):
        return False
    from core.knowledge import progress

    await progress.forget([progress.key(kb.id, row.id)])  # a row a run that died left behind (PG1)
    await _forget_parse(row.sha256)
    if await _exists(row.path):  # written again while it was being deleted
        fresh, queued = await documents.upsert_for_path(kb, row.path, source="vault", sha256=None, size=None)
        if queued:
            await dispatch.enqueue(kb.id, "upsert", "vault_write", path=fresh.path, doc_id=fresh.id)
    return True


async def drop_base(kb_id: str) -> None:
    """A deleted knowledge base: its crawls and their state, its index, its documents and snapshots,
    then its record. The Vault folder and its files stay (Naveen, D157)."""
    from core.knowledge import bases, documents
    from core.knowledge.errors import KnowledgeUnavailable
    from core.knowledge.seams import get_knowledge_index

    kb = await bases.get_base(kb_id)
    if kb is not None:
        from core.knowledge.web import forget_base

        await forget_base(kb)  # S1: no crawl keeps writing into a base that is going
    await documents.tombstone_under(kb_id)
    try:
        await get_knowledge_index().drop_kb(kb_id)
    except KnowledgeUnavailable:
        pass
    contents = {row.sha256 for row in await documents.list_for(kb_id)}
    await documents.delete_all_rows(kb_id)
    from core.knowledge import progress

    await progress.forget_base(kb_id)
    for sha in contents:
        await _forget_parse(sha)
    await bases.delete_base_record(kb_id)


async def _resume(kb: KnowledgeBase) -> None:
    """Queue a knowledge base's paused documents again (its budget was raised, or a month began)."""
    from core.knowledge import dispatch, documents

    for row in await documents.list_for(kb.id, status="paused"):
        again = await documents.transition(row.id, "queued", previous="paused")
        if again is not None:
            await dispatch.enqueue(kb.id, "upsert", "budget_resume", path=again.path, doc_id=again.id)


async def _forget_parse(sha256: str | None) -> None:
    """A deleted document's parse goes with it, unless another document has the same content (A2)."""
    from core.knowledge.parse.cache import forget_unreferenced
    from core.log import get_logger

    try:
        await forget_unreferenced(sha256)
    except Exception as exc:  # noqa: BLE001 — the hourly sweep removes what this misses
        get_logger(__name__).warning(f"parse cache entry for {str(sha256)[:12]} not removed: {exc}")


async def _exists(path: str) -> bool:
    import asyncio

    from core.storage import get_blob_store

    return await asyncio.to_thread(get_blob_store().exists, f"vault/{path}")
