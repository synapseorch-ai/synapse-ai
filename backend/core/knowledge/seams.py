# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's five seams — what a deployment plugs in — in the pattern of `core.agent_memory`.

Each is a module global with a setter; `set_…(None)` restores the default. OSS
runs the defaults; cloud's worker registers its own at startup
(`worker/engine_hooks.install`), in every engine process that can run
Knowledge, and asserts it did.

| Seam | Default | Cloud |
|---|---|---|
| `set_knowledge_index` | `core.knowledge.index_sql:SqlKnowledgeIndex` (B1) | `PgKnowledgeIndex` (pgvector) |
| `set_knowledge_embedder` | `core.knowledge.embedders:ProviderEmbedder` (B2) | the gateway's, batched (T1) |
| `set_reranker` | `core.knowledge.rerankers:from_settings` (B2): none unless configured | Cohere Rerank 3.5 via the gateway |
| `set_ingest_dispatcher` | `InProcessDispatcher` (below) | ARQ, queue `synapse:knowledge` |
| `set_ocr_provider` | none: pages without a text layer are reported | Textract |

**Only the search index plugs in; the records do not** (D153's store split).
Documents, chunks, snapshots, queries and costs live in the engine's own
tables everywhere — cloud mirrors them under RLS — so an external vector store
(Qdrant, Pinecone, after release) implements `KnowledgeIndex` and nothing else.

**Every seam call returns its `Usage`**, so the cost ledger never guesses.

**An embedder raises; it never answers with zero vectors.** The provider
functions this replaces returned all-zero vectors on error
(`core/llm_providers.py` `_embed_openai`, `_embed_gemini`), which store fine and
then match nothing, or everything. `get_knowledge_embedder()` therefore always
hands out a validating wrapper, so no implementation can skip the check.

A default that is not built yet (a lane's module is missing) resolves to an
honest `KnowledgeUnavailable`, never to a stand-in that loses data.
"""
from __future__ import annotations

import asyncio
import importlib
import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from core.knowledge.errors import EmbedderUnavailable, EmbeddingInvalid, KnowledgeUnavailable
from core.knowledge.types import (
    Attribution, DispatchReceipt, EmbeddingSpec, EmbedKind, EmbedResult, HybridQuery, IndexCapabilities,
    IndexEntry, IndexStats, IngestJob, LegResult, LexicalQuery, OcrResult, PageImage, RerankResult,
    RerankSpec, Scope, VectorQuery,
)
from core.log import get_logger

_log = get_logger(__name__)

DEFAULT_INDEX = "core.knowledge.index_sql:SqlKnowledgeIndex"
DEFAULT_EMBEDDER = "core.knowledge.embedders:ProviderEmbedder"
DEFAULT_RERANKER = "core.knowledge.rerankers:from_settings"
INGEST_ENTRYPOINT = "core.knowledge.jobs:run_ingest_job"

#: The smallest L2 norm an embedding may have. Below it the vector is zero in
#: all but rounding, and cosine similarity with it is meaningless.
MIN_NORM = 1e-12


# ── the protocols ──────────────────────────────────────────────────────────────

@runtime_checkable
class KnowledgeIndex(Protocol):
    """Where chunks are searched. Keyed by chunk id, scoped by (kb, snapshot), tenant implicit.

    The rules every implementation keeps are the conformance suite
    (`core.knowledge.testing.conformance`), run against every index: higher
    score is better, ranks are 1-based, ties break by chunk id ascending;
    lexical text is plain text, never query syntax; with `filter_pushdown` the
    filter runs inside the query and a top-k is never padded with excluded rows;
    `upsert` is idempotent per chunk id; mixed vector widths in one snapshot
    raise `IndexDimensionError`; the tenant (`core.tenancy.get_tenant()`) scopes
    every read and write.
    """

    capabilities: IndexCapabilities

    async def upsert(self, entries: Sequence[IndexEntry]) -> int: ...
    async def delete_chunks(self, scope: Scope, chunk_ids: Sequence[str]) -> int: ...
    async def delete_document(self, kb_id: str, doc_id: str, *, snapshot_id: str | None = None) -> int: ...
    async def delete_prefix(self, kb_id: str, path_prefix: str) -> int: ...
    async def drop_snapshot(self, scope: Scope) -> int: ...
    async def drop_kb(self, kb_id: str) -> int: ...
    async def search_lexical(self, query: LexicalQuery) -> LegResult: ...
    async def search_vector(self, query: VectorQuery) -> LegResult: ...
    async def search_hybrid(self, query: HybridQuery) -> LegResult: ...
    async def stats(self, scope: Scope) -> IndexStats: ...


@runtime_checkable
class KnowledgeEmbedder(Protocol):
    """Embeds texts as documents or as queries. Raises on failure; never zero vectors."""

    max_batch: int

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind, spec: EmbeddingSpec,
                    settings: Mapping[str, Any], attribution: Attribution) -> EmbedResult: ...


@runtime_checkable
class KnowledgeReranker(Protocol):
    """Reorders at most `max_documents` (≤ 100) candidates for a query."""

    max_documents: int

    async def rerank(self, query: str, documents: Sequence[str], *, spec: RerankSpec,
                     settings: Mapping[str, Any], attribution: Attribution) -> RerankResult: ...


@runtime_checkable
class IngestDispatcher(Protocol):
    """Hands an `IngestJob` to whatever runs it. Never raises to the caller: a Vault
    write must not fail because indexing could not be scheduled — reconcile
    repairs a missed job."""

    async def dispatch(self, job: IngestJob) -> DispatchReceipt: ...


@runtime_checkable
class OcrProvider(Protocol):
    max_pages_per_call: int

    async def ocr(self, pages: Sequence[PageImage], *, lang_hints: Sequence[str],
                  settings: Mapping[str, Any], attribution: Attribution) -> OcrResult: ...


# ── resolving a default by name ────────────────────────────────────────────────

def _load(dotted: str) -> Any | None:
    """`module:attr`, or None when that module does not exist yet (a lane not built).

    Only the named module's absence is swallowed: an import error *inside* a
    module that exists is a bug and propagates.
    """
    module_name, _, attr = dotted.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    return getattr(module, attr)


# ── the embedder guard ─────────────────────────────────────────────────────────

class _Validated:
    """Wraps any embedder; checks what it returns before anything stores it."""

    def __init__(self, inner: KnowledgeEmbedder):
        self.inner = inner

    @property
    def max_batch(self) -> int:
        return int(getattr(self.inner, "max_batch", 1) or 1)

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind, spec: EmbeddingSpec,
                    settings: Mapping[str, Any], attribution: Attribution) -> EmbedResult:
        if not texts:
            raise ValueError("embed() needs at least one text")
        if kind not in ("document", "query"):
            raise ValueError(f"kind is 'document' or 'query', not {kind!r}")
        result = await self.inner.embed(texts, kind=kind, spec=spec, settings=settings,
                                        attribution=attribution)
        check_vectors(result, expected_count=len(texts), expected_dims=spec.dims)
        return result


def check_vectors(result: EmbedResult, *, expected_count: int, expected_dims: int | None) -> None:
    """Raise `EmbeddingInvalid` unless `result` holds `expected_count` storable vectors."""
    vectors = result.vectors
    if len(vectors) != expected_count:
        raise EmbeddingInvalid(f"asked for {expected_count} embeddings, got {len(vectors)}")
    if expected_dims is not None and result.dims != expected_dims:
        raise EmbeddingInvalid(f"asked for {expected_dims} dimensions, the model answered {result.dims}")
    for i, vector in enumerate(vectors):
        if len(vector) != result.dims:
            raise EmbeddingInvalid(f"embedding {i} has {len(vector)} dimensions, not {result.dims}")
        norm = 0.0
        for value in vector:
            if not math.isfinite(value):
                raise EmbeddingInvalid(f"embedding {i} holds a non-finite value")
            norm += value * value
        if math.sqrt(norm) < MIN_NORM:
            raise EmbeddingInvalid(f"embedding {i} is a zero vector")


# ── the in-process dispatcher (OSS's default) ──────────────────────────────────

class InProcessDispatcher:
    """Runs each job as a background task in this process, a few at a time.

    The task is created in the caller's context, so the tenant (a ContextVar)
    travels with it. `knowledge_ingest_concurrency` bounds how many run at once;
    the rest wait on the semaphore, in order.
    """

    def __init__(self) -> None:
        self._semaphores: dict[int, asyncio.Semaphore] = {}

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._semaphores.get(id(loop))
        if sem is None:
            from core.knowledge.config import knowledge_settings
            sem = asyncio.Semaphore(max(1, knowledge_settings().ingest_concurrency))
            self._semaphores = {id(loop): sem}
        return sem

    async def dispatch(self, job: IngestJob) -> DispatchReceipt:
        from core.background import spawn_background
        try:
            spawn_background(self._run(job), name=f"knowledge:{job.op}:{job.kb_id}")
        except Exception as exc:  # noqa: BLE001 — never fail the caller; reconcile repairs
            _log.warning(f"knowledge job for {job.kb_id} not scheduled: {exc}")
        return DispatchReceipt(mode="background", job_id=None)

    async def _run(self, job: IngestJob) -> None:
        async with self._semaphore():
            runner = _load(INGEST_ENTRYPOINT)
            if runner is None:
                _log.warning("knowledge ingestion is not installed; job dropped")
                return
            await runner(job)


# ── the seams ──────────────────────────────────────────────────────────────────

_index: KnowledgeIndex | None = None
_default_index: KnowledgeIndex | None = None
_embedder: KnowledgeEmbedder | None = None
_default_embedder: KnowledgeEmbedder | None = None
_reranker: KnowledgeReranker | None = None
_dispatcher: IngestDispatcher | None = None
_default_dispatcher: InProcessDispatcher | None = None
_ocr: OcrProvider | None = None


def set_knowledge_index(index: KnowledgeIndex | None) -> None:
    """Install a search index. None restores the SQL default."""
    global _index, _default_index
    _index = index
    _default_index = None


def get_knowledge_index() -> KnowledgeIndex:
    global _default_index
    if _index is not None:
        return _index
    if _default_index is None:
        factory = _load(DEFAULT_INDEX)
        if factory is None:
            raise KnowledgeUnavailable("No knowledge index is installed.")
        _default_index = factory()
    return _default_index


def set_knowledge_embedder(embedder: KnowledgeEmbedder | None) -> None:
    """Install an embedder. None restores the provider default."""
    global _embedder, _default_embedder
    _embedder = embedder
    _default_embedder = None


def get_knowledge_embedder() -> _Validated:
    """The embedder, always behind the vector check."""
    global _default_embedder
    if _embedder is not None:
        return _Validated(_embedder)
    if _default_embedder is None:
        factory = _load(DEFAULT_EMBEDDER)
        if factory is None:
            raise EmbedderUnavailable("No embedder is configured.")
        _default_embedder = factory()
    return _Validated(_default_embedder)


def set_reranker(reranker: KnowledgeReranker | None) -> None:
    """Install a reranker. None restores the default: whatever the settings configure, or none."""
    global _reranker
    _reranker = reranker


def get_reranker(settings: Mapping[str, Any] | None = None) -> KnowledgeReranker | None:
    """The reranker, or None: the search then keeps the fused order and its trace says `rerank_unconfigured`."""
    if _reranker is not None:
        return _reranker
    factory = _load(DEFAULT_RERANKER)
    if factory is None:
        return None
    return factory(settings or {})


def set_ingest_dispatcher(dispatcher: IngestDispatcher | None) -> None:
    """Install a dispatcher. None restores the in-process one."""
    global _dispatcher
    _dispatcher = dispatcher


def get_ingest_dispatcher() -> IngestDispatcher:
    global _default_dispatcher
    if _dispatcher is not None:
        return _dispatcher
    if _default_dispatcher is None:
        _default_dispatcher = InProcessDispatcher()
    return _default_dispatcher


def set_ocr_provider(provider: OcrProvider | None) -> None:
    """Install an OCR provider. None: pages with no text layer are reported, not read."""
    global _ocr
    _ocr = provider


def get_ocr_provider() -> OcrProvider | None:
    return _ocr


def reset_all() -> None:
    """Every seam back to its default. Tests call this around each test."""
    set_knowledge_index(None)
    set_knowledge_embedder(None)
    set_reranker(None)
    set_ingest_dispatcher(None)
    set_ocr_provider(None)
    global _default_dispatcher
    _default_dispatcher = None
