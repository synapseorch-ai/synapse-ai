"""Contextual Retrieval's context cache: what a model wrote for a passage, kept by the file's content (CR1, D180).

A rebuild, a retry after a failed call, or a job that died part-way never pays twice for a context it already
has: `enrich` looks here first, and writes what it got after every window of calls.

* **Where:** beside the parse cache, `knowledge/parse-cache/<sha[:2]>/<sha256>.ctx-<signature>.json` — keyed by
  the same file sha256, so the parse cache's own clean-up (`parse.cache.forget_unreferenced`, `sweep`, which
  delete every `<sha256>.` entry once no document has that content) removes it too, in OSS and in cloud alike.
  It is outside `vault/`, so a plan's `vault_mb` never counts it; the store's tenant prefix keeps tenants apart.
* **The signature** names what a context depends on besides the file: the model, the prompt's version and the
  window sizes. Another model, prompt or window writes another file.
* **An entry** is keyed by the passage's window and its text (`entry_key`), so a chunking change re-asks only
  for passages whose text or window changed. Only a usable answer is kept: a passage that kept the
  deterministic prefix is asked again next time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from core.knowledge.parse.cache import PREFIX

#: Bump when an entry's shape changes.
SCHEMA = 1


def signature(model: str, *, prompt_version: str, windows: str) -> str:
    """What a context depends on besides the file: the model, the prompt, the window sizes."""
    blob = f"ctx{SCHEMA}|{prompt_version}|{windows}|{model}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def key(sha256: str, sig: str) -> str:
    return f"{PREFIX}/{sha256[:2]}/{sha256}.ctx-{sig}.json"


def entry_key(window_id: str, content: str) -> str:
    """One passage, as asked: the window the model saw and the passage's own text."""
    return hashlib.sha256(f"{window_id}\n{content}".encode("utf-8")).hexdigest()[:32]


async def load(sha256: str, sig: str) -> dict[str, str]:
    """The contexts kept for this file under `sig` ({} when none, or when the entry is unreadable)."""
    from core.log import get_logger
    from core.storage import get_blob_store

    if not sha256:
        return {}
    store = get_blob_store()
    try:
        data = await asyncio.to_thread(store.get_bytes, key(sha256, sig))
    except Exception as exc:  # noqa: BLE001 — a cache that cannot be read only costs the calls
        get_logger(__name__).warning(f"context cache read for {sha256[:12]} failed: {exc}")
        return {}
    if data is None:
        return {}
    try:
        payload = json.loads(data)
        entries = payload.get("contexts") if isinstance(payload, dict) and payload.get("schema") == SCHEMA else None
        if not isinstance(entries, dict):
            raise ValueError("not a context cache entry")
        return {str(k): str(v) for k, v in entries.items() if isinstance(v, str) and v}
    except ValueError as exc:
        get_logger(__name__).warning(f"context cache entry {sha256[:12]} unreadable, dropped: {exc}")
        try:
            await asyncio.to_thread(store.delete, key(sha256, sig))
        except Exception:  # noqa: BLE001
            pass
        return {}


async def save(sha256: str, sig: str, contexts: dict[str, str], *, model: str) -> None:
    """Keep `contexts` (the whole set for this file and signature). Never raises; never writes an empty entry."""
    from core.log import get_logger
    from core.storage import get_blob_store

    if not sha256 or not contexts:
        return
    blob = json.dumps({"schema": SCHEMA, "model": model, "contexts": contexts},
                      ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        await asyncio.to_thread(get_blob_store().put_bytes, key(sha256, sig), blob, content_type="application/json")
    except Exception as exc:  # noqa: BLE001 — a cache that cannot be written only costs a later call
        get_logger(__name__).warning(f"context cache write for {sha256[:12]} failed: {exc}")
