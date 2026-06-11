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
import json
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


# ── Semantic cache (optional, ChromaDB-backed via memory.MemoryStore) ─────────
#
# Implementation is intentionally light. We reuse the same embedding pipeline
# the chat memory layer uses, store the (system+user) text in a per-step Chroma
# collection, and persist the response text in our flat-file store keyed by
# the document's ID. A high similarity threshold (0.95 by default) keeps
# semantic hits limited to nearly-identical prompts.

_semantic_collections: dict[str, Any] = {}
# Track step IDs we've already warned about so the "embedding unavailable" log
# fires at most once per step per process — avoids spamming during a long run.
_warned_no_embed_steps: set[str] = set()


def _get_memory_store():
    """Resolve the live MemoryStore from server module (initialised at startup)."""
    try:
        from core import server as _server
        return getattr(_server, "memory_store", None)
    except Exception:
        return None


def _get_semantic_collection(step_id: str):
    """Lazy ChromaDB collection per step. Returns None on failure (cache disabled)."""
    if step_id in _semantic_collections:
        return _semantic_collections[step_id]
    mem = _get_memory_store()
    if mem is None or not getattr(mem, "client", None):
        _semantic_collections[step_id] = None
        return None
    try:
        coll = mem.client.get_or_create_collection(name=f"{NAMESPACE_SEMANTIC_PREFIX}{step_id}")
        _semantic_collections[step_id] = coll
        return coll
    except Exception as e:
        print(f"DEBUG cache: semantic cache unavailable ({e}); falling back to exact only")
        _semantic_collections[step_id] = None
        return None


def _embed(text: str) -> Optional[list[float]]:
    mem = _get_memory_store()
    if mem is None:
        return None
    try:
        return mem.get_embedding(text)
    except Exception:
        return None


def _warn_no_embedding(step_id: str) -> None:
    """One-shot log when the embedding provider is unreachable for this step."""
    if step_id in _warned_no_embed_steps:
        return
    _warned_no_embed_steps.add(step_id)
    print(
        f"DEBUG cache: ⚠️  semantic cache disabled for step '{step_id}' — "
        f"embedding provider unavailable. Start Ollama with `nomic-embed-text` "
        f"or set `embedding_model` in Settings. Exact-match cache still works.",
        flush=True,
    )


def get_semantic(
    step_id: str,
    model: str,
    system: str | None,
    user_message: str,
    threshold: float = 0.95,
) -> Optional[dict]:
    """Return the response from the closest semantic neighbour, if any beat threshold."""
    coll = _get_semantic_collection(step_id)
    if coll is None:
        return None
    emb = _embed((system or "") + "\n\n" + user_message)
    if emb is None:
        _warn_no_embedding(step_id)
        return None
    try:
        res = coll.query(query_embeddings=[emb], n_results=1)
    except Exception:
        return None
    ids = (res.get("ids") or [[]])[0]
    distances = (res.get("distances") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    if not ids:
        return None
    # Chroma returns cosine distance; similarity = 1 - distance.
    similarity = 1.0 - float(distances[0])
    if similarity < threshold:
        return None
    if metas[0].get("model") != model:
        return None
    entry = store.get(NAMESPACE_EXACT, ids[0])
    if entry is None:
        return None
    return entry.get("value")


def set_semantic(
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
    coll = _get_semantic_collection(step_id)
    if coll is None:
        return
    emb = _embed((system or "") + "\n\n" + user_message)
    if emb is None:
        _warn_no_embedding(step_id)
        return
    # Reuse the exact-cache key as the Chroma document ID so storage stays unified.
    key = store.make_key("resp_semantic", model, step_id, user_message)
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
            metadatas=[{"model": model, "step_id": step_id}],
        )
    except Exception as e:
        print(f"DEBUG cache: semantic upsert failed ({e})")
