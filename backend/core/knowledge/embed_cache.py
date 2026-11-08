"""A document's answered embeddings, kept across a pause so they are never bought twice (F345, PG1a).

A document whose embedding stops part-way — the wallet empties (402), a rate limit outlasts its wait, the
provider is down, the job is cancelled or times out — has paid for the passages answered so far. Indexing writes
no row until every passage is embedded, so without this a resume embeds, and pays for, those passages again.

* **Written only when embedding stops** (Naveen's call 3 in D183): a document that indexes writes nothing here.
* **Read on the next attempt** — a resume, a retry, a rebuild's next try: a passage whose exact searched text
  was answered is not sent again.
* **Dropped** once the document's rows are written: the index holds the vectors from then on.
* **Where:** beside the parse cache, `knowledge/parse-cache/<sha[:2]>/<sha256>.emb-<signature>.json`, so the
  parse cache's own clean-up (`forget_unreferenced`, `sweep`: every `<sha256>.` entry once no document has that
  content) removes it too, in OSS and in cloud. Outside `vault/`: a plan's `vault_mb` never counts it.
* **The signature** is the model, the width asked for and the model's profile version: another model, width or
  profile writes another file. **An entry** is keyed by the passage's searched text (its context and its words),
  so a passage whose context changed is embedded again; vectors are float32 (`vectors.pack`), as the index keeps
  them.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

from core.knowledge.parse.cache import PREFIX

#: Bump when an entry's shape changes.
SCHEMA = 1


def signature(model: str, dims: int | None, profile_version: str | None) -> str:
    blob = f"emb{SCHEMA}|{model}|{dims if dims else 'native'}|{profile_version or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def key(sha256: str, sig: str) -> str:
    return f"{PREFIX}/{sha256[:2]}/{sha256}.emb-{sig}.json"


def entry_key(text: str) -> str:
    """One passage, as it was embedded: its searched text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


async def load(sha256: str | None, sig: str) -> dict[str, list[float]]:
    """The vectors kept for this content under `sig` ({} when none, or when the entry cannot be read — it is
    dropped, and those passages are embedded again)."""
    from core.knowledge.vectors import unpack
    from core.log import get_logger
    from core.storage import get_blob_store

    if not sha256:
        return {}
    store = get_blob_store()
    try:
        data = await asyncio.to_thread(store.get_bytes, key(sha256, sig))
    except Exception as exc:  # noqa: BLE001 — a cache that cannot be read only costs the calls
        get_logger(__name__).warning(f"embedding cache read for {sha256[:12]} failed: {exc}")
        return {}
    if data is None:
        return {}
    try:
        payload = json.loads(data)
        entries = payload.get("vectors") if isinstance(payload, dict) and payload.get("schema") == SCHEMA else None
        if not isinstance(entries, dict):
            raise ValueError("not an embedding cache entry")
        found = {str(k): unpack(base64.b64decode(v)) for k, v in entries.items()}
        if len({len(v) for v in found.values()}) > 1 or any(not v for v in found.values()):
            raise ValueError("vectors of more than one width")
        return found
    except (ValueError, TypeError) as exc:
        get_logger(__name__).warning(f"embedding cache entry {sha256[:12]} unreadable, dropped: {exc}")
        await drop(sha256, sig)
        return {}


async def save(sha256: str | None, sig: str, vectors: dict[str, list[float]], *, model: str) -> None:
    """Keep `vectors` (every passage answered for this content and signature, by `entry_key`). Never raises;
    never writes an empty entry."""
    from core.knowledge.vectors import pack
    from core.log import get_logger
    from core.storage import get_blob_store

    if not sha256 or not vectors:
        return
    blob = json.dumps({"schema": SCHEMA, "model": model,
                       "vectors": {k: base64.b64encode(pack(v)).decode("ascii") for k, v in vectors.items()}},
                      sort_keys=True).encode("utf-8")
    try:
        await asyncio.to_thread(get_blob_store().put_bytes, key(sha256, sig), blob, content_type="application/json")
    except Exception as exc:  # noqa: BLE001 — a vector not kept only costs a later call
        get_logger(__name__).warning(f"embedding cache write for {sha256[:12]} failed: {exc}")


async def drop(sha256: str | None, sig: str) -> None:
    """Forget the vectors once the document's rows hold them. Never raises."""
    from core.log import get_logger
    from core.storage import get_blob_store

    if not sha256:
        return
    try:
        await asyncio.to_thread(get_blob_store().delete, key(sha256, sig))
    except Exception as exc:  # noqa: BLE001 — the sweep removes it with its content
        get_logger(__name__).warning(f"embedding cache delete for {sha256[:12]} failed: {exc}")
