"""Which rerank models a workspace's keys can use (BF9, D293).

Knowledge's rerank pickers list these, the way PROV1's model list reads a chat provider's own list: a person
pastes a key and sees what it answers for, never a name the key cannot call.

| Provider | How its models are learned |
|---|---|
| Cohere | its own list, `GET /v1/models?endpoint=rerank`, the deprecated ones left out |
| Voyage AI, Jina AI | none published: the list we keep (`CATALOG`, or the host's `catalog`) once a rerank answers |
| TEI / Infinity | the server says what it serves: TEI's `GET /info` (`model_id`), else Infinity's `GET /models` |

Our gateway's own (`synapse.`) is the host's to add: OSS has none, and cloud lists its rows while they answer.

A refused key lists nothing and says so on its card, in the chat cards' words. A Voyage or Jina check that cannot
finish (a rate limit, a 5xx, the network) keeps the list and says the key could not be checked. One provider's
failure never hides another's. A clean answer is kept for `TTL_S` per key, unless asked again with `refresh`;
a refusal or a failure is asked again each time.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.knowledge.rerankers import ROUTES, RerankRoute

#: The rerank models of the vendors that publish no list, newest first: the first is the one the key check calls.
#: docs.voyageai.com/docs/reranker and jina.ai/reranker, read 2026-10-17. Cloud's staff keep their own copy.
CATALOG: dict[str, tuple[str, ...]] = {
    "voyage": ("rerank-3", "rerank-3-lite", "rerank-2.5", "rerank-2.5-lite"),
    "jina": ("jina-reranker-v3.5", "jina-reranker-v3", "jina-reranker-m0", "jina-reranker-v2-base-multilingual"),
}
TTL_S = 900
TIMEOUT_S = 5.0
_COHERE_MODELS = "https://api.cohere.com/v1/models"
_MAX_PAGES = 5


@dataclass(frozen=True)
class RerankModel:
    id: str
    label: str


@dataclass(frozen=True)
class RerankListing:
    provider: str
    label: str
    #: The setting that lights this provider: its key, or the rerank server's address.
    key: str
    models: tuple[RerankModel, ...]
    #: Why nothing is listed: the key was refused, or the list could not be read.
    error: str | None = None
    #: The list stands, but something about the key is worth saying.
    note: str | None = None


_cache: dict[tuple[str, str], tuple[float, RerankListing]] = {}


def forget() -> None:
    """Drop every kept answer (tests; a host whose keys changed may also call it)."""
    _cache.clear()


def _route(provider: str) -> RerankRoute:
    return next(r for r in ROUTES if r.provider == provider)


def _models(route: RerankRoute, names: Sequence[str]) -> tuple[RerankModel, ...]:
    return tuple(RerankModel(id=f"{route.prefix}{name}", label=name) for name in dict.fromkeys(names) if name)


def _refused(route: RerankRoute, status: int) -> RerankListing:
    return RerankListing(route.provider, route.label, route.key or "", (),
                         error=f"{route.label} refused this key ({status})")


async def _cohere(key: str) -> RerankListing:
    import httpx

    route = _route("cohere")
    names: list[str] = []
    params: dict[str, Any] = {"endpoint": "rerank", "page_size": 1000}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            for _ in range(_MAX_PAGES):
                resp = await client.get(_COHERE_MODELS, params=dict(params),
                                        headers={"Authorization": f"Bearer {key}"})
                if resp.status_code in (401, 403):
                    return _refused(route, resp.status_code)
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                data = resp.json()
                for model in data.get("models") or []:
                    if isinstance(model, dict) and model.get("name") and not model.get("is_deprecated") \
                            and "rerank" in (model.get("endpoints") or ["rerank"]):
                        names.append(str(model["name"]))
                token = data.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
    except Exception as exc:  # noqa: BLE001 — one provider's failure is said on its card, never raised
        return RerankListing(route.provider, route.label, route.key or "", (),
                             error=f"{route.label}'s model list could not be read ({type(exc).__name__}: {exc})")
    return RerankListing(route.provider, route.label, route.key or "", _models(route, names))


async def _kept(provider: str, key: str, names: Sequence[str]) -> RerankListing:
    """Voyage's or Jina's kept list, once one rerank on the key answers."""
    import httpx

    route = _route(provider)
    names = list(names)
    if not names:
        return RerankListing(route.provider, route.label, route.key or "", (),
                             error=f"There are no rerank models listed for {route.label}.")
    body = {"model": names[0], "query": "check", "documents": ["check"], route.top_field: 1}
    note = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(route.url or "", json=body, headers={"Authorization": f"Bearer {key}"})
        if resp.status_code in (401, 403):
            return _refused(route, resp.status_code)
        if resp.status_code >= 400:
            note = f"The key could not be checked just now ({route.label} answered HTTP {resp.status_code})."
    except Exception as exc:  # noqa: BLE001 — one provider's failure is said on its card, never raised
        note = f"The key could not be checked just now ({type(exc).__name__})."
    return RerankListing(route.provider, route.label, route.key or "", _models(route, names), note=note)


async def _server(base: str, key: str) -> RerankListing:
    """What a TEI or Infinity server says it serves."""
    import httpx

    route = _route("tei")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    names: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            info = await client.get(f"{base}/info", headers=headers)
            served = _json(info) if info.status_code < 400 else None
            if isinstance(served, dict) and served.get("model_id"):
                names.append(str(served["model_id"]))
            else:
                listed = await client.get(f"{base}/models", headers=headers)
                if listed.status_code in (401, 403):
                    return RerankListing(route.provider, route.label, route.address or "", (),
                                         error=f"The rerank server refused its key ({listed.status_code})")
                answer = _json(listed) if listed.status_code < 400 else None
                if isinstance(answer, dict):
                    for model in answer.get("data") or []:
                        capabilities = model.get("capabilities") if isinstance(model, dict) else None
                        if isinstance(model, dict) and model.get("id") and (
                                capabilities is None or "rerank" in capabilities):
                            names.append(str(model["id"]))
    except Exception as exc:  # noqa: BLE001 — one provider's failure is said on its card, never raised
        return RerankListing(route.provider, route.label, route.address or "", (),
                             error=f"The rerank server at {base} could not be reached ({type(exc).__name__}).")
    if not names:
        return RerankListing(route.provider, route.label, route.address or "", (),
                             error=f"The rerank server at {base} did not say which model it serves.")
    return RerankListing(route.provider, route.label, route.address or "", _models(route, names))


def _json(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


async def _cached(provider: str, secret: str, refresh: bool,
                  fetch: Callable[[], Awaitable[RerankListing]]) -> RerankListing:
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()
    now = time.monotonic()
    kept = _cache.get((provider, fingerprint))
    if kept and not refresh and kept[0] > now:
        return kept[1]
    listing = await fetch()
    if listing.error is None and listing.note is None:  # a refusal or a passing failure is asked again next time
        _cache[(provider, fingerprint)] = (now + TTL_S, listing)
    return listing


async def list_rerank_models(settings: Mapping[str, Any], *, refresh: bool = False,
                             catalog: Mapping[str, Sequence[str]] | None = None) -> list[RerankListing]:
    """Every provider this workspace has a key (or a rerank server) for, with the models it can use.

    `catalog` is the host's own kept list for the vendors that publish none (cloud's staff edit theirs); left
    out, `CATALOG`."""
    kept = CATALOG if catalog is None else catalog
    asks: list[Awaitable[RerankListing]] = []
    order: list[RerankRoute] = []
    for provider in ("cohere", "voyage", "jina"):
        route = _route(provider)
        key = str(settings.get(route.key or "") or "").strip()
        if not key:
            continue
        names = tuple(kept.get(provider) or ())
        fetch = (lambda key=key: _cohere(key)) if provider == "cohere" else (
            lambda provider=provider, key=key, names=names: _kept(provider, key, names))
        asks.append(_cached(provider, f"{key}\n{'|'.join(names)}", refresh, fetch))
        order.append(route)
    base = str(settings.get("knowledge_rerank_base_url") or "").strip().rstrip("/")
    if base:
        key = str(settings.get("knowledge_rerank_key") or "").strip()
        asks.append(_cached("tei", f"{base}\n{key}", refresh, lambda: _server(base, key)))
        order.append(_route("tei"))

    answers = await asyncio.gather(*asks, return_exceptions=True)
    out: list[RerankListing] = []
    for route, answer in zip(order, answers):
        if isinstance(answer, BaseException):
            answer = RerankListing(route.provider, route.label, route.key or route.address or "", (),
                                   error=f"{route.label}'s models could not be read ({type(answer).__name__}).")
        out.append(answer)
    return out
