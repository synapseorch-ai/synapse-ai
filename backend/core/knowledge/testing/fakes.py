"""Deterministic stand-ins for the seams, each reporting `Usage` like the real ones must."""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from core.knowledge.errors import EmbeddingError, OcrError, RerankError
from core.knowledge.types import (
    Attribution, DispatchReceipt, EmbeddingSpec, EmbedKind, EmbedResult, IngestJob, OcrPage, OcrResult,
    PageImage, RerankResult, RerankSpec, Usage,
)

_WORD = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


class HashingEmbedder:
    """Feature hashing, L2-normalized: texts sharing words get similar vectors.

    `fail_on` texts raise `EmbeddingError`; `zero_on` texts come back as zero
    vectors (to prove the seam's guard catches them). Every call is recorded.
    """

    def __init__(self, dims: int = 64, *, model: str = "hash-64", cost_source: str = "local",
                 fail_on: Sequence[str] = (), zero_on: Sequence[str] = (), max_batch: int = 32,
                 usd_per_mtok: float = 0.0):
        self.dims = dims
        self.model = model
        self.cost_source = cost_source
        self.fail_on = set(fail_on)
        self.zero_on = set(zero_on)
        self.max_batch = max_batch
        self.usd_per_mtok = usd_per_mtok
        self.calls: list[dict[str, Any]] = []

    def vector(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for token in tokens(text) or [text or "∅"]:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dims
            v[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0.0:
            v[0], norm = 1.0, 1.0
        return [x / norm for x in v]

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind, spec: EmbeddingSpec,
                    settings: Mapping[str, Any], attribution: Attribution) -> EmbedResult:
        self.calls.append({"texts": list(texts), "kind": kind, "spec": spec, "attribution": attribution})
        for text in texts:
            if text in self.fail_on:
                raise EmbeddingError(f"fake embedder refused {text!r}")
        vectors = [[0.0] * self.dims if t in self.zero_on else self.vector(t) for t in texts]
        n_tokens = sum(max(1, len(tokens(t))) for t in texts)
        usage = Usage(stage="embed", cost_source=self.cost_source, model=self.model, provider="fake",  # type: ignore[arg-type]
                      input_tokens=n_tokens, units=float(n_tokens), unit="tokens",
                      est_usd=n_tokens * self.usd_per_mtok / 1_000_000)
        return EmbedResult(vectors=vectors, model=self.model, dims=self.dims, usage=usage)


class FakeReranker:
    """Scores each document by the share of the query's words it contains."""

    def __init__(self, *, model: str = "fake-rerank", max_documents: int = 100, fail: bool = False):
        self.model = model
        self.max_documents = max_documents
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def rerank(self, query: str, documents: Sequence[str], *, spec: RerankSpec,
                     settings: Mapping[str, Any], attribution: Attribution) -> RerankResult:
        self.calls.append({"query": query, "documents": list(documents), "spec": spec})
        if self.fail:
            raise RerankError("fake reranker refused")
        wanted = set(tokens(query))
        scored = []
        for i, doc in enumerate(documents[: self.max_documents]):
            have = set(tokens(doc))
            scored.append((i, len(wanted & have) / len(wanted) if wanted else 0.0))
        scored.sort(key=lambda p: (-p[1], p[0]))
        usage = Usage(stage="rerank", cost_source="local", model=self.model, provider="fake",
                      units=1.0, unit="search_units")
        return RerankResult(ranking=scored[: spec.top_n], model=self.model, usage=usage)


class RecordingDispatcher:
    """Keeps every job; `drain(runner)` runs them in order (a queue's semantics: one per dedup key)."""

    def __init__(self) -> None:
        self.jobs: list[IngestJob] = []

    async def dispatch(self, job: IngestJob) -> DispatchReceipt:
        if all(j.dedup_key() != job.dedup_key() for j in self.jobs):
            self.jobs.append(job)
        return DispatchReceipt(mode="queued", job_id=job.dedup_key())

    async def drain(self, runner: Callable[[IngestJob], Awaitable[Any]]) -> list[Any]:
        results = []
        while self.jobs:
            results.append(await runner(self.jobs.pop(0)))
        return results


class FakeOcr:
    """Answers with the text given per page; pages it has none for fail."""

    def __init__(self, pages: Mapping[int, str], *, engine: str = "fake-ocr", max_pages_per_call: int = 10):
        self.pages = dict(pages)
        self.engine = engine
        self.max_pages_per_call = max_pages_per_call

    async def ocr(self, pages: Sequence[PageImage], *, lang_hints: Sequence[str],
                  settings: Mapping[str, Any], attribution: Attribution) -> OcrResult:
        out = []
        for page in pages:
            if page.page not in self.pages:
                raise OcrError(f"fake OCR has no text for page {page.page}")
            out.append(OcrPage(page=page.page, text=self.pages[page.page], confidence=0.99))
        usage = Usage(stage="ocr", cost_source="local", model=self.engine, provider="fake",
                      units=float(len(pages)), unit="pages")
        return OcrResult(pages=out, engine=self.engine, usage=usage)
