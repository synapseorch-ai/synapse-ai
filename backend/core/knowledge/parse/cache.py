"""The parse cache: a `ParsedDocument` kept by its file's sha256 (16 §3.5).

A re-embed, a blue/green build (B2b) or a retry after an indexing failure
never parses a file again: `service.parse_document` looks here first.

* **Where:** `knowledge/parse-cache/<sha[:2]>/<sha256>.<signature>.json` in the
  blob store — outside `vault/`, so a plan's `vault_mb` counts the source and
  never its parse (D153 #8); the store's tenant prefix keeps tenants apart.
* **The signature** names everything the result depends on besides the bytes:
  the schema, the format and its reader's version, Docling's version when it
  read the file, and the OCR engine (or none). Configuring OCR, upgrading a
  reader or installing Docling therefore parses again; nothing else does.
* **What is kept:** only a clean result. A parse cut by a timeout, or one
  whose OCR failed, is not cached, so a retry tries again. Text formats are
  not cached: reading them is cheaper than reading the cache.
* **A deleted file's text does not linger:** the entry goes with the last
  document that had that content (`forget_unreferenced`), and `sweep` removes
  any whose content no document has any more (a file's older versions).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import posixpath
import time
from typing import TYPE_CHECKING

from core.knowledge.parsed import PARSED_SCHEMA_VERSION, ParsedDocument, SourceInfo

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.parse.formats import Format

PREFIX = "knowledge/parse-cache"
#: `sweep` runs at most this often per process.
SWEEP_EVERY_S = 3600.0
_last_sweep: dict[str, float] = {}


def signature(fmt: Format, *, path: str, docling: bool, ocr_engine: str | None) -> str:
    parts = [f"schema{PARSED_SCHEMA_VERSION}", fmt.name, fmt.version, f"ocr={ocr_engine or 'none'}"]
    if docling:
        try:
            parts.append(f"docling={importlib.metadata.version('docling')}")
        except importlib.metadata.PackageNotFoundError:
            parts.append("docling=?")
    if fmt.name == "csv":
        parts.append(posixpath.basename(path))  # the sheet card prints the file's name
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def key(sha256: str, sig: str) -> str:
    return f"{PREFIX}/{sha256[:2]}/{sha256}.{sig}.json"


async def get(sha256: str, sig: str, source: SourceInfo) -> ParsedDocument | None:
    """The cached parse, re-homed onto `source` (the document asking), or None."""
    from core.log import get_logger
    from core.storage import get_blob_store

    store = get_blob_store()
    data = await asyncio.to_thread(store.get_bytes, key(sha256, sig))
    if data is None:
        return None
    try:
        doc = ParsedDocument.model_validate_json(data)
    except ValueError as exc:  # an entry from an older schema, or a damaged write: parse again
        get_logger(__name__).warning(f"parse cache entry {sha256[:12]} unreadable, dropped: {exc}")
        await asyncio.to_thread(store.delete, key(sha256, sig))
        return None
    return rehome(doc, source)


async def put(sha256: str, sig: str, doc: ParsedDocument) -> None:
    """Keep `doc`. Never raises: a cache that cannot be written only costs a later parse."""
    from core.log import get_logger
    from core.storage import get_blob_store

    try:
        await asyncio.to_thread(get_blob_store().put_bytes, key(sha256, sig), doc.model_dump_json().encode(),
                                content_type="application/json")
    except Exception as exc:  # noqa: BLE001
        get_logger(__name__).warning(f"parse cache write for {sha256[:12]} failed: {exc}")


def cacheable(doc: ParsedDocument, *, cut: str | None) -> bool:
    """Only a clean parse is kept: not one a timeout cut, not one whose OCR failed."""
    return cut is None and not any(w.code in ("ocr_failed", "parser_timeout", "parser_memory", "parser_crashed")
                                   for w in doc.warnings)


def rehome(doc: ParsedDocument, source: SourceInfo) -> ParsedDocument:
    """`doc` as parsed from `source`: the same bytes at another path, or from another source.
    Attachments' paths (`<parent>#<name>`) follow their parent's."""
    old = doc.source.path

    def move(d: ParsedDocument, src: SourceInfo) -> ParsedDocument:
        attachments = []
        for a in d.attachments:
            if a.parsed is not None:
                inner = a.parsed.source
                path = src.path + inner.path[len(old):] if inner.path.startswith(old + "#") else inner.path
                a = a.model_copy(update={"parsed": move(a.parsed, inner.model_copy(update={"path": path}))})
            attachments.append(a)
        return d.model_copy(update={"source": src, "attachments": attachments})

    return move(doc, source)


async def forget_unreferenced(sha256: str | None) -> int:
    """Delete every cache entry for `sha256` if no document of this tenant has that content any more."""
    if not sha256 or await _referenced({sha256}):
        return 0
    return await _delete_for({sha256})


async def sweep(*, force: bool = False) -> int:
    """Delete the entries whose content no document of this tenant has (a replaced version, a delete
    that was missed). At most once an hour per tenant and process unless `force`."""
    from core.storage import get_blob_store
    from core.tenancy import get_tenant

    tenant = get_tenant()
    now = time.monotonic()
    if not force and now - _last_sweep.get(tenant, -SWEEP_EVERY_S) < SWEEP_EVERY_S:
        return 0
    _last_sweep[tenant] = now
    stats = await asyncio.to_thread(get_blob_store().list_stats, PREFIX + "/")
    shas = {posixpath.basename(s.key).split(".", 1)[0] for s in stats}
    if not shas:
        return 0
    stale = shas - await _referenced(shas)
    return await _delete_for(stale) if stale else 0


async def _referenced(shas: set[str]) -> set[str]:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    found: set[str] = set()
    ordered = sorted(shas)
    async with session() as s:
        for start in range(0, len(ordered), 500):
            chunk = ordered[start:start + 500]
            rows = await s.execute(select(D.sha256).where(D.tenant_id == get_tenant(), D.sha256.in_(chunk)))
            found.update(r for (r,) in rows if r)
    return found


async def _delete_for(shas: set[str]) -> int:
    from core.storage import get_blob_store

    store = get_blob_store()
    deleted = 0
    for sha in shas:  # list the directory: the local store lists directories, not name prefixes
        for stat in await asyncio.to_thread(store.list_stats, f"{PREFIX}/{sha[:2]}/"):
            if posixpath.basename(stat.key).startswith(f"{sha}."):
                await asyncio.to_thread(store.delete, stat.key)
                deleted += 1
    return deleted
