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
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core.config import DATA_DIR

CACHE_ROOT = Path(DATA_DIR) / "cache"

_lock = threading.Lock()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _path_for(namespace: str, key_hash: str) -> Path:
    return CACHE_ROOT / namespace / key_hash[:2] / f"{key_hash}.json"


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
    path = _path_for(namespace, key_hash)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
    except Exception:
        return None
    ttl = entry.get("ttl_seconds")
    if ttl is not None and ttl > 0:
        age = time.time() - entry.get("created_at", 0)
        if age > ttl:
            try:
                path.unlink()
            except Exception:
                pass
            return None
    return entry


def set(namespace: str, key: str, value: Any, ttl_seconds: Optional[int] = None, meta: Optional[dict] = None) -> str:
    """Persist `value` under `key` in `namespace`. Returns the key hash."""
    key_hash = key if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else _hash_key(key)
    path = _path_for(namespace, key_hash)
    entry = {
        "value": value,
        "created_at": time.time(),
        "ttl_seconds": ttl_seconds,
        "meta": meta or {},
    }
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    return key_hash


def delete(namespace: str, key: str) -> bool:
    key_hash = key if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else _hash_key(key)
    path = _path_for(namespace, key_hash)
    if path.exists():
        try:
            path.unlink()
            return True
        except Exception:
            return False
    return False


def clear_namespace(namespace: str) -> int:
    """Delete every entry under a namespace. Returns the count removed."""
    base = CACHE_ROOT / namespace
    if not base.exists():
        return 0
    removed = 0
    with _lock:
        for p in base.rglob("*.json"):
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    return removed


def stats() -> dict:
    """Return per-namespace entry count and total bytes on disk."""
    out: dict[str, dict] = {}
    if not CACHE_ROOT.exists():
        return out
    for ns_dir in CACHE_ROOT.iterdir():
        if not ns_dir.is_dir():
            continue
        count = 0
        size = 0
        for p in ns_dir.rglob("*.json"):
            try:
                count += 1
                size += p.stat().st_size
            except Exception:
                pass
        out[ns_dir.name] = {"entries": count, "bytes": size}
    return out
