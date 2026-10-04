"""Rerankers: the second look a search takes at its best candidates (B2a, D163).

`seams.DEFAULT_RERANKER` names `from_settings`. It answers None (no rerank: the search keeps its fused order and
its trace says `rerank_unconfigured`) unless a rerank model is configured, the knowledge base's own
(`kb.rerank.model`) or the deployment's (`knowledge_rerank_model`). **OSS reranks only when someone set a model;**
cloud's default is our gateway's Cohere Rerank 3.5.

A rerank model id names who serves it (Naveen, D163 #1). Each has its own key, saved like any other secret:

| Id | Served by | Settings | Cost |
|---|---|---|---|
| `synapse.<id>` | our gateway's `/v1/rerank` (C1, D160) | `model_gateway_base_url`, `_key` | the wallet's charge |
| `cohere.<model>` | Cohere, `/v2/rerank` | `cohere_key` | $2 per 1,000 searches |
| `voyage.<model>` | Voyage AI, `/v1/rerank` | `voyage_key` | per token |
| `jina.<model>` | Jina AI, `/v1/rerank` | `jina_key` | per token (unpriced) |
| `tei.<name>` | the customer's TEI or Infinity | `knowledge_rerank_base_url`, `_key` | local: $0 |

Every one answers a `RerankResult`: (document index, score) best first, ties by index, at most `top_n`. A failure
raises — `RerankError`, or `WalletExhausted` for our gateway's 402 — and the search degrades to its fused order
with `rerank_failed`. The table is separate from the provider table (`core.providers.registry`): these vendors
serve no chat, and routing a chat id by them would change where those ids go.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.knowledge.errors import RerankError, WalletExhausted
from core.knowledge.types import Attribution, RerankResult, RerankSpec, Usage
from core.log import get_logger

_log = get_logger(__name__)

#: The most candidates one call reranks (Cohere's and our gateway's cap).
MAX_DOCUMENTS = 100
#: Characters of one candidate sent: Cohere 3.5 reads 4,096 tokens of query and document together.
MAX_DOCUMENT_CHARS = 4000
#: A search waits this long for its rerank, then keeps the fused order. An abandoned call may still be charged
#: one search unit by a metering server.
RERANK_TIMEOUT = 30.0


@dataclass(frozen=True)
class RerankRoute:
    prefix: str
    provider: str
    label: str
    url: str | None  # None: built from `address`
    key: str | None
    address: str | None = None
    path: str = ""
    cost_source: str = "byok"
    top_field: str = "top_n"
    #: USD per search unit, or per 1M tokens; None: unpriced.
    usd_per_search: float | None = None
    usd_per_mtok: dict[str, float] | None = None


ROUTES: tuple[RerankRoute, ...] = (
    RerankRoute("synapse.", "model_gateway", "the model gateway", None, "model_gateway_key",
                address="model_gateway_base_url", path="/v1/rerank", cost_source="platform", usd_per_search=0.002),
    RerankRoute("cohere.", "cohere", "Cohere", "https://api.cohere.com/v2/rerank", "cohere_key",
                usd_per_search=0.002),
    RerankRoute("voyage.", "voyage", "Voyage AI", "https://api.voyageai.com/v1/rerank", "voyage_key",
                top_field="top_k",
                # docs.voyageai.com/docs/pricing, per 1M tokens (2026-10-04): rerank-3 is current (F368).
                usd_per_mtok={"rerank-3-lite": 0.02, "rerank-3": 0.05, "rerank-2.5-lite": 0.02, "rerank-2.5": 0.05}),
    RerankRoute("jina.", "jina", "Jina AI", "https://api.jina.ai/v1/rerank", "jina_key"),
    RerankRoute("tei.", "tei", "the rerank server", None, "knowledge_rerank_key",
                address="knowledge_rerank_base_url", path="/rerank", cost_source="local"),
)


def route_for(model: str) -> RerankRoute | None:
    lowered = (model or "").strip().lower()
    for route in ROUTES:
        if lowered.startswith(route.prefix):
            return route
    return None


def _token_rate(route: RerankRoute, name: str) -> float | None:
    if not route.usd_per_mtok:
        return None
    for key in sorted(route.usd_per_mtok, key=len, reverse=True):
        if name.lower().startswith(key):
            return route.usd_per_mtok[key]
    return None


class ProviderReranker:
    """Reranks with whichever model `spec.model` names (see the module docstring)."""

    max_documents = MAX_DOCUMENTS

    async def rerank(self, query: str, documents: Sequence[str], *, spec: RerankSpec,
                     settings: Mapping[str, Any], attribution: Attribution) -> RerankResult:
        import httpx

        route = route_for(spec.model)
        if route is None:
            raise RerankError(f"No reranker serves {spec.model!r}: name it cohere.…, voyage.…, jina.…, tei.… or "
                              f"synapse.….", retryable=False)
        name = spec.model[len(route.prefix):]
        docs = [d[:MAX_DOCUMENT_CHARS] for d in list(documents)[:MAX_DOCUMENTS]]
        if not docs:
            raise RerankError("Nothing to rerank.", retryable=False)
        top_n = max(1, min(spec.top_n, len(docs)))
        key = str(settings.get(route.key) or "").strip() if route.key else ""
        if route.url is not None:
            url = route.url
            if not key:
                raise RerankError(f"{route.label} reranking needs {route.key} (Settings).", retryable=False)
        else:
            base = str(settings.get(route.address or "") or "").strip().rstrip("/")
            if not base:
                raise RerankError(f"{route.label} reranking needs {route.address} (Settings).", retryable=False)
            if route.provider == "model_gateway":
                from core.llm_providers import _normalize_v1_base_url

                base = _normalize_v1_base_url(base)
            url = base + route.path
        if route.provider == "tei":
            # TEI takes `texts` and answers a list; Infinity takes `documents` and answers `results`. Both
            # ignore the other's field, so one body serves either.
            body: dict[str, Any] = {"query": query, "texts": docs, "documents": docs, "model": name,
                                    "top_n": top_n, "raw_scores": False}
        else:
            body = {"model": name, "query": query, "documents": docs, route.top_field: top_n}
        headers = {"Authorization": f"Bearer {key}"} if key else {}

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=RERANK_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise RerankError(f"{route.label} reranking failed: {type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            raise _refusal(route, resp.status_code, data, getattr(resp, "text", "") or "")

        ranking = _ranking(data, len(docs))
        if ranking is None:
            raise RerankError(f"{route.label} answered the rerank in a shape Synapse does not read.",
                              retryable=False)
        ranking.sort(key=lambda p: (-p[1], p[0]))
        from core.knowledge.costs import rerank_usd_per_search

        rate = await rerank_usd_per_search(spec.model)  # the deployment's, for a platform reranker (F334)
        return RerankResult(ranking=ranking[:top_n], model=spec.model,
                            usage=_usage(route, spec.model, name, data, latency_ms, rate))


def _refusal(route: RerankRoute, status: int, data: Any, text: str) -> Exception:
    error = data.get("error") if isinstance(data, dict) else None
    detail = error if isinstance(error, dict) else {}
    message = (detail.get("message") or (error if isinstance(error, str) else None)
               or (data.get("message") if isinstance(data, dict) else None) or text[:200] or "no message")
    if status == 402 and route.provider == "model_gateway":
        return WalletExhausted(f"The model gateway refused the rerank: {message}",
                               needed_microusd=detail.get("needed_microusd"),
                               available_microusd=detail.get("available_microusd"))
    retryable = status == 429 or status >= 500 or status in (408, 499)
    return RerankError(f"{route.label} refused the rerank: HTTP {status}: {message}", retryable=retryable)


def _ranking(data: Any, count: int) -> list[tuple[int, float]] | None:
    """(index, score) pairs from any of the shapes: Cohere/Jina/our gateway/Infinity `results`, Voyage `data`,
    TEI's bare list."""
    if isinstance(data, dict):
        items = data.get("results", data.get("data"))
    else:
        items = data
    if not isinstance(items, list):
        return None
    out: list[tuple[int, float]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if not isinstance(index, int) or not isinstance(score, (int, float)) or not 0 <= index < count:
            return None
        out.append((index, float(score)))
    return out


def _usage(route: RerankRoute, model: str, name: str, data: Any, latency_ms: int,
           usd_per_search: float | None = None) -> Usage:
    usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
    meta = ((data.get("meta") or {}).get("billed_units") or {}) if isinstance(data, dict) else {}
    charged = usage.get("charged_microusd") if route.provider == "model_gateway" else None
    charged = int(charged) if isinstance(charged, (int, float)) and not isinstance(charged, bool) else None
    source = route.cost_source
    if route.usd_per_mtok is not None or route.provider == "jina":
        tokens = int(usage.get("total_tokens") or 0)
        rate = _token_rate(route, name)
        return Usage(stage="rerank", cost_source=source, model=model, provider=route.provider, input_tokens=tokens,
                     units=float(tokens), unit="tokens", est_usd=tokens * (rate or 0.0) / 1_000_000,
                     priced=rate is not None, latency_ms=latency_ms)
    units = float(usage.get("search_units") or meta.get("search_units") or 1)
    if source == "local":
        return Usage(stage="rerank", cost_source="local", model=model, provider=route.provider, units=units,
                     unit="search_units", latency_ms=latency_ms)
    rate = usd_per_search if usd_per_search is not None else route.usd_per_search
    return Usage(stage="rerank", cost_source=source, model=model, provider=route.provider, units=units,
                 unit="search_units", est_usd=units * (rate or 0.0), charged_microusd=charged,
                 priced=rate is not None, latency_ms=latency_ms)


def from_settings(settings: Mapping[str, Any]) -> ProviderReranker | None:
    """The default reranker: one when a rerank model is configured, else None."""
    model = str(settings.get("knowledge_rerank_model") or "").strip()
    return ProviderReranker() if model else None
