"""
Shared disk-backed key/value store for the cache layer.

Each cached value lives in its own JSON file under data/cache/<namespace>/<aa>/<full_hash>.json
where <aa> is the first two hex chars of the hash (avoids cramming thousands of
files into a single directory).

Format on disk:
{
  "value": <jsonable>,
  "created_at": <unix ts>,
  "ttl_seconds": <int|None>,
  "meta": {...}        // arbitrary caller metadata (tool_name, model, etc.)
}

The store is intentionally simple — no LRU, no compression, no Redis. The
hot path is one open()+json.load() per lookup; for the dataset sizes we care
about (tens of MB per namespace) this is well under a millisecond.
"""
import hashlib
import json
import threading
import time
from typing import Any, Optional

from core.storage import get_blob_store

_lock = threading.Lock()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _blob_key(namespace: str, key_hash: str) -> str:
    """Blob key for a cache entry.

    The blob store prefixes this with the tenant, which is the whole point of
    routing the cache through it. Cache keys are content hashes — model plus
    messages, or tool name plus arguments — so without a tenant dimension two
    tenants asking the same question share an answer. That is a cross-tenant
    read on an ordinary request, no race required.
    """
    return f"cache/{namespace}/{key_hash[:2]}/{key_hash}.json"


def make_key(*parts: Any) -> str:
    """Build a deterministic cache key from arbitrary parts.

    Dicts/lists are serialised with sort_keys so attribute order doesn't break
    the hash. Bytes and tuples are coerced via repr.
    """
    norm: list[str] = []
    for p in parts:
        if p is None:
            norm.append("\x00")
        elif isinstance(p, (dict, list)):
            norm.append(json.dumps(p, sort_keys=True, default=str, separators=(",", ":")))
        else:
            norm.append(str(p))
    return _hash_key("\x1f".join(norm))


def get(namespace: str, key: str) -> Optional[dict]:
    """Return the cached entry dict, or None if missing/expired."""
    key_hash = key if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else _hash_key(key)
    blob = _blob_key(namespace, key_hash)
    raw = get_blob_store().get(blob)
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except Exception:
        return None
    ttl = entry.get("ttl_seconds")
    if ttl is not None and ttl > 0:
        age = time.time() - entry.get("created_at", 0)
        if age > ttl:
            try:
                get_blob_store().delete(blob)
            except Exception:
                pass
            return None
    return entry


def set(namespace: str, key: str, value: Any, ttl_seconds: Optional[int] = None, meta: Optional[dict] = None) -> str:
    """Persist `value` under `key` in `namespace`. Returns the key hash."""
    key_hash = key if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else _hash_key(key)
    entry = {
        "value": value,
        "created_at": time.time(),
        "ttl_seconds": ttl_seconds,
        "meta": meta or {},
    }
    with _lock:
        get_blob_store().put(
            _blob_key(namespace, key_hash),
            json.dumps(entry, ensure_ascii=False, default=str),
        )
    return key_hash


def delete(namespace: str, key: str) -> bool:
    key_hash = key if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else _hash_key(key)
    blob = _blob_key(namespace, key_hash)
    store = get_blob_store()
    if not store.exists(blob):
        return False
    try:
        store.delete(blob)
        return True
    except Exception:
        return False


def clear_namespace(namespace: str) -> int:
    """Delete every entry under a namespace, for this tenant only."""
    store = get_blob_store()
    removed = 0
    with _lock:
        for key in store.list(f"cache/{namespace}"):
            try:
                store.delete(key)
                removed += 1
            except Exception:
                pass
    return removed


def stats() -> dict:
    """Per-namespace entry count and total bytes, for this tenant only."""
    store = get_blob_store()
    out: dict[str, dict] = {}
    for key in store.list("cache"):
        parts = key.split("/")
        # cache/<namespace>/<shard>/<hash>.json
        if len(parts) < 2:
            continue
        ns = parts[1]
        bucket = out.setdefault(ns, {"entries": 0, "bytes": 0})
        bucket["entries"] += 1
        raw = store.get(key)
        if raw is not None:
            bucket["bytes"] += len(raw.encode("utf-8"))
    return out
