"""Following the Vault: the hook every write point calls, and reconcile for what the hook never sees.

**The hook.** `observe` is installed as the Vault's one observer
(`core.vault_events`) when the server starts. It reads `knowledge_enabled()`
on every change, so switching Knowledge on or off takes effect without a
restart, and off means it does nothing. A write under a knowledge base's
folder queues that document; a delete (a file or a whole folder) tombstones
every document at or under the path and queues its delete; a move is both.
Knowledge's own writes — an upload's completion, a record — call
`on_vault_change` directly, because they need the document back and carry
metadata a `VaultChange` cannot.

**Reconcile** compares a knowledge base's folder with its documents: new files
are queued, changed ones (size or mtime differs, then the sha256 does) are
queued again, missing ones are deleted, and work left behind — a job lost to
a crash, a document stuck mid-parse, a tombstone whose delete failed — is
queued again. A file skipped as a format not read yet is queued again once
it is read. A web page's refusal or failure (S1) is a document with no file
(`sha256` null): reconcile leaves it be. It catches every write the hook never sees: the sandbox's
`vault_*` tools (a separate process, with no observer), the Filesystem MCP,
`run_bash`, a crash between a write and its notification.

OSS runs it at startup and every `RECONCILE_INTERVAL_S` (Naveen, D157). Cloud
never scans (it arms on write): cloud's app has its own lifespan and calls
`reconcile` from its own job.
"""
from __future__ import annotations

import asyncio
import hashlib
import posixpath
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from core.knowledge.kb_settings import REFUSED_ROOTS, folders_overlap, kb_for_path
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase
    from core.store.models import KnowledgeDocumentDB
    from core.vault_events import VaultChange

_log = get_logger(__name__)

RECONCILE_INTERVAL_S = 60.0
#: Work older than this in a working state (or queued with no job waiting) is queued again.
STUCK_AFTER = timedelta(minutes=15)

#: A VaultChange's source → the document source a new document takes (the path decides first).
_DOC_SOURCE = {"route": "vault", "tool": "vault", "reconcile": "vault", "upload": "upload", "record": "record",
               "fetch": "web"}
_REASON = {"route": "vault_write", "tool": "vault_write", "reconcile": "reconcile", "upload": "upload",
           "record": "record", "fetch": "url"}


def clean_path(path: str) -> str:
    """A Vault path as documents store it: NFC, POSIX, no leading or trailing slash."""
    return unicodedata.normalize("NFC", (path or "").replace("\\", "/")).strip("/")


def ignored(path: str) -> bool:
    """Files no knowledge base indexes: dotfiles (`.keep` markers) and a store's own temp files."""
    name = posixpath.basename(path)
    return not name or name.startswith(".") or name.endswith(".tmp")


def _key(path: str) -> str:
    return f"vault/{path}"


async def _fingerprint(path: str) -> tuple[str, int, datetime | None] | None:
    """(sha256, size, mtime) of a Vault file, or None when it is not there."""
    from core.storage import get_blob_store

    def read() -> tuple[str, int, datetime | None] | None:
        store = get_blob_store()
        stat = store.stat(_key(path))
        pieces = store.iter_bytes(_key(path))
        if stat is None or pieces is None:
            return None
        digest, size = hashlib.sha256(), 0
        for piece in pieces:
            digest.update(piece)
            size += len(piece)
        return digest.hexdigest(), size, stat.modified_at

    return await asyncio.to_thread(read)


async def _given(change: VaultChange, path: str) -> tuple[str, int, datetime | None] | None:
    """The writer's own hash and size (it had the bytes), with the file's mtime."""
    from core.storage import get_blob_store

    stat = await asyncio.to_thread(get_blob_store().stat, _key(path))
    if stat is None:
        return None
    return change.sha256, change.size, stat.modified_at  # type: ignore[return-value]


# ── the hook ───────────────────────────────────────────────────────────────────

async def observe(change: VaultChange) -> None:
    """The installed Vault observer: `on_vault_change`, while Knowledge is on."""
    from core.knowledge.config import knowledge_enabled

    path = clean_path(change.path)
    if path.split("/", 1)[0] in REFUSED_ROOTS or not knowledge_enabled():
        return  # the engine's own folders are never a knowledge base: no database read for a tool output
    await on_vault_change(change)


def install() -> None:
    from core.vault_events import set_vault_observer

    set_vault_observer(observe)


async def on_vault_change(change: VaultChange, *, metadata: dict | None = None,
                          source_url: str | None = None) -> list[KnowledgeDocumentDB]:
    """Act on one Vault change; returns the documents it queued or tombstoned."""
    from core.knowledge import bases

    path = clean_path(change.path)
    kbs = [kb for kb in await bases.list_bases() if kb.status == "active"]
    if not kbs or not path:
        return []
    if change.op == "mkdir":
        return []
    if change.op == "delete":
        return await _deleted(path, kbs, reason="vault_delete")
    if change.op == "move":
        out = await _deleted(clean_path(change.old_path or ""), kbs, reason="vault_move") if change.old_path else []
        return out + await _written(path, kbs, change, metadata=metadata, source_url=source_url)
    return await _written(path, kbs, change, metadata=metadata, source_url=source_url)


async def _written(path: str, kbs: list[KnowledgeBase], change: VaultChange, *, metadata: dict | None,
                   source_url: str | None) -> list[KnowledgeDocumentDB]:
    from core.knowledge import dispatch, documents

    kb = kb_for_path(path, kbs)
    if kb is None or ignored(path) or change.is_dir:
        return []
    found = await _given(change, path) if change.sha256 and change.size is not None else await _fingerprint(path)
    if found is None:  # gone again before we looked
        return await _deleted(path, kbs, reason="vault_delete")
    sha, size, modified = found
    row, queued = await documents.upsert_for_path(
        kb, path, source=_DOC_SOURCE.get(change.source, "vault"), sha256=sha, size=size, modified_at=modified,
        meta=metadata, source_url=source_url)
    if queued:
        await dispatch.enqueue(kb.id, "upsert", _REASON.get(change.source, "vault_write"),  # type: ignore[arg-type]
                               path=row.path, doc_id=row.id)
    return [row]


async def _deleted(path: str, kbs: list[KnowledgeBase], *, reason: str) -> list[KnowledgeDocumentDB]:
    """Tombstone every document at or under `path`, in every base it touches, and queue the deletes."""
    from core.knowledge import dispatch, documents

    out = []
    for kb in kbs:
        if not path or not folders_overlap(path, kb.folder):
            continue
        prefix = path if len(path.split("/")) >= len(kb.folder.split("/")) else kb.folder
        for row in await documents.tombstone_under(kb.id, prefix):
            await dispatch.enqueue(kb.id, "delete", reason, path=row.path, doc_id=row.id)  # type: ignore[arg-type]
            out.append(row)
    return out


# ── a knowledge base's folder ──────────────────────────────────────────────────

async def ensure_folder(kb: KnowledgeBase) -> None:
    """Create the base's Vault folder if it is missing (a `.keep` marker on an object store)."""
    from core.storage import get_blob_store

    def make() -> None:
        store = get_blob_store()
        local = store.path_for(_key(kb.folder))
        if local is not None:
            local.mkdir(parents=True, exist_ok=True)
        elif not store.list(_key(kb.folder) + "/"):
            store.put(_key(f"{kb.folder}/.keep"), "\n")  # never empty: a zero-byte PUT stalls MinIO

    await asyncio.to_thread(make)


async def rehome(before: KnowledgeBase, after: KnowledgeBase) -> None:
    """A base moved to another folder: the old folder's documents go, the new folder's come."""
    from core.knowledge import dispatch, documents

    for row in await documents.tombstone_under(before.id):
        await dispatch.enqueue(before.id, "delete", "reconcile", path=row.path, doc_id=row.id)
    await ensure_folder(after)
    await dispatch.enqueue(after.id, "reindex", "reconcile")


# ── reconcile ──────────────────────────────────────────────────────────────────

@dataclass
class Drift:
    """What reconcile found out of step, by kind. All zero: the base was in step."""

    added: int = 0
    changed: int = 0
    touched: int = 0
    removed: int = 0
    stuck: int = 0
    supported: int = 0
    #: Chunks indexed keyword-only that this pass embedded (B2a's backfill).
    embedded: int = 0

    def any(self) -> bool:
        return any(asdict(self).values())

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _aware(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _same_moment(a: datetime | None, b: datetime | None) -> bool:
    a, b = _aware(a), _aware(b)
    return a is not None and b is not None and abs((a - b).total_seconds()) < 1e-3


def _scanned_pages_unread(row) -> bool:
    """A document indexed (or skipped) before OCR was configured: pages with no text layer, and no OCR
    stage in its report. An OCR stage that ran — read, failed or empty — is never retried from here."""
    if row.status not in ("partial", "skipped") or not isinstance(row.report, dict):
        return False
    pages = row.report.get("pages") or {}
    stages = row.report.get("stages") or []
    return (pages.get("no_text_layer") or 0) > (pages.get("ocr_applied") or 0) and not any(
        isinstance(s, dict) and s.get("stage") == "ocr" for s in stages)


async def reconcile(kb: KnowledgeBase, *, startup: bool = False) -> Drift:
    """Bring a base's documents in step with its folder. `startup`: nothing from before is still
    running, so any document left mid-work is queued again, whatever its age. Last, chunks indexed
    keyword-only are embedded once a model can embed them (`backfill.embed_missing`, D163)."""
    from core.knowledge import dispatch, documents
    from core.knowledge.parse import supports
    from core.knowledge.seams import get_ocr_provider
    from core.storage import get_blob_store

    drift = Drift()
    if kb.status != "active":
        return drift
    files = {}
    for stat in await asyncio.to_thread(get_blob_store().list_stats, _key(kb.folder)):
        path = clean_path(stat.key.removeprefix("vault/"))
        if not ignored(path) and kb_for_path(path, [kb]) is kb:
            files[path] = stat
    rows = {row.path: row for row in await documents.list_for(kb.id)}
    now = datetime.now(timezone.utc)

    for path, stat in files.items():
        row = rows.get(path)
        if row is not None and (row.status == "deleting" or (
                row.size_bytes == stat.size and _same_moment(row.source_modified_at, stat.modified_at))):
            continue
        found = await _fingerprint(path)
        if found is None:
            continue
        sha, size, modified = found
        if row is not None and row.sha256 == sha:
            await documents.set_columns(row.id, source_modified_at=modified, size_bytes=size)
            drift.touched += 1
            continue
        fresh, queued = await documents.upsert_for_path(kb, path, source="vault", sha256=sha, size=size,
                                                        modified_at=modified)
        if queued:
            await dispatch.enqueue(kb.id, "upsert", "reconcile", path=fresh.path, doc_id=fresh.id)
            if row is None:
                drift.added += 1
            else:
                drift.changed += 1

    ocr_ready = get_ocr_provider() is not None
    for path, row in rows.items():
        if path in files:
            readable = row.status == "skipped" and row.status_reason == "unsupported_format" and supports(path)
            if readable or (ocr_ready and _scanned_pages_unread(row)):
                again = await documents.transition(row.id, "queued", previous=row.status)
                if again is not None:
                    await dispatch.enqueue(kb.id, "upsert", "reconcile", path=path, doc_id=row.id)
                    drift.supported += 1
            continue
        if row.source == "web" and row.sha256 is None:
            continue  # a fetch's refusal or failure (S1): it never had a file, so none is missing
        if row.status != "deleting":
            gone = await documents.tombstone(row.id)
            if gone is not None:
                await dispatch.enqueue(kb.id, "delete", "reconcile", path=path, doc_id=row.id)
                drift.removed += 1

    for row in await documents.list_for(kb.id):
        if row.status not in ("queued", "parsing", "indexing", "deleting"):
            continue
        since = _aware({"parsing": row.started_at, "indexing": row.started_at, "queued": row.queued_at}.get(
            row.status, row.updated_at)) or _aware(row.updated_at)  # a tombstone's age is its last update
        stale = startup or since is None or now - since > STUCK_AFTER
        if row.status == "queued":
            job = dispatch.job_for(kb.id, "upsert", "reconcile", path=row.path, doc_id=row.id)
            if stale and not dispatch.is_pending(job):
                await dispatch.enqueue(kb.id, "upsert", "reconcile", path=row.path, doc_id=row.id)
                drift.stuck += 1
        elif not stale:
            continue
        elif row.status == "deleting":
            await dispatch.enqueue(kb.id, "delete", "reconcile", path=row.path, doc_id=row.id)
            drift.stuck += 1
        else:
            again = await documents.transition(row.id, "queued", previous=row.status)
            if again is not None:
                await dispatch.enqueue(kb.id, "upsert", "reconcile", path=row.path, doc_id=row.id)
                drift.stuck += 1

    from core.knowledge.backfill import embed_missing  # never raises

    drift.embedded = await embed_missing(kb)
    if drift.any():
        _log.info(f"knowledge reconcile {kb.id}: {drift.as_dict()}")
    return drift


async def reconcile_all(*, startup: bool = False) -> dict[str, Drift]:
    from core.knowledge import bases

    out = {}
    for kb in await bases.list_bases():
        try:
            out[kb.id] = await reconcile(kb, startup=startup)
        except Exception as exc:  # noqa: BLE001 — one base's failure never stops the others
            _log.warning(f"knowledge reconcile of {kb.id} failed: {exc}")
    try:
        from core.knowledge.parse.cache import sweep

        await sweep()  # parses of content no document has any more; at most hourly
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"parse cache sweep failed: {exc}")
    return out


async def reconcile_forever(interval: float = RECONCILE_INTERVAL_S) -> None:
    """OSS's loop: at startup, then every `interval` seconds, while Knowledge is on. Each pass also
    sweeps upload sessions past their expiry and starts the web crawls that are due (S1)."""
    from core.knowledge.config import knowledge_enabled

    startup = not _scale_mode()
    while True:
        try:
            if knowledge_enabled():
                await reconcile_all(startup=startup)
                startup = False
                from core.knowledge.ingest import sweep_expired_uploads

                await sweep_expired_uploads()
                await _due_crawls()
        except Exception as exc:  # noqa: BLE001 — the loop outlives any one pass
            _log.warning(f"knowledge reconcile pass failed: {exc}")
        await asyncio.sleep(interval)


async def _due_crawls() -> None:
    """Re-crawls that are due, and crawls a restart interrupted (`web.start_due_crawls`, S1)."""
    try:
        from core.knowledge.web import start_due_crawls

        await start_due_crawls()
    except Exception as exc:  # noqa: BLE001 — a crawl's trouble never stops reconcile
        _log.warning(f"knowledge web crawls not started: {exc}")


def _scale_mode() -> bool:
    try:
        from core.scale.config import get_scale_config

        return bool(get_scale_config().scale_mode)
    except Exception:  # noqa: BLE001
        return False


def start() -> asyncio.Task[Any]:
    """Install the hook and start the reconcile loop (OSS's server lifespan). Returns the loop's task."""
    install()
    return asyncio.create_task(reconcile_forever(), name="knowledge-reconcile")
