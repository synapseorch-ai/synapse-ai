"""
LLM response cache — exact-match + optional semantic-match.

Exact match: SHA256 of (model, system_prompt, messages, tools_json). O(1) lookup.
Semantic match: embed the last user message, compare against prior cached entries
                for the same (model, system_prompt) family.

By design, this cache is OFF unless a caller explicitly opts in. AGENT steps in
orchestration must NEVER consult it (their behaviour is state-dependent and the
shared_state mutations from skipping the LLM call would diverge silently).
LLM / EVALUATOR / EXTRACT_JSON steps can opt in safely.
"""
import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Optional

from core.cache import store

NAMESPACE_EXACT = "responses_exact"
# Semantic cache is opt-in per step; entries are scoped by step_id to keep
# behaviour comparable to exact match (similar prompts on the same step only).
NAMESPACE_SEMANTIC_PREFIX = "responses_semantic_"


def _build_exact_key(
    model: str,
    system: str | None,
    messages: list[dict] | None,
    tools: list[dict] | None,
) -> str:
    # Tools are normalised to a stable string — list of function names + their schemas.
    tools_norm: list[dict] = []
    for t in tools or []:
        fn = t.get("function", {}) if isinstance(t, dict) else {}
        tools_norm.append({
            "name": fn.get("name", ""),
            "params": fn.get("parameters", {}),
        })
    return store.make_key(
        "resp",
        model or "",
        system or "",
        messages or [],
        tools_norm,
    )


def get_exact(
    model: str,
    system: str | None,
    messages: list[dict] | None,
    tools: list[dict] | None,
) -> Optional[dict]:
    """Return the cached response entry {"text", "input_tokens", "output_tokens"} or None."""
    key = _build_exact_key(model, system, messages, tools)
    entry = store.get(NAMESPACE_EXACT, key)
    if entry is None:
        return None
    return entry.get("value")


def set_exact(
    model: str,
    system: str | None,
    messages: list[dict] | None,
    tools: list[dict] | None,
    *,
    text: str,
    input_tokens: int,
    output_tokens: int,
    ttl_seconds: int = 3600,
    step_id: str | None = None,
) -> None:
    key = _build_exact_key(model, system, messages, tools)
    store.set(
        NAMESPACE_EXACT,
        key,
        {
            "text": text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        ttl_seconds=ttl_seconds,
        meta={"model": model, "step_id": step_id},
    )


# ── Semantic backend seam ─────────────────────────────────────────────────────
#
# The local semantic cache below keys per-step Chroma collections, which in a
# multi-tenant process is a cross-tenant leak by construction (finding T1) —
# it is why multi-tenant workers refuse to attach a memory store at all. An
# embedder registers a backend that scopes entries properly (org + step + a
# hash of the system prompt and history); the local behaviour stays the
# single-tenant default.

_backend: Any = None


def set_response_cache_backend(backend: Any) -> None:
    """Install a semantic-cache backend. None restores the local default.

    A backend implements `async get(step_id, model, system, user_message,
    threshold) -> dict | None` and `async set(step_id, model, system,
    user_message, *, text, input_tokens, output_tokens, ttl_seconds)`.
    """
    global _backend
    _backend = backend


async def get_semantic_routed(
    step_id: str,
    model: str,
    system: str | None,
    user_message: str,
    threshold: float = 0.95,
) -> Optional[dict]:
    if _backend is not None:
        return await _backend.get(step_id, model, system, user_message, threshold)
    return await get_semantic(step_id, model, system, user_message, threshold)


async def set_semantic_routed(
    step_id: str,
    model: str,
    system: str | None,
    user_message: str,
    *,
    text: str,
    input_tokens: int,
    output_tokens: int,
    ttl_seconds: int = 3600,
) -> None:
    if _backend is not None:
        await _backend.set(
            step_id, model, system, user_message,
            text=text, input_tokens=input_tokens,
            output_tokens=output_tokens, ttl_seconds=ttl_seconds,
        )
        return
    await set_semantic(
        step_id, model, system, user_message,
        text=text, input_tokens=input_tokens,
        output_tokens=output_tokens, ttl_seconds=ttl_seconds,
    )


# ── Semantic cache (the local default: Chroma, embedded with the Embedding Model) ─
#
# Each step keeps its entries in its own Chroma collection on the memory
# store's client, one per embedding model (another model's width starts it
# afresh), measuring cosine. Only the user's message is embedded, and an entry
# matches only under the same chat model and system prompt: embedding a long
# system prompt with it made every message on a step look alike (F327). The
# response text lives in the flat-file store under the document's id. A high
# similarity threshold (0.95 by default) keeps hits to nearly identical prompts.
#
# The vectors come from Settings → Models' Embedding Model through
# `embed_batch_ex`, which raises rather than answering zeros. With none set,
# or when it fails, the step is exact-match only and the log says why, once.

_EMBED_CHARS = 20_000      # the end of a long message, where its question usually is
_EMBED_WAIT_S = 30.0
_EMBEDDED_KEEP = 32

_semantic_collections: dict[tuple[str, str], Any] = {}
# Steps already told why their semantic cache is off: once per step per
# process (and again after a settings save), not on every call of a long run.
_warned_no_embed_steps: set[str] = set()
# A miss and the set that follows embed one text: the vector is kept for it.
_embedded: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()


class _NoEmbedding(Exception):
    """Why a step's semantic cache cannot run now, as a sentence."""


def forget_local_state() -> None:
    """Forget the local semantic cache's collections, warnings and vectors.

    Saving settings calls it, so the next call reads the Embedding Model
    afresh (and drops the collections an earlier model left).
    """
    _semantic_collections.clear()
    _warned_no_embed_steps.clear()
    _embedded.clear()


#: A process's own vector store, when it is not the API's: a single-tenant
#: scale worker builds one (`WorkerServerModule._attach_memory_store`) on its
#: own module, which nothing read — so Semantic match was exact-only on every
#: OSS worker (F393). A multi-tenant process sets none: cloud uses `_backend`.
_memory_store = None


def set_memory_store(store) -> None:
    """Give this process's semantic cache its vector store. None falls back to the API's."""
    global _memory_store
    _memory_store = store


def _get_memory_store():
    """This process's vector store: a worker's own, else the API's (set at startup)."""
    if _memory_store is not None:
        return _memory_store
    try:
        from core import server as _server
        return getattr(_server, "memory_store", None)
    except Exception:
        return None


def _embedding_settings() -> tuple[str, dict]:
    from core.config import load_settings

    settings = load_settings()
    model = (settings.get("embedding_model") or "").strip()
    if not model:
        raise _NoEmbedding("no Embedding Model is set — choose one in Settings → Models")
    return model, settings


def _system_hash(system: str | None) -> str:
    return hashlib.sha256((system or "").encode()).hexdigest()[:16]


def _collection_name(step_id: str, embedding_model: str) -> str:
    return f"{NAMESPACE_SEMANTIC_PREFIX}{step_id}__{hashlib.sha256(embedding_model.encode()).hexdigest()[:8]}"


def _drop_other_collections(client: Any, step_id: str, keep: str) -> None:
    """This step's collections from other embedding models, or from before F327."""
    mine = re.compile(re.escape(f"{NAMESPACE_SEMANTIC_PREFIX}{step_id}") + r"(__[0-9a-f]{8})?")
    for c in client.list_collections():
        name = c if isinstance(c, str) else getattr(c, "name", "")
        if name != keep and mine.fullmatch(name):
            client.delete_collection(name)


def _semantic_collection(step_id: str, embedding_model: str):
    key = (step_id, embedding_model)
    if key in _semantic_collections:
        return _semantic_collections[key]
    mem = _get_memory_store()
    client = getattr(mem, "client", None) if mem is not None else None
    if client is None:
        raise _NoEmbedding("this process has no vector store")
    name = _collection_name(step_id, embedding_model)
    try:
        _drop_other_collections(client, step_id, keep=name)
        coll = client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine", "embedding_model": embedding_model})
    except Exception as e:  # noqa: BLE001 — the cache never fails a step
        raise _NoEmbedding(f"its vector store failed ({e})") from e
    _semantic_collections[key] = coll
    return coll


async def _embed(text: str, embedding_model: str, settings: dict) -> list[float]:
    from core import llm_providers as llm

    text = (text or "")[-_EMBED_CHARS:]
    key = (embedding_model, hashlib.sha256(text.encode()).hexdigest())
    if key in _embedded:
        _embedded.move_to_end(key)
        return _embedded[key]
    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(
            llm.embed_batch_ex([text], embedding_model, settings, input_type="query"), _EMBED_WAIT_S)
    except asyncio.TimeoutError as e:
        raise _NoEmbedding(f"the Embedding Model ({embedding_model}) did not answer in {_EMBED_WAIT_S:.0f} s") from e
    except Exception as e:  # noqa: BLE001 — EmbeddingFailed carries the sentence
        raise _NoEmbedding(str(e) or f"the Embedding Model ({embedding_model}) did not embed") from e
    await _log_embedding(answer, text, time.monotonic() - started)
    vector = answer.vectors[0]
    _embedded[key] = vector
    while len(_embedded) > _EMBEDDED_KEEP:
        _embedded.popitem(last=False)
    return vector


async def _log_embedding(answer, text: str, seconds: float) -> None:
    """A usage row for the call (F394): Usage counted none, so a paid Embedding Model went unseen."""
    from core import usage_tracker

    try:
        await usage_tracker.log_embedding(
            model=answer.model, provider=answer.provider, source="cache",
            input_tokens=answer.input_tokens or usage_tracker.estimate_tokens([text]),
            estimated=not answer.input_tokens, charged_microusd=answer.charged_microusd,
            latency_seconds=seconds)
    except Exception as e:  # noqa: BLE001 — a usage row never fails the cache
        print(f"DEBUG cache: the embedding's usage row was not written ({e})")


async def _ready(step_id: str, user_message: str) -> tuple[Any, list[float]] | None:
    """This step's collection and the message's vector, or None (said once)."""
    try:
        embedding_model, settings = _embedding_settings()
        coll = _semantic_collection(step_id, embedding_model)
        return coll, await _embed(user_message, embedding_model, settings)
    except _NoEmbedding as why:
        _warn_no_embedding(step_id, str(why))
        return None


def _warn_no_embedding(step_id: str, reason: str) -> None:
    """One log line per step saying why its semantic cache is off."""
    if step_id in _warned_no_embed_steps:
        return
    _warned_no_embed_steps.add(step_id)
    print(
        f"DEBUG cache: ⚠️  semantic cache off for step '{step_id}': {reason.rstrip('. ')}. "
        f"Exact-match cache still works.",
        flush=True,
    )


async def get_semantic(
    step_id: str,
    model: str,
    system: str | None,
    user_message: str,
    threshold: float = 0.95,
) -> Optional[dict]:
    """Return the response from the closest semantic neighbour, if any beat threshold."""
    ready = await _ready(step_id, user_message)
    if ready is None:
        return None
    coll, emb = ready
    try:
        res = coll.query(
            query_embeddings=[emb], n_results=1,
            where={"$and": [{"model": model}, {"system_hash": _system_hash(system)}]},
        )
    except Exception:
        return None
    ids = (res.get("ids") or [[]])[0]
    distances = (res.get("distances") or [[]])[0]
    if not ids:
        return None
    # The collection measures cosine distance; similarity = 1 - distance.
    similarity = 1.0 - float(distances[0])
    if similarity < threshold:
        return None
    entry = store.get(NAMESPACE_EXACT, ids[0])
    if entry is None:
        return None
    return entry.get("value")


async def set_semantic(
    step_id: str,
    model: str,
    system: str | None,
    user_message: str,
    *,
    text: str,
    input_tokens: int,
    output_tokens: int,
    ttl_seconds: int = 3600,
) -> None:
    ready = await _ready(step_id, user_message)
    if ready is None:
        return
    coll, emb = ready
    system_hash = _system_hash(system)
    # Reuse the exact-cache key as the Chroma document ID so storage stays unified.
    key = store.make_key("resp_semantic", model, step_id, system_hash, user_message)
    store.set(
        NAMESPACE_EXACT,
        key,
        {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens},
        ttl_seconds=ttl_seconds,
        meta={"model": model, "step_id": step_id, "semantic": True},
    )
    try:
        coll.upsert(
            ids=[key],
            embeddings=[emb],
            documents=[(user_message or "")[:2000]],
            metadatas=[{"model": model, "step_id": step_id, "system_hash": system_hash}],
        )
    except Exception as e:
        print(f"DEBUG cache: semantic upsert failed ({e})")
