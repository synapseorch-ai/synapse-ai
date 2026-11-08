# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's settings, read in one place, and the one gate: `knowledge_enabled()`.

The keys live in `core.config._DEFAULTS` and the `Settings` model like every
other setting. **Knowledge is off until its release** (Naveen, 2026-09-30,
D153): the feature branch is pinned into cloud before release, so a switch —
not the branch — keeps it dark. Cloud overlays its platform switch onto
`knowledge_enabled` through its settings provider, so there is one gate for
routes, tools, steps and the Vault hook alike.

Per-file and per-crawl caps are settings, one for every plan: the plan's
`vault_mb` already bounds how much a workspace can store (D153 #8, #11, #14).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The keys and their shipped defaults. `core.config._DEFAULTS` carries the same
#: values; a test holds the two equal.
DEFAULTS: dict[str, Any] = {
    "knowledge_enabled": False,
    "knowledge_max_file_mb": 100,
    "knowledge_max_file_pages": 2000,
    "knowledge_crawl_max_depth": 3,
    "knowledge_crawl_max_pages": 500,
    "knowledge_crawl_rps_per_host": 1.0,
    "knowledge_crawl_max_hours": 0.0,
    "knowledge_recrawl_days": 7,
    "knowledge_trace_retention_days": 30,
    "knowledge_api_search_per_min": 120,
    "knowledge_embedding_model": "",
    "knowledge_rerank_model": "",
    "knowledge_ingest_concurrency": 2,
    "knowledge_snapshot_grace_days": 7,
    # CCR-3 (B2a, D163): a bring-your-own TEI/Infinity reranker's address and key.
    "knowledge_rerank_base_url": "",
    "knowledge_rerank_key": "",
    # CCR-10 (CR1, D180): the model that writes Contextual Retrieval's contexts when a base names
    # none. Blank means the workspace's chat model; cloud's overlay sets its platform default.
    "knowledge_contextual_model": "",
}


@dataclass(frozen=True)
class KnowledgeSettings:
    enabled: bool
    max_file_mb: int
    max_file_pages: int
    crawl_max_depth: int
    crawl_max_pages: int
    crawl_rps_per_host: float
    #: Time one crawl may spend fetching, across its slices, before it stops `capped`; 0: no limit (CCR-14).
    crawl_max_hours: float
    recrawl_days: int
    trace_retention_days: int
    api_search_per_min: int
    embedding_model: str
    rerank_model: str
    ingest_concurrency: int
    snapshot_grace_days: int
    rerank_base_url: str = ""  # CCR-3
    rerank_key: str = ""  # CCR-3
    contextual_model: str = ""  # CCR-10


#: A workspace's "no rerank" (QA1e's F1020). Cloud reads an empty `knowledge_rerank_model` as the platform's reranker
#: (BF9), so "stop reranking" needs a value of its own; the engine reads it as no default reranker, as OSS's "".
#: Never a model id: those always name their provider (`cohere.…`).
RERANK_OFF = "none"


def _rerank_model(settings: Mapping[str, Any]) -> str:
    model = str(_get(settings, "knowledge_rerank_model") or "").strip()
    return "" if model == RERANK_OFF else model


def _get(settings: Mapping[str, Any], key: str) -> Any:
    value = settings.get(key)
    return DEFAULTS[key] if value is None else value


def knowledge_settings(settings: Mapping[str, Any] | None = None) -> KnowledgeSettings:
    """The typed view of the current (or given) settings.

    `knowledge_embedding_model` falls back to the workspace's `embedding_model`,
    so a workspace that already chose one for code search is not asked twice.
    """
    if settings is None:
        from core.config import load_settings
        settings = load_settings()
    embedding = str(_get(settings, "knowledge_embedding_model") or settings.get("embedding_model") or "")
    return KnowledgeSettings(
        enabled=bool(_get(settings, "knowledge_enabled")),
        max_file_mb=int(_get(settings, "knowledge_max_file_mb")),
        max_file_pages=int(_get(settings, "knowledge_max_file_pages")),
        crawl_max_depth=int(_get(settings, "knowledge_crawl_max_depth")),
        crawl_max_pages=int(_get(settings, "knowledge_crawl_max_pages")),
        crawl_rps_per_host=float(_get(settings, "knowledge_crawl_rps_per_host")),
        crawl_max_hours=float(_get(settings, "knowledge_crawl_max_hours")),
        recrawl_days=int(_get(settings, "knowledge_recrawl_days")),
        trace_retention_days=int(_get(settings, "knowledge_trace_retention_days")),
        api_search_per_min=int(_get(settings, "knowledge_api_search_per_min")),
        embedding_model=embedding,
        rerank_model=_rerank_model(settings),
        ingest_concurrency=int(_get(settings, "knowledge_ingest_concurrency")),
        snapshot_grace_days=int(_get(settings, "knowledge_snapshot_grace_days")),
        rerank_base_url=str(_get(settings, "knowledge_rerank_base_url") or ""),
        rerank_key=str(_get(settings, "knowledge_rerank_key") or ""),
        contextual_model=str(_get(settings, "knowledge_contextual_model") or ""),
    )


def knowledge_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    """The gate. Routes answer 404, tools are absent, steps refuse and the Vault hook
    does nothing while this is false."""
    return knowledge_settings(settings).enabled
