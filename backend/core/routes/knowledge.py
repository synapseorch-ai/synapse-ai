"""Knowledge's internal API: knowledge bases, uploads, records and documents (A1, D157),
search and a search's trace (B1b, D161), web sources and their crawls (S1, D165).

Paths are `/api` + the frozen route table (`core.knowledge.api_models.ROUTES`),
with its bodies and status codes; a test holds the two equal. Costs, cost
previews and replay are other lanes' routes and are not mounted here.

Knowledge is off until its release: while `knowledge_enabled` is false every
route here answers 404, as if it did not exist.
"""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from core.knowledge.api_models import (
    CrawlAccepted, DeleteAccepted, DocumentDetail, DocumentOut, IngestAccepted, KnowledgeBaseCreate,
    KnowledgeBaseOut, KnowledgeBaseUpdate, RecordIngestRequest, SearchRequest, SearchResponse, UploadComplete,
    UploadInit, UploadSession, UrlIngestRequest, WebCrawlState,
)
from core.knowledge.trace import Trace

#: Search's own "not found" codes (`core.knowledge.search`): a base or a pinned snapshot the
#: search names does not exist.
_NOT_FOUND_CODES = frozenset({"kb_not_found", "snapshot_not_found"})


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
    from core.knowledge import bases, dispatch, sync

    kb = await bases.create_base(body)
    await sync.ensure_folder(kb)
    await dispatch.enqueue(kb.id, "reindex", "reconcile")
    return (await _out([kb]))[0]


@router.get("/api/knowledge/bases/{kb_id}", response_model=KnowledgeBaseOut)
@_errors
async def get_knowledge_base(kb_id: str) -> KnowledgeBaseOut:
    from core.knowledge import bases

    return (await _out([await bases.require_base(kb_id)]))[0]


@router.patch("/api/knowledge/bases/{kb_id}", response_model=KnowledgeBaseOut)
@_errors
async def update_knowledge_base(kb_id: str, body: KnowledgeBaseUpdate) -> KnowledgeBaseOut:
    """Edit from the revision the edit started at (409 if it is stale). A new folder moves the documents;
    switching a base back on reconciles it. A change to how the index is built applies to documents
    indexed from now on until blue/green rebuilds arrive (B2): `reindex_started` is false."""
    from core.knowledge import bases, budgets, dispatch, sync

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
    out = (await _out([after]))[0]
    return out.model_copy(update={"reindex_started": False})


@router.delete("/api/knowledge/bases/{kb_id}", response_model=DeleteAccepted, status_code=202)
@_errors
async def delete_knowledge_base(kb_id: str) -> DeleteAccepted:
    """Forget the knowledge base: its index, documents and snapshots. Its Vault folder and files stay."""
    from core.knowledge import bases
    from core.knowledge.jobs import drop_base

    await bases.require_base(kb_id)
    await drop_base(kb_id)
    return DeleteAccepted()


# ── uploads ────────────────────────────────────────────────────────────────────

@router.post("/api/knowledge/bases/{kb_id}/uploads", response_model=UploadSession, status_code=201)
@_errors
async def start_upload(kb_id: str, body: UploadInit) -> UploadSession:
    """Open an upload of one file. PUT each part to its URL, then complete."""
    from core.knowledge import bases, ingest

    kb = await bases.require_base(kb_id)
    if kb.status != "active":
        raise HTTPException(status_code=409, detail="This knowledge base is disabled.")
    return await ingest.start_upload(kb, body,
                                     part_url=lambda upload_id, n: f"/api/knowledge/uploads/{upload_id}/parts/{n}")


@router.put("/api/knowledge/uploads/{upload_id}/parts/{part_number}", status_code=200)
@_errors
async def put_upload_part(upload_id: str, part_number: int, request: Request) -> Response:
    """One part's bytes, as the request body. Answers with the part's ETag, which complete names."""
    import json

    from core.knowledge import ingest

    etag = await ingest.put_part(upload_id, part_number, request.stream())
    return Response(content=json.dumps({"part_number": part_number, "etag": etag}), media_type="application/json",
                    headers={"ETag": f'"{etag}"'})


@router.post("/api/knowledge/uploads/{upload_id}/complete", response_model=IngestAccepted, status_code=202)
@_errors
async def complete_upload(upload_id: str, body: UploadComplete) -> IngestAccepted:
    from core.knowledge import documents, ingest

    rows = await ingest.complete_upload(upload_id, body)
    return IngestAccepted(documents=[documents.to_out(r) for r in rows])


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
    """A base's documents by id, paged with `after` (the last id seen)."""
    from core.knowledge import bases, documents

    await bases.require_base(kb_id)
    rows = await documents.list_for(kb_id, after=after, limit=limit, status=status, path_prefix=path_prefix)
    return [documents.to_out(r) for r in rows]


@router.get("/api/knowledge/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str) -> DocumentDetail:
    """A document with its ingestion report — what happened to it, stage by stage."""
    from core.knowledge import documents

    return documents.to_detail(await _document(doc_id))


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
