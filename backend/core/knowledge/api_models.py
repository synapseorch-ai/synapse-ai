# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's API: every request and response body, and the one route table.

The same models serve three surfaces, so a customer's code works on both
products:

* OSS's own app routes, `/api` + path (session auth; A1 builds them);
* OSS's public API, `/api/v1` + path (`sk-syn-…` keys; P1);
* cloud's public API on `api.`, `/api/v1` + path (keys or OAuth tokens, D124,
  D127; P1). Cloud's app talks to its control plane with these same bodies (T1).

`ROUTES` is the route table. A1 and P1 each assert the routes they mount match
it, and `openapi_knowledge.json` snapshots the models. v1 never breaks: fields
are only ever added, with defaults.

Conventions, as the rest of v1: request bodies refuse unknown fields (422
naming them); lists are bare arrays paged by `?after=<last id>&limit=`; errors
are `{"detail": …}`. Ingest answers 202 with each document `queued` — its
status is then read, or pushed by webhook. Search is synchronous.

A bad filter is a 422 whose error `type` is `filter.<code>` (a
`core.knowledge.filters.FILTER_ERROR_CODES` code) and whose message names the
JSON pointer inside the filter.

Pure (pydantic only): cloud's API process imports this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from core.knowledge.errors import FilterError
from core.knowledge.filters import check_metadata, parse_filter
from core.knowledge.ids import is_record_id
from core.knowledge.kb_settings import (
    Chunking, Embedding, Enrichment, Fusion, KnowledgeBase, Rerank, Retrieval, Web,
)
from core.knowledge.parsed import SourceKind
from core.knowledge.status import DocStatus, IngestionReport
from core.knowledge.trace import SNIPPET_MAX, ReplayReport, ReplayRequest, Trace

MAX_RECORD_BYTES = 1_000_000
MAX_BATCH = 100
MAX_KBS_PER_SEARCH = 10

#: The two orchestration step types Knowledge adds (R1): priced like a logic step (D153 #8).
STEP_TYPES: tuple[str, ...] = ("retrieve", "ingest")


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _metadata(value: dict | None) -> dict:
    try:
        return check_metadata(value or {})
    except FilterError as exc:
        raise PydanticCustomError(f"filter.{exc.filter_code}", "{detail} (at {path})",
                                  {"detail": exc.detail, "path": exc.path}) from None


def _filters(value: dict | None) -> dict | None:
    try:
        parse_filter(value)
    except FilterError as exc:
        raise PydanticCustomError(f"filter.{exc.filter_code}", "{detail} (at {path})",
                                  {"detail": exc.detail, "path": exc.path or "/"}) from None
    return value


# ── knowledge bases ────────────────────────────────────────────────────────────

class KnowledgeBaseCreate(_Request):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    folder: str
    language: str = "simple"
    visitor_access: bool = False
    budget_monthly_usd: float | None = Field(default=None, ge=0)
    chunking: Chunking = Field(default_factory=Chunking)
    embedding: Embedding = Field(default_factory=Embedding)
    rerank: Rerank = Field(default_factory=Rerank)
    fusion: Fusion = Field(default_factory=Fusion)
    retrieval: Retrieval = Field(default_factory=Retrieval)
    enrichment: Enrichment = Field(default_factory=Enrichment)
    web: Web = Field(default_factory=Web)


class KnowledgeBaseUpdate(_Request):
    """A patch. `revision` is the one the edit started from; a stale one is a 409."""

    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    folder: str | None = None
    status: Literal["active", "disabled"] | None = None
    language: str | None = None
    visitor_access: bool | None = None
    budget_monthly_usd: float | None = Field(default=None, ge=0)
    clear_budget: bool = False
    chunking: Chunking | None = None
    embedding: Embedding | None = None
    rerank: Rerank | None = None
    fusion: Fusion | None = None
    retrieval: Retrieval | None = None
    enrichment: Enrichment | None = None
    web: Web | None = None


class KnowledgeBaseState(_Response):
    counts_by_status: dict[str, int] = Field(default_factory=dict)
    active_snapshot: str | None = None
    building_snapshot: str | None = None
    month_spend_usd: float = 0.0
    paused_reason: str | None = None


class KnowledgeBaseOut(KnowledgeBase):
    model_config = ConfigDict(extra="ignore")

    state: KnowledgeBaseState = Field(default_factory=KnowledgeBaseState)
    reindex_started: bool = False


class StageEstimate(_Response):
    stage: str
    model: str
    units: float
    unit: str
    est_usd: float
    cost_source: str


class CostPreviewRequest(_Request):
    """Settings to price instead of the stored ones (a draft the settings screen has not saved)."""

    overrides: dict | None = None


class CostPreview(_Response):
    docs: int
    pages: int
    stages: list[StageEstimate] = Field(default_factory=list)
    total_est_usd: float
    budget_remaining_usd: float | None = None


# ── documents ──────────────────────────────────────────────────────────────────

class DocumentOut(_Response):
    id: str
    kb_id: str
    path: str
    source: SourceKind
    source_url: str | None = None
    status: DocStatus
    status_reason: str | None = None
    error: str | None = None
    version: int = 0
    sha256: str | None = None
    size_bytes: int | None = None
    mime: str | None = None
    title: str | None = None
    doc_date: str | None = None
    metadata: dict = Field(default_factory=dict)
    pages: int | None = None
    chunks: int | None = None
    tokens: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    indexed_at: str | None = None


class DocumentDetail(DocumentOut):
    report: IngestionReport | None = None


class IngestAccepted(_Response):
    documents: list[DocumentOut]


class DeleteAccepted(_Response):
    status: Literal["deleting"] = "deleting"


# ── uploads (presigned multipart in cloud; the same shape, served locally, in OSS) ──

class UploadInit(_Request):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    mime: str = Field(max_length=255)
    path: str | None = Field(default=None, max_length=1024)  # inside the KB's folder; default: its root
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def check_metadata_keys(cls, value: dict) -> dict:
        return _metadata(value)


class UploadPart(_Response):
    part_number: int
    url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)


class UploadSession(_Response):
    upload_id: str
    part_size: int
    parts: list[UploadPart]
    expires_at: str


class CompletedPart(_Request):
    part_number: int = Field(ge=1)
    etag: str


class UploadComplete(_Request):
    parts: list[CompletedPart] = Field(min_length=1, max_length=10_000)


# ── web pages ──────────────────────────────────────────────────────────────────

class UrlIngestRequest(_Request):
    urls: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    kind: Literal["page", "sitemap", "site"] = "page"
    max_depth: int | None = Field(default=None, ge=0, le=5)
    max_pages: int | None = Field(default=None, ge=1)
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def check_metadata_keys(cls, value: dict) -> dict:
        return _metadata(value)

    @field_validator("urls")
    @classmethod
    def _http(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if not url.lower().startswith(("http://", "https://")) or len(url) > 2048:
                raise ValueError("each URL is http(s) and at most 2048 characters")
        return urls


class CrawlAccepted(_Response):
    crawl_id: str
    accepted: int
    status: Literal["queued"] = "queued"


class CrawlCounts(_Response):
    """What one crawl of a web source did, page by page (CCR-4, S1)."""

    fetched: int = 0       # requests answered
    unchanged: int = 0     # a 304, or the same content as before
    written: int = 0       # new or changed pages written to the Vault
    deleted: int = 0       # pages gone (404/410), marked noindex, or no longer reached
    blocked: int = 0       # refused by robots.txt or the egress guard
    failed: int = 0        # network errors and server errors
    skipped: int = 0       # not a page or document a knowledge base reads, or over a size cap
    not_followed: int = 0  # links out of scope, past the depth, or past the page cap


class WebCrawlState(_Response):
    """A web source and its latest crawl (CCR-4, S1). `capped` means the crawl stopped at its page cap,
    so it deleted nothing; `interrupted` means the server stopped mid-crawl, and the next pass runs it
    again."""

    source_id: str
    url: str
    kind: Literal["page", "sitemap", "site"]
    status: Literal["idle", "queued", "running", "done", "capped", "failed", "cancelled", "interrupted"] = "idle"
    crawl_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pages: int = 0  # the pages this source holds now
    counts: CrawlCounts = Field(default_factory=CrawlCounts)
    next_due_at: str | None = None
    last_error: str | None = Field(default=None, max_length=2000)


# ── records ────────────────────────────────────────────────────────────────────

class RecordIn(_Request):
    """One record: text, or a JSON object, with metadata. Sending the same `id`
    again replaces it. Stored as `<folder>/_records/<id>.json`."""

    id: str | None = None
    text: str | None = None
    json_: dict | None = Field(default=None, alias="json")
    title: str | None = Field(default=None, max_length=1000)
    doc_date: str | None = None
    metadata: dict = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("metadata")
    @classmethod
    def check_metadata_keys(cls, value: dict) -> dict:
        return _metadata(value)

    @model_validator(mode="after")
    def _one_body(self) -> RecordIn:
        if (self.text is None) == (self.json_ is None):
            raise ValueError("a record has exactly one of text or json")
        if self.id is not None and not is_record_id(self.id):
            raise ValueError("a record id is letters, digits, '.', '_' or '-', at most 128, "
                             "starting with a letter or digit")
        import json as _json
        size = len((self.text or _json.dumps(self.json_)).encode("utf-8"))
        if size > MAX_RECORD_BYTES:
            raise ValueError(f"a record is at most {MAX_RECORD_BYTES} bytes")
        return self


class RecordIngestRequest(_Request):
    records: list[RecordIn] = Field(min_length=1, max_length=MAX_BATCH)


# ── search ─────────────────────────────────────────────────────────────────────

class QuoteCheck(_Response):
    quote: str
    matched: bool
    method: Literal["exact", "normalized"]


class Citation(_Response):
    """A `[K#]` label resolved to where the words are, and how fresh they are."""

    label: str
    kb_id: str
    doc_id: str
    chunk_id: str
    path: str
    source: SourceKind
    source_url: str | None = None
    title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    char_start: int
    char_end: int
    heading_path: list[str] = Field(default_factory=list)
    snippet: str = Field(default="", max_length=SNIPPET_MAX)
    sha256: str | None = None
    doc_version: int | None = None
    indexed_at: str | None = None
    snapshot_id: str
    quote_check: QuoteCheck | None = None


class Hit(_Response):
    label: str
    chunk_id: str
    doc_id: str
    kb_id: str
    content: str
    context: str = ""
    score: float
    scores: dict[str, float] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    citation: Citation


class CostSummary(_Response):
    total_est_usd: float = 0.0
    total_charged_microusd: int | None = None
    by_stage: dict[str, float] = Field(default_factory=dict)


class SearchRequest(_Request):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[str] = Field(min_length=1, max_length=MAX_KBS_PER_SEARCH)
    filters: dict | None = None
    path_prefix: str | None = Field(default=None, max_length=1024)
    top_k: int | None = Field(default=None, ge=1, le=50)
    rerank: bool | None = None
    score_threshold: float | None = None
    neighbors: int = Field(default=0, ge=0, le=3)
    mode: Literal["hybrid", "lexical", "vector"] = "hybrid"
    snapshot_ids: dict[str, str] | None = None  # kb_id → ksnap_…, to pin a search to a version
    include_trace: bool = False

    @field_validator("filters")
    @classmethod
    def check_filters(cls, value: dict | None) -> dict | None:
        return _filters(value)


class SearchResponse(_Response):
    query_id: str
    hits: list[Hit]
    degraded: list[str] = Field(default_factory=list)
    cost: CostSummary = Field(default_factory=CostSummary)
    trace: Trace | None = None


class CostGroup(_Response):
    key: str  # a day, a stage, a document or a model, per `group_by`
    est_usd: float
    charged_microusd: int | None = None
    input_tokens: int = 0
    units: float = 0.0


# ── orchestration steps (R1) ───────────────────────────────────────────────────

class RetrieveStepConfig(_Request):
    kb_ids: list[str] = Field(min_length=1, max_length=MAX_KBS_PER_SEARCH)
    query: str = Field(min_length=1, max_length=4000)  # a template: {{state.question}}
    filters: dict | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    rerank: bool | None = None
    snapshot: str = "active"  # "active" or a ksnap_… id: a deterministic step is pinned

    @field_validator("filters")
    @classmethod
    def check_filters(cls, value: dict | None) -> dict | None:
        return _filters(value)


class RetrieveStepOutput(_Response):
    context: str
    chunks: list[Hit]
    citations: list[Citation]
    trace_id: str
    snapshot_ids: dict[str, str]


class IngestStepConfig(_Request):
    kb_id: str
    records_from: str  # a state key holding a list of objects
    id_field: str | None = None
    text_field: str | None = None
    metadata_fields: list[str] = Field(default_factory=list, max_length=50)


class IngestStepOutput(_Response):
    kb_id: str
    documents: list[DocumentOut]


# ── the route table ────────────────────────────────────────────────────────────

Surface = Literal["internal", "v1"]


@dataclass(frozen=True)
class RouteSpec:
    """One route. `path` has no prefix: "/api" (internal) or "/api/v1" (public) goes in front.

    `bucket` is the rate bucket beyond the per-key and per-org ones (D153 #17):
    "search" is Knowledge's own per-key bucket, "starts" the existing one for
    work-starting calls. `scope` is what a key or token needs. Cloud derives
    scope from the method (D127: GET is read, anything else run), so the two
    POSTs marked "read" here — search and cost preview — are the exceptions P1
    names in cloud's `scope_for`: they start no work and change nothing.
    """

    method: str
    path: str
    request: type[BaseModel] | None
    response: type | None
    status: int
    surfaces: frozenset[str]
    bucket: str | None
    scope: Literal["read", "run"]


_BOTH = frozenset({"internal", "v1"})
_INTERNAL = frozenset({"internal"})

ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec("GET", "/knowledge/bases", None, list[KnowledgeBaseOut], 200, _BOTH, None, "read"),
    RouteSpec("POST", "/knowledge/bases", KnowledgeBaseCreate, KnowledgeBaseOut, 201, _BOTH, None, "run"),
    RouteSpec("GET", "/knowledge/bases/{kb_id}", None, KnowledgeBaseOut, 200, _BOTH, None, "read"),
    RouteSpec("PATCH", "/knowledge/bases/{kb_id}", KnowledgeBaseUpdate, KnowledgeBaseOut, 200, _BOTH, None, "run"),
    RouteSpec("DELETE", "/knowledge/bases/{kb_id}", None, DeleteAccepted, 202, _BOTH, None, "run"),
    RouteSpec("POST", "/knowledge/bases/{kb_id}/cost-preview", CostPreviewRequest, CostPreview, 200, _BOTH,
              None, "read"),
    RouteSpec("POST", "/knowledge/bases/{kb_id}/uploads", UploadInit, UploadSession, 201, _BOTH, "starts", "run"),
    RouteSpec("PUT", "/knowledge/uploads/{upload_id}/parts/{part_number}", None, None, 200, _INTERNAL, None,
              "run"),  # OSS only: the part URL an OSS UploadSession hands out
    RouteSpec("POST", "/knowledge/uploads/{upload_id}/complete", UploadComplete, IngestAccepted, 202, _BOTH,
              "starts", "run"),
    RouteSpec("POST", "/knowledge/bases/{kb_id}/urls", UrlIngestRequest, CrawlAccepted, 202, _BOTH, "starts",
              "run"),
    RouteSpec("GET", "/knowledge/bases/{kb_id}/crawls", None, list[WebCrawlState], 200, _INTERNAL, None,
              "read"),  # CCR-4 (S1): the screen's view of each web source; P1 decides v1
    RouteSpec("POST", "/knowledge/bases/{kb_id}/records", RecordIngestRequest, IngestAccepted, 202, _BOTH,
              "starts", "run"),
    RouteSpec("DELETE", "/knowledge/bases/{kb_id}/records/{record_id}", None, DeleteAccepted, 202, _BOTH,
              None, "run"),
    RouteSpec("GET", "/knowledge/bases/{kb_id}/documents", None, list[DocumentOut], 200, _BOTH, None, "read"),
    RouteSpec("GET", "/knowledge/documents/{doc_id}", None, DocumentDetail, 200, _BOTH, None, "read"),
    RouteSpec("POST", "/knowledge/documents/{doc_id}/retry", None, DocumentOut, 202, _BOTH, "starts", "run"),
    RouteSpec("DELETE", "/knowledge/documents/{doc_id}", None, DeleteAccepted, 202, _BOTH, None, "run"),
    RouteSpec("POST", "/knowledge/search", SearchRequest, SearchResponse, 200, _BOTH, "search", "read"),
    RouteSpec("GET", "/knowledge/queries/{query_id}", None, Trace, 200, _BOTH, None, "read"),
    RouteSpec("POST", "/knowledge/queries/{query_id}/replay", ReplayRequest, ReplayReport, 200, _INTERNAL,
              "search", "run"),
    RouteSpec("GET", "/knowledge/bases/{kb_id}/costs", None, list[CostGroup], 200, _BOTH, None, "read"),
)
