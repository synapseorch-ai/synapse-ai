"""Knowledge's internal API: knowledge bases, uploads, records and documents (A1, D157),
search and a search's trace (B1b, D161), web sources and their crawls (S1, D165), and a base's eval:
question sets, drafts, runs (V1, D168).

Paths are `/api` + the frozen route table (`core.knowledge.api_models.ROUTES`),
with its bodies and status codes; a test holds the two equal. The cost preview
and a base's costs are B2b's (D167). Replay is frozen but not built (F339: listed
for after release), so it is not mounted. The public API serves the table's v1
rows on these same handlers (`api_v1_knowledge.py`, D176).

Knowledge is off until its release: while `knowledge_enabled` is false every
route here answers 404, as if it did not exist.
"""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from core.knowledge.api_models import (
    CostGroup, CostPreview, CostPreviewRequest, CrawlAccepted, DeleteAccepted, DocumentDetail, DocumentOut,
    EvalCompareRequest, EvalConfigResult, EvalDraftRequest, EvalDrafts, EvalEstimate, EvalEstimateRequest,
    EvalPromote, EvalQuery, EvalRun, EvalRunRequest, EvalRunSummary, EvalSet, EvalSetCreate, EvalSetSummary,
    EvalSetUpdate, IngestAccepted,
    KnowledgeBaseCreate, KnowledgeBaseOut, KnowledgeBaseUpdate, RecordIngestRequest, SearchRequest, SearchResponse,
    UploadComplete, UploadInit, UploadSession, UrlIngestRequest, WebCrawlState,
)
from core.knowledge.trace import Trace

#: Search's own "not found" codes (`core.knowledge.search`): a base or a pinned snapshot the
#: search names does not exist.
_NOT_FOUND_CODES = frozenset({"kb_not_found", "snapshot_not_found", "eval_not_found"})
#: A question set's run is under way (V1).
_CONFLICT_CODES = frozenset({"eval_busy"})


def _require_enabled() -> None:
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(dependencies=[Depends(_require_enabled)])


def _errors(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Knowledge's typed errors as HTTP answers."""
    from core.knowledge.bases import UnknownKnowledgeBase
    from core.knowledge.errors import FilterError, KnowledgeError, KnowledgePathError, StaleRevision
    from core.knowledge.ingest import NotFound, UploadError

    @functools.wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except (UnknownKnowledgeBase, NotFound) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StaleRevision as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UploadError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except FilterError as exc:
            raise HTTPException(status_code=422, detail=[{"type": f"filter.{exc.filter_code}", "loc": ["body"],
                                                          "msg": str(exc)}]) from exc
        except (KnowledgePathError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KnowledgeError as exc:  # last: the ones above are its subclasses
            if exc.code in _NOT_FOUND_CODES:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if exc.code in _CONFLICT_CODES:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise

    return wrapped


async def _out(kbs: list) -> list[KnowledgeBaseOut]:
    from core.knowledge import bases

    states = await bases.states(kbs)
    return [KnowledgeBaseOut(**kb.model_dump(), state=states[kb.id]) for kb in kbs]


async def _document(doc_id: str):
    from core.knowledge import documents

    row = await documents.get(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such document.")
    return row


# ── knowledge bases ────────────────────────────────────────────────────────────

@router.get("/api/knowledge/bases", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases() -> list[KnowledgeBaseOut]:
    from core.knowledge import bases

    return await _out(await bases.list_bases())


@router.post("/api/knowledge/bases", response_model=KnowledgeBaseOut, status_code=201)
@_errors
async def create_knowledge_base(body: KnowledgeBaseCreate) -> KnowledgeBaseOut:
    """A new knowledge base on a Vault folder; the folder is created if missing, and what is already
    in it is queued."""
    from core.knowledge import bases

    # 422 before anything is saved; the builder's create_knowledge_base makes one the same way (F1200).
    return (await _out([await bases.make_base(body)]))[0]


@router.get("/api/knowledge/bases/{kb_id}", response_model=KnowledgeBaseOut)
@_errors
async def get_knowledge_base(kb_id: str) -> KnowledgeBaseOut:
    from core.knowledge import bases

    return (await _out([await bases.require_base(kb_id)]))[0]


@router.patch("/api/knowledge/bases/{kb_id}", response_model=KnowledgeBaseOut)
@_errors
async def update_knowledge_base(kb_id: str, body: KnowledgeBaseUpdate) -> KnowledgeBaseOut:
    """Edit from the revision the edit started at (409 if it is stale). A new folder moves the documents;
    switching a base back on reconciles it. A change to how the index is built (chunking, embedding,
    enrichment, language) rebuilds it beside the serving one, which keeps answering until the new one is
    complete (`reindex_started`; B2b). Any edit retries a rebuild that failed, and a raised or cleared
    budget resumes what it paused."""
    from core.knowledge import bases, budgets, costs, dispatch, snapshots, sync

    costs.check_settings(embedding=body.embedding, rerank=body.rerank,
                         enrichment=body.enrichment)  # only the blocks the edit sends
    before, after = await bases.update_base(kb_id, body)
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
    out = (await _out([after]))[0]
    return out.model_copy(update={"reindex_started": started})


@router.delete("/api/knowledge/bases/{kb_id}", response_model=DeleteAccepted, status_code=202)
@_errors
async def delete_knowledge_base(kb_id: str) -> DeleteAccepted:
    """Forget the knowledge base: its index, documents and snapshots. Its Vault folder and files stay."""
    from core.knowledge import bases
    from core.knowledge.jobs import drop_base

    from core.knowledge import evals

    await bases.require_base(kb_id)
    await drop_base(kb_id)
    await evals.forget_base(kb_id)  # V1: its question sets and runs go with it
    return DeleteAccepted()


# ── what it costs (B2b, D167) ──────────────────────────────────────────────────

@router.post("/api/knowledge/bases/{kb_id}/cost-preview", response_model=CostPreview)
@_errors
async def preview_costs(kb_id: str, body: CostPreviewRequest) -> CostPreview:
    """What re-indexing the base's documents under its settings, with `overrides` laid over them (a draft
    the settings screen has not saved), would cost: OCR, embedding, Contextual Retrieval, and the budget left.
    Starts nothing."""
    from core.knowledge import bases, costs

    return await costs.preview(await bases.require_base(kb_id), body.overrides)


@router.get("/api/knowledge/bases/{kb_id}/costs", response_model=list[CostGroup])
@_errors
async def list_costs(kb_id: str, group_by: str = Query("day"), since: str | None = None, until: str | None = None,
                     limit: int = Query(1000, ge=1, le=1000)) -> list[CostGroup]:
    """What the base has cost, grouped by `day` (UTC), `stage`, `document` ("search" for searches) or `model`,
    from `since` (ISO date or time; the month's start) to `until` (now)."""
    from datetime import datetime

    from core.knowledge import bases, costs

    await bases.require_base(kb_id)
    try:
        start = datetime.fromisoformat(since) if since else None
        end = datetime.fromisoformat(until) if until else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="since and until are ISO dates or times.") from exc
    return await costs.report(kb_id, group_by=group_by, since=start, until=end, limit=limit)


# ── uploads ────────────────────────────────────────────────────────────────────

async def open_upload(kb_id: str, body: UploadInit, *, part_url: Callable[[str, int], str],
                      token_header: str | None = None) -> UploadSession:
    """An upload session into an active base; the public API's start opens its own the same way."""
    from core.knowledge import bases, ingest

    kb = await bases.require_base(kb_id)
    if kb.status != "active":
        raise HTTPException(status_code=409, detail="This knowledge base is disabled.")
    return await ingest.start_upload(kb, body, part_url=part_url, token_header=token_header)


async def store_part(upload_id: str, part_number: int, request: Request, *, token: str | None = None) -> Response:
    """Store one part from the request body and answer with its ETag, as S3 does (header and body)."""
    import json

    from core.knowledge import ingest

    etag = await ingest.put_part(upload_id, part_number, request.stream(), token=token)
    return Response(content=json.dumps({"part_number": part_number, "etag": etag}), media_type="application/json",
                    headers={"ETag": f'"{etag}"'})


@router.post("/api/knowledge/bases/{kb_id}/uploads", response_model=UploadSession, status_code=201)
@_errors
async def start_upload(kb_id: str, body: UploadInit) -> UploadSession:
    """Open an upload of one file. PUT each part to its URL, then complete."""
    return await open_upload(kb_id, body,
                             part_url=lambda upload_id, n: f"/api/knowledge/uploads/{upload_id}/parts/{n}")


@router.put("/api/knowledge/uploads/{upload_id}/parts/{part_number}", status_code=200)
@_errors
async def put_upload_part(upload_id: str, part_number: int, request: Request) -> Response:
    """One part's bytes, as the request body. Answers with the part's ETag, which complete names."""
    return await store_part(upload_id, part_number, request)


@router.post("/api/knowledge/uploads/{upload_id}/complete", response_model=IngestAccepted, status_code=202)
@_errors
async def complete_upload(upload_id: str, body: UploadComplete) -> IngestAccepted:
    from core.knowledge import documents, ingest

    rows = await ingest.complete_upload(upload_id, body)
    return IngestAccepted(documents=[documents.to_out(r) for r in rows])


@router.delete("/api/knowledge/uploads/{upload_id}", status_code=204, response_model=None)
@_errors
async def cancel_upload(upload_id: str) -> Response:
    """Give up an upload that will not be completed: its parts go (F341, CCR-12)."""
    from core.knowledge import ingest

    await ingest.cancel_upload(upload_id)
    return Response(status_code=204)


# ── records ────────────────────────────────────────────────────────────────────

@router.post("/api/knowledge/bases/{kb_id}/records", response_model=IngestAccepted, status_code=202)
@_errors
async def put_records(kb_id: str, body: RecordIngestRequest) -> IngestAccepted:
    """Records as text or JSON with metadata. The same id replaces its record."""
    from core.knowledge import bases, documents, ingest

    kb = await bases.require_base(kb_id)
    if kb.status != "active":
        raise HTTPException(status_code=409, detail="This knowledge base is disabled.")
    rows = await ingest.put_records(kb, body)
    return IngestAccepted(documents=[documents.to_out(r) for r in rows])


@router.delete("/api/knowledge/bases/{kb_id}/records/{record_id}", response_model=DeleteAccepted,
               status_code=202)
@_errors
async def delete_record(kb_id: str, record_id: str) -> DeleteAccepted:
    from core.knowledge import bases, ingest

    await ingest.delete_record(await bases.require_base(kb_id), record_id)
    return DeleteAccepted()


# ── web pages ──────────────────────────────────────────────────────────────────

@router.post("/api/knowledge/bases/{kb_id}/urls", response_model=CrawlAccepted, status_code=202)
@_errors
async def add_urls(kb_id: str, body: UrlIngestRequest) -> CrawlAccepted:
    """Public web pages as sources: a page, a sitemap's pages, or a site crawled from a URL. Each URL is
    recorded in the base's `web.sources` (the same URL and kind again crawls it again now), and its
    crawl starts; the pages arrive as documents."""
    from core.knowledge import bases, web

    kb = await bases.require_base(kb_id)
    if kb.status != "active":
        raise HTTPException(status_code=409, detail="This knowledge base is disabled.")
    return await web.add_urls(kb, body)


@router.get("/api/knowledge/bases/{kb_id}/crawls", response_model=list[WebCrawlState])
@_errors
async def list_crawls(kb_id: str) -> list[WebCrawlState]:
    """Each web source of the base with its latest crawl: status, counts, when it is next due."""
    from core.knowledge import bases, web

    return await web.crawl_states(await bases.require_base(kb_id))


# ── documents ──────────────────────────────────────────────────────────────────

@router.get("/api/knowledge/bases/{kb_id}/documents", response_model=list[DocumentOut])
@_errors
async def list_documents(kb_id: str, after: str | None = None, limit: int = Query(100, ge=1, le=1000),
                         status: str | None = None, path_prefix: str | None = None) -> list[DocumentOut]:
    """A base's documents by id, paged with `after` (the last id seen); a working one says how far it has got."""
    from core.knowledge import bases, documents, progress

    await bases.require_base(kb_id)
    rows = await documents.list_for(kb_id, after=after, limit=limit, status=status, path_prefix=path_prefix)
    return await progress.attach([documents.to_out(r) for r in rows])


@router.get("/api/knowledge/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str) -> DocumentDetail:
    """A document with its ingestion report — what happened to it, stage by stage — and, while it works, how far
    it has got."""
    from core.knowledge import documents, progress

    (detail,) = await progress.attach([documents.to_detail(await _document(doc_id))])
    return detail


@router.post("/api/knowledge/documents/{doc_id}/retry", response_model=DocumentOut, status_code=202)
async def retry_document(doc_id: str) -> DocumentOut:
    from core.knowledge import documents, ingest

    again = await ingest.retry_document(await _document(doc_id))
    if again is None:
        raise HTTPException(status_code=409, detail="This document is being worked on; retry once it rests.")
    return documents.to_out(again)


@router.delete("/api/knowledge/documents/{doc_id}", response_model=DeleteAccepted, status_code=202)
async def delete_document(doc_id: str) -> DeleteAccepted:
    """Delete the document's Vault file, and with it the document (a file left behind would be
    indexed again by the next reconcile)."""
    from core.knowledge import ingest

    await ingest.delete_document(await _document(doc_id))
    return DeleteAccepted()


# ── search ─────────────────────────────────────────────────────────────────────

@router.post("/api/knowledge/search", response_model=SearchResponse)
@_errors
async def search_knowledge(body: SearchRequest) -> SearchResponse:
    """Search knowledge bases, as the Knowledge screen's test search does: labelled hits with their
    citations. The search is traced as the UI's (`origin.surface == "ui"`)."""
    from core.knowledge import search
    from core.knowledge.trace import Origin

    return await search.search(body, origin=Origin(surface="ui"))


@router.get("/api/knowledge/queries/{query_id}", response_model=Trace)
async def get_query_trace(query_id: str) -> Trace:
    """A search's trace: the request, every leg's candidates, the cuts and the hits (30 days)."""
    from core.knowledge import trace_store

    trace = await trace_store.get(query_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="No such query.")
    return trace


# ── evaluation: test a knowledge base on its own documents (V1, D168) ──────────

@router.get("/api/knowledge/bases/{kb_id}/eval/sets", response_model=list[EvalSetSummary])
@_errors
async def list_eval_sets(kb_id: str) -> list[EvalSetSummary]:
    """The base's question sets, each with its newest run."""
    from core.knowledge import bases, evals

    await bases.require_base(kb_id)
    return await evals.list_sets(kb_id)


@router.post("/api/knowledge/bases/{kb_id}/eval/sets", response_model=EvalSet, status_code=201)
@_errors
async def create_eval_set(kb_id: str, body: EvalSetCreate) -> EvalSet:
    from core.knowledge import evals

    return await evals.create_set(kb_id, body)


@router.get("/api/knowledge/bases/{kb_id}/eval/sets/{set_id}", response_model=EvalSet)
@_errors
async def get_eval_set(kb_id: str, set_id: str) -> EvalSet:
    from core.knowledge import evals

    return await evals.get_set(kb_id, set_id)


@router.put("/api/knowledge/bases/{kb_id}/eval/sets/{set_id}", response_model=EvalSet)
@_errors
async def update_eval_set(kb_id: str, set_id: str, body: EvalSetUpdate) -> EvalSet:
    """Save the cases a person accepted or edited, from the revision the edit started at (409 if stale)."""
    from core.knowledge import evals

    return await evals.update_set(kb_id, set_id, body)


@router.delete("/api/knowledge/bases/{kb_id}/eval/sets/{set_id}", status_code=204, response_model=None)
@_errors
async def delete_eval_set(kb_id: str, set_id: str) -> Response:
    """Forget the set and its runs (409 while a run is under way)."""
    from core.knowledge import evals

    await evals.delete_set(kb_id, set_id)
    return Response(status_code=204)


@router.post("/api/knowledge/bases/{kb_id}/eval/sets/{set_id}/promote", response_model=EvalSet)
@_errors
async def promote_eval_query(kb_id: str, set_id: str, body: EvalPromote) -> EvalSet:
    """Make one of the base's real searches a case: the passages ticked are where its answer is written.
    Its trace is then kept past the 30-day cleanup."""
    from core.knowledge import evals

    return await evals.promote(kb_id, set_id, body)


@router.get("/api/knowledge/bases/{kb_id}/eval/queries", response_model=list[EvalQuery])
@_errors
async def list_eval_queries(kb_id: str, limit: int = Query(50, ge=1, le=200)) -> list[EvalQuery]:
    """The base's newest real searches (any surface but the eval's own), to promote."""
    from core.knowledge import bases, evals

    await bases.require_base(kb_id)
    return await evals.recent_queries(kb_id, limit=limit)


@router.post("/api/knowledge/bases/{kb_id}/eval/drafts", response_model=EvalDrafts)
@_errors
async def draft_eval_questions(kb_id: str, body: EvalDraftRequest) -> EvalDrafts:
    """Draft questions from the base's own passages (paid: one chat call each). Saves nothing: a person adds
    the ones they keep to a set."""
    from core.config import load_settings
    from core.knowledge import bases, evals, evals_generate

    kb = await bases.require_base(kb_id)
    asked = [ev for case in (await evals.cases_of(kb_id, body.set_id) if body.set_id else [])
             for ev in case.get("evidence") or []]
    return await evals_generate.draft(kb, body, load_settings(), exclude=asked)


@router.post("/api/knowledge/bases/{kb_id}/eval/estimate", response_model=EvalEstimate)
@_errors
async def estimate_eval(kb_id: str, body: EvalEstimateRequest) -> EvalEstimate:
    """What drafting, or a run, would cost — before anything is spent. A model the rate card does not price
    is listed as unpriced."""
    from core.config import load_settings
    from core.knowledge import bases, evals_generate, evals_runs

    kb = await bases.require_base(kb_id)
    if body.drafts is not None:
        return await evals_generate.estimate_drafts(kb, body.drafts, load_settings())
    return await evals_runs.estimate(kb, body.run)


@router.post("/api/knowledge/bases/{kb_id}/eval/runs", response_model=EvalRunSummary, status_code=202)
@_errors
async def start_eval_run(kb_id: str, body: EvalRunRequest) -> EvalRunSummary:
    """Run a question set: each question searched once per snapshot, scored keyword-only, vector-only, fused
    and as served; and, when asked, an answer graded by a judge of another model family. 422 when the
    searches would cross the base's budget; 409 while the set has a run under way."""
    from core.knowledge import bases, evals_runs

    kb = await bases.require_base(kb_id)
    if kb.status != "active":
        raise HTTPException(status_code=409, detail="This knowledge base is disabled.")
    return await evals_runs.start(kb, body)


@router.get("/api/knowledge/bases/{kb_id}/eval/runs", response_model=list[EvalRunSummary])
@_errors
async def list_eval_runs(kb_id: str, set_id: str | None = None,
                         limit: int = Query(50, ge=1, le=200)) -> list[EvalRunSummary]:
    from core.knowledge import bases, evals

    await bases.require_base(kb_id)
    return await evals.list_runs(kb_id, set_id=set_id, limit=limit)


@router.get("/api/knowledge/bases/{kb_id}/eval/runs/{run_id}", response_model=EvalRun)
@_errors
async def get_eval_run(kb_id: str, run_id: str) -> EvalRun:
    """A run with each question's result: where its evidence ranked in each configuration."""
    from core.knowledge import evals_runs

    return await evals_runs.get_run(kb_id, run_id)


@router.post("/api/knowledge/bases/{kb_id}/eval/runs/{run_id}/cancel", response_model=EvalRunSummary)
@_errors
async def cancel_eval_run(kb_id: str, run_id: str) -> EvalRunSummary:
    from core.knowledge import evals_runs

    return await evals_runs.cancel(kb_id, run_id)


@router.post("/api/knowledge/bases/{kb_id}/eval/runs/{run_id}/compare", response_model=list[EvalConfigResult])
@_errors
async def compare_eval_run(kb_id: str, run_id: str, body: EvalCompareRequest) -> list[EvalConfigResult]:
    """The run's searches fused again with other weights: no search, $0."""
    from core.knowledge import evals_runs

    return await evals_runs.compare(kb_id, run_id, body)
