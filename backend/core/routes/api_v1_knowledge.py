"""Knowledge on the public API: `/api/v1` + the frozen route table's v1 rows (P1a, D176).

Each route is the app's own handler (`core/routes/knowledge.py`) behind an API key instead of the app's
gate, so the two surfaces answer alike — the same bodies, status codes, query parameters and errors — and
a test holds them to `api_models.ROUTES`. Knowledge's switch comes first: while it is off, every route here
answers 404, as if it did not exist, before a key is looked at.

Three are v1's own:

* **search** is traced as the API's (`origin.surface == "api"`), naming the key;
* **an upload's start** hands out v1's part URLs, each with the session's own token in `headers`;
* **a part's PUT** takes that token and no key (CCR-8). A client PUTs `parts[i].url` with exactly
  `parts[i].headers`, as it does to cloud's presigned URLs, which carry no key either.

OSS has no Knowledge webhooks: a document's status is read (`GET /api/v1/knowledge/documents/{doc_id}`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute

from core.api_key_middleware import require_api_key
from core.knowledge.api_models import ROUTES, SearchRequest, SearchResponse, UploadInit, UploadSession
from core.routes import knowledge as app_routes

PREFIX = "/api/v1"
#: The header an OSS v1 upload part carries its session's token in (CCR-8).
UPLOAD_TOKEN_HEADER = "X-Synapse-Upload-Token"

_SEARCH = ("POST", "/knowledge/search")
_START = ("POST", "/knowledge/bases/{kb_id}/uploads")
_PART = ("PUT", "/knowledge/uploads/{upload_id}/parts/{part_number}")
_OWN = frozenset({_SEARCH, _START, _PART})

#: (summary, description) for the API's documentation, by route.
_DOCS: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/knowledge/bases"): (
        "List knowledge bases",
        "Every knowledge base with its state: documents by status, the serving and building index, this "
        "month's spend, and a rebuild's progress (`state.build`)."),
    ("POST", "/knowledge/bases"): (
        "Create a knowledge base",
        "A knowledge base is one Vault folder made searchable. Its settings choose the chunking, the "
        "embedding model, rerank, fusion and retrieval; its folder may not overlap another base's."),
    ("GET", "/knowledge/bases/{kb_id}"): ("Get a knowledge base", "The base's settings and state."),
    ("PATCH", "/knowledge/bases/{kb_id}"): (
        "Update a knowledge base",
        "A patch from `revision`, the one the edit started from; a stale revision is a 409. A change the "
        "index depends on rebuilds it beside the serving one (`reindex_started`), which answers searches "
        "until the new one is ready."),
    ("DELETE", "/knowledge/bases/{kb_id}"): (
        "Delete a knowledge base",
        "Forgets the base: its index, documents and snapshots. Its Vault folder and files stay."),
    ("POST", "/knowledge/bases/{kb_id}/cost-preview"): (
        "Preview a base's costs",
        "What indexing the base costs with its settings, or with `overrides`: pages, tokens and estimated "
        "USD by stage, and the budget left this month. Starts nothing."),
    _START: (
        "Start an upload",
        "Opens an upload of one file into the base. PUT each `parts[i].url` with exactly `part_size` bytes "
        "(the last part may be shorter) and exactly `parts[i].headers` — no Authorization header — then "
        "complete it with each part's ETag. The session expires at `expires_at`."),
    _PART: (
        "Upload a part",
        f"One part's bytes, as the request body, authorised by the session's token in "
        f"`{UPLOAD_TOKEN_HEADER}` (from `parts[i].headers`), not by an API key. Answers with the part's "
        "ETag, in the `ETag` header and in the body. A wrong or missing token is a 404."),
    ("POST", "/knowledge/uploads/{upload_id}/complete"): (
        "Complete an upload",
        "Assembles the parts, each checked against its ETag, into the Vault file and queues its document "
        "(202). Read its status with `GET /api/v1/knowledge/documents/{doc_id}`."),
    ("POST", "/knowledge/bases/{kb_id}/urls"): (
        "Add web pages",
        "A page, a sitemap's pages, or a site crawled from a URL, fetched into the base (`_web/<host>/…`) "
        "with robots.txt respected, and fetched again on the base's schedule. 202: the crawl runs on."),
    ("POST", "/knowledge/bases/{kb_id}/records"): (
        "Ingest records",
        "Text or JSON records, each with metadata, stored as `<folder>/_records/<id>.json`; sending an `id` "
        "again replaces its record. 202, with each document queued."),
    ("DELETE", "/knowledge/bases/{kb_id}/records/{record_id}"): (
        "Delete a record", "Deletes the record's file, and with it its document."),
    ("GET", "/knowledge/bases/{kb_id}/documents"): (
        "List a base's documents",
        "Paged by `after` (the last id seen) and `limit`; narrowed by `status` or `path_prefix`."),
    ("GET", "/knowledge/documents/{doc_id}"): (
        "Get a document",
        "The document with its ingestion report: what happened to it, stage by stage, and why it failed "
        "if it did."),
    ("POST", "/knowledge/documents/{doc_id}/retry"): (
        "Retry a document",
        "Queues a resting document again (indexed, partial, failed, skipped, paused or blocked). 409 while "
        "it is being worked on."),
    ("DELETE", "/knowledge/documents/{doc_id}"): (
        "Delete a document", "Deletes the document's Vault file, and with it the document."),
    _SEARCH: (
        "Search knowledge bases",
        "Hybrid search (keywords and meaning, fused, then reranked when the base reranks) over up to ten "
        "bases, with metadata `filters` applied inside the search. Each hit has a label (`[K1]`, …), its "
        "text and its citation: the file, its pages and characters, its heading path, and when it was "
        "indexed. `include_trace` adds the search's trace."),
    ("GET", "/knowledge/queries/{query_id}"): (
        "Read a search's trace",
        "The request, every leg's candidates with their ranks and scores, the cuts and the hits. Kept 30 "
        "days."),
    ("GET", "/knowledge/bases/{kb_id}/costs"): (
        "A base's costs",
        "What the base's ingestion and searches cost, grouped by `group_by` (`day`, `stage`, `document` or "
        "`model`) from `since` to `until` (ISO dates or times; this month by default)."),
}

router = APIRouter(tags=["Knowledge"])
_GATE = Depends(app_routes._require_enabled)


def _app_handlers() -> dict[tuple[str, str], object]:
    """The app's handler for each of its routes, by (method, path without "/api")."""
    found: dict[tuple[str, str], object] = {}
    for route in app_routes.router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                found[(method, route.path[len("/api"):])] = route.endpoint
    return found


def _mount() -> None:
    handlers = _app_handlers()
    for spec in ROUTES:
        key = (spec.method, spec.path)
        if "v1" not in spec.surfaces or key in _OWN:  # v1's own are declared below; a duplicate would win silently
            continue
        summary, description = _DOCS[key]
        router.add_api_route(PREFIX + spec.path, handlers[key], methods=[spec.method], status_code=spec.status,
                             response_model=spec.response, dependencies=[_GATE, Depends(require_api_key)],
                             summary=summary, description=description,
                             name=f"v1_{handlers[key].__name__}")  # type: ignore[attr-defined]


_mount()


@router.post(PREFIX + _SEARCH[1], response_model=SearchResponse, dependencies=[_GATE],
             summary=_DOCS[_SEARCH][0], description=_DOCS[_SEARCH][1])
@app_routes._errors
async def v1_search_knowledge(body: SearchRequest, key: dict = Depends(require_api_key)) -> SearchResponse:
    from core.knowledge import search
    from core.knowledge.trace import Origin

    return await search.search(body, origin=Origin(surface="api", api_key_id=key.get("id")))


@router.post(PREFIX + _START[1], response_model=UploadSession, status_code=201,
             dependencies=[_GATE, Depends(require_api_key)], summary=_DOCS[_START][0], description=_DOCS[_START][1])
@app_routes._errors
async def v1_start_upload(kb_id: str, body: UploadInit) -> UploadSession:
    return await app_routes.open_upload(
        kb_id, body, part_url=lambda upload_id, n: f"{PREFIX}/knowledge/uploads/{upload_id}/parts/{n}",
        token_header=UPLOAD_TOKEN_HEADER)


@router.put(PREFIX + _PART[1], status_code=200, dependencies=[_GATE], summary=_DOCS[_PART][0],
            description=_DOCS[_PART][1])
@app_routes._errors
async def v1_put_upload_part(upload_id: str, part_number: int, request: Request) -> Response:
    # "" when the header is missing: a v1 part is always checked, never let through as the app's own.
    return await app_routes.store_part(upload_id, part_number, request,
                                       token=request.headers.get(UPLOAD_TOKEN_HEADER) or "")
