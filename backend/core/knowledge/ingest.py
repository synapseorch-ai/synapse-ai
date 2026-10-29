"""How files enter (and leave) a knowledge base through its API: uploads, records, document deletes.

**Uploads** follow `api_models.UploadSession`, the shape cloud serves with
presigned S3 multipart URLs. OSS serves the parts itself
(`PUT /api/knowledge/uploads/{id}/parts/{n}`): each part is at most
`PART_SIZE` (8 MiB, well under the web app's 32 MiB request limit,
`frontend/src/lib/requestBodyLimit.ts`) and lands in
the blob store under `knowledge/uploads/<id>/` — outside `vault/`, so a
half-finished upload is never a Vault file and never counts toward a plan's
storage. Completing streams the parts, in order and each checked against its
ETag, into the Vault file in one all-or-nothing write, then queues the
document. A session expires after `SESSION_TTL`; the reconcile timer sweeps
what is left. A session opened through the public API (`/api/v1`) carries its
own token in each part's `headers` and its parts need it (CCR-8): a part PUT
there carries no key, as cloud's presigned PUTs carry none.

**Records** become `<folder>/_records/<id>.json`; the same id replaces the
file, and unchanged content queues nothing.

**Deleting a document deletes its Vault file** (Naveen, D157): otherwise the
next reconcile would find the file and add the document back.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import secrets
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from core.knowledge.errors import KnowledgeError
from core.knowledge.ids import RECORDS_DIR, UPLOAD_PREFIX, WEB_DIR, new_id
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import RecordIngestRequest, UploadComplete, UploadInit, UploadSession
    from core.knowledge.kb_settings import KnowledgeBase
    from core.store.models import KnowledgeDocumentDB

_log = get_logger(__name__)

MIB = 1024 * 1024
PART_SIZE = 8 * MIB
SESSION_TTL = timedelta(hours=24)
UPLOADS = "knowledge/uploads"

_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class UploadError(KnowledgeError):
    """An upload the API refuses; `status` is the HTTP status it answers with."""

    code = "upload_refused"

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


class NotFound(KnowledgeError, LookupError):
    """No such upload, record or document here (a 404)."""

    code = "not_found"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _store():
    from core.storage import get_blob_store

    return get_blob_store()


# ── paths ──────────────────────────────────────────────────────────────────────

def _segment(name: str) -> str:
    """One file name, by the Vault's own rules (`core/routes/vault.py::_segment`)."""
    name = unicodedata.normalize("NFC", name or "")
    if (not name or name != name.strip() or name in (".", "..") or any(ch in name for ch in "/\\[]")
            or name.startswith(".") or any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in name)
            or len(name.encode("utf-8")) > 255):
        raise UploadError("A file name is one segment: no slashes or brackets, no leading dot, no surrounding "
                          "spaces or control characters, at most 255 bytes.")
    return name


def upload_path(kb: KnowledgeBase, filename: str, folder: str | None) -> str:
    """Where an upload lands: `folder` inside the base (its root by default), then the file name.

    `folder` may be given relative to the base or as the full Vault path. The
    base's `_records/` and `_web/` belong to records and fetched pages.
    """
    name = _segment(filename)
    inner = unicodedata.normalize("NFC", (folder or "").replace("\\", "/")).strip("/")
    if inner == kb.folder or inner.startswith(kb.folder + "/"):
        inner = inner[len(kb.folder):].strip("/")
    parts = [p for p in inner.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError("An upload's folder cannot contain '..'.")
    for p in parts:
        _segment(p)
    if parts and parts[0] in (RECORDS_DIR, WEB_DIR):
        raise UploadError(f"'{parts[0]}' holds a knowledge base's records and fetched pages; upload elsewhere.")
    path = "/".join([kb.folder, *parts, name])
    if len(path.encode("utf-8")) > 1024:
        raise UploadError("The file's path is longer than 1024 bytes.")
    return path


# ── uploads ────────────────────────────────────────────────────────────────────

def _manifest_key(upload_id: str) -> str:
    return f"{UPLOADS}/{upload_id}/manifest.json"


def _part_key(upload_id: str, n: int) -> str:
    return f"{UPLOADS}/{upload_id}/part-{n:05d}"


async def _manifest(upload_id: str) -> dict[str, Any]:
    from core.knowledge.ids import is_id

    if not is_id(upload_id, UPLOAD_PREFIX):
        raise NotFound("No such upload.")
    raw = await asyncio.to_thread(_store().get, _manifest_key(upload_id))
    if raw is None:
        raise NotFound("No such upload, or it finished or expired.")
    manifest = json.loads(raw)
    if datetime.fromisoformat(manifest["expires_at"]) < _now():
        await _discard(upload_id)
        raise NotFound("This upload expired; start it again.")
    return manifest


def check_file(path: str, size_bytes: int) -> None:
    """A file a knowledge base takes: a type it reads (415) and within the size per file (413). An upload's rule, and
    a Vault file's added by the builder (F1200)."""
    from core.knowledge.config import knowledge_settings
    from core.knowledge.parse import ACCEPTED_EXTENSIONS, extension

    if extension(path) not in ACCEPTED_EXTENSIONS:
        raise UploadError(f"{extension(path) or 'A file without an extension'} is not a type a knowledge base "
                          "takes.", status=415)
    cap = knowledge_settings().max_file_mb
    if size_bytes > cap * MIB:
        raise UploadError(f"The file is {size_bytes / MIB:.1f} MB; the limit is {cap} MB per file.", status=413)


async def vault_file_lands(kb: KnowledgeBase, source: str) -> tuple[str, str, int]:
    """Where a Vault file added to `kb` lands, by an upload's rules (`upload_path`, `check_file`), checked before
    anything is written: (the file's Vault path, where it lands, its size). One already in the base's folder lands
    where it is; one elsewhere, in the base's root under its own name, never over a file there (F1200)."""
    from core.knowledge import kb_settings
    from core.vault import mention_key

    key = mention_key(source)
    stat = await asyncio.to_thread(_store().stat, key) if key else None
    if stat is None:
        raise NotFound(f"There is no file at {source!r} in the Vault.")
    rel = key.split("/", 1)[1]
    if kb_settings.kb_for_path(rel, [kb]) is kb:
        check_file(rel, stat.size)
        return rel, rel, stat.size
    path = upload_path(kb, rel.rsplit("/", 1)[-1], None)
    check_file(path, stat.size)
    if await asyncio.to_thread(_store().stat, f"vault/{path}") is not None:
        raise UploadError(f"{path} is already in this knowledge base; rename the Vault file to add it as well.",
                          status=409)
    return rel, path, stat.size


async def add_vault_file(kb: KnowledgeBase, source: str) -> list[KnowledgeDocumentDB]:
    """A file the person already has in the Vault, added to `kb` (F1200): copied to where `vault_file_lands` says,
    and queued."""
    from core.knowledge import sync
    from core.vault_events import VaultChange

    rel, path, _ = await vault_file_lands(kb, source)
    if path == rel:
        return await sync.on_vault_change(VaultChange(path=rel, op="write", source="route"))
    store = _store()
    size, sha = await asyncio.to_thread(store.put_stream, f"vault/{path}", store.iter_bytes(f"vault/{rel}") or iter(()))
    return await sync.on_vault_change(VaultChange(path=path, op="write", sha256=sha, size=size, source="upload"))


async def start_upload(kb: KnowledgeBase, body: UploadInit, *, part_url: Callable[[str, int], str],
                       token_header: str | None = None) -> UploadSession:
    """Open an upload session for one file into `kb`.

    With `token_header`, the session mints a token, keeps it in its manifest and names it in each part's
    `headers`; `put_part` then takes a part only with it."""
    from core.knowledge.api_models import UploadPart, UploadSession

    path = upload_path(kb, body.filename, body.path)
    check_file(path, body.size_bytes)
    upload_id = new_id(UPLOAD_PREFIX)
    parts = max(1, math.ceil(body.size_bytes / PART_SIZE))
    expires = _now() + SESSION_TTL
    manifest = {"upload_id": upload_id, "kb_id": kb.id, "path": path, "size_bytes": body.size_bytes,
                "mime": body.mime, "metadata": body.metadata, "part_size": PART_SIZE, "parts": parts,
                "created_at": _now().isoformat(), "expires_at": expires.isoformat()}
    headers: dict[str, str] = {}
    if token_header:
        manifest["part_token"] = secrets.token_urlsafe(32)
        headers = {token_header: manifest["part_token"]}
    await asyncio.to_thread(_store().put, _manifest_key(upload_id), json.dumps(manifest))
    return UploadSession(upload_id=upload_id, part_size=PART_SIZE, expires_at=expires.isoformat(),
                         parts=[UploadPart(part_number=n, url=part_url(upload_id, n), headers=dict(headers))
                                for n in range(1, parts + 1)])


async def put_part(upload_id: str, part_number: int, body: AsyncIterator[bytes], *,
                   token: str | None = None) -> str:
    """Store one part; returns its ETag (the part's MD5, as S3's).

    `token` is what the public route was sent (`""` when nothing was): it must be the session's own.
    None is the internal route, behind the app's own gate, which checks none."""
    manifest = await _manifest(upload_id)
    if token is not None:
        expected = manifest.get("part_token")
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise NotFound("No such upload, or it finished or expired.")  # the same answer as an unknown one
    if not 1 <= part_number <= manifest["parts"]:
        raise UploadError(f"This upload has parts 1 to {manifest['parts']}.", status=404)
    buf = bytearray()
    async for piece in body:
        buf.extend(piece)
        if len(buf) > manifest["part_size"]:
            raise UploadError(f"A part is at most {manifest['part_size']} bytes.", status=413)
    data = bytes(buf)
    await asyncio.to_thread(_store().put_bytes, _part_key(upload_id, part_number), data)
    return hashlib.md5(data).hexdigest()  # noqa: S324 — an ETag, not a secret


async def complete_upload(upload_id: str, body: UploadComplete) -> list[KnowledgeDocumentDB]:
    """Assemble the parts into the Vault file and queue its document."""
    from core.knowledge import bases, sync
    from core.vault_events import VaultChange

    manifest = await _manifest(upload_id)
    numbers = [p.part_number for p in body.parts]
    if sorted(numbers) != list(range(1, manifest["parts"] + 1)):
        raise UploadError(f"Complete names each of parts 1 to {manifest['parts']} once.")
    etags = {p.part_number: p.etag.strip('"').lower() for p in body.parts}
    store = _store()
    sizes = []
    for n in range(1, manifest["parts"] + 1):
        stat = await asyncio.to_thread(store.stat, _part_key(upload_id, n))
        if stat is None:
            raise UploadError(f"Part {n} was never uploaded.")
        sizes.append(stat.size)
    if sum(sizes) != manifest["size_bytes"]:
        raise UploadError(f"The parts add up to {sum(sizes)} bytes; the upload said {manifest['size_bytes']}.")
    kb = await bases.get_base(manifest["kb_id"])
    if kb is None or kb.status != "active" or not manifest["path"].startswith(kb.folder + "/"):
        raise NotFound("The knowledge base this upload was for is gone, disabled or moved.")

    def parts() -> Iterator[bytes]:
        for n in range(1, manifest["parts"] + 1):
            digest = hashlib.md5()  # noqa: S324
            for piece in store.iter_bytes(_part_key(upload_id, n)) or ():
                digest.update(piece)
                yield piece
            if digest.hexdigest() != etags[n]:
                raise UploadError(f"Part {n} does not match its ETag; upload it again.")

    size, sha = await asyncio.to_thread(store.put_stream, f"vault/{manifest['path']}", parts(),
                                        content_type=manifest["mime"])
    await _discard(upload_id)
    return await sync.on_vault_change(
        VaultChange(path=manifest["path"], op="write", sha256=sha, size=size, source="upload"),
        metadata=manifest["metadata"])


async def cancel_upload(upload_id: str) -> None:
    """Give up an upload: its parts and its manifest go (F341, CCR-12). `NotFound` for one that is unknown,
    finished, expired or already cancelled — the same answer as for a part or a complete."""
    await _manifest(upload_id)
    await _discard(upload_id)


async def _discard(upload_id: str) -> None:
    def remove() -> None:
        store = _store()
        for key in store.list(f"{UPLOADS}/{upload_id}/"):
            store.delete(key)
        store.delete(f"{UPLOADS}/{upload_id}")

    await asyncio.to_thread(remove)


async def sweep_expired_uploads() -> int:
    """Remove upload sessions past their expiry. Returns how many."""
    def expired() -> list[str]:
        store = _store()
        out = []
        for key in store.list(f"{UPLOADS}/"):
            if not key.endswith("/manifest.json"):
                continue
            try:
                manifest = json.loads(store.get(key) or "{}")
                if datetime.fromisoformat(manifest["expires_at"]) < _now():
                    out.append(manifest["upload_id"])
            except Exception:  # noqa: BLE001 — an unreadable manifest is left for a person
                continue
        return out

    gone = await asyncio.to_thread(expired)
    for upload_id in gone:
        await _discard(upload_id)
    return len(gone)


# ── records ────────────────────────────────────────────────────────────────────

def record_path(kb: KnowledgeBase, record_id: str) -> str:
    return f"{kb.folder}/{RECORDS_DIR}/{record_id}.json"


def _record_file(record_id: str, record) -> str:
    """The file a record is stored as. Deterministic, so sending the same record twice changes nothing."""
    doc: dict[str, Any] = {"id": record_id}
    if record.title is not None:
        doc["title"] = record.title
    if record.doc_date is not None:
        doc["doc_date"] = record.doc_date
    doc["metadata"] = record.metadata
    if record.text is not None:
        doc["text"] = record.text
    else:
        doc["json"] = record.json_
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


async def put_records(kb: KnowledgeBase, body: RecordIngestRequest) -> list[KnowledgeDocumentDB]:
    from core.knowledge import sync
    from core.vault_events import VaultChange

    out = []
    for record in body.records:
        record_id = record.id or uuid.uuid4().hex
        path = record_path(kb, record_id)
        data = _record_file(record_id, record)
        await asyncio.to_thread(_store().put, f"vault/{path}", data)
        raw = data.encode("utf-8")
        meta = {**record.metadata, **({"title": record.title} if record.title is not None else {}),
                **({"doc_date": record.doc_date} if record.doc_date is not None else {})}
        out += await sync.on_vault_change(
            VaultChange(path=path, op="write", sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                        source="record"), metadata=meta)
    return out


async def delete_record(kb: KnowledgeBase, record_id: str) -> None:
    from core.knowledge.ids import is_record_id

    if not is_record_id(record_id):
        raise NotFound("No such record.")
    path = record_path(kb, record_id)
    if not await asyncio.to_thread(_store().exists, f"vault/{path}"):
        raise NotFound("No such record.")
    await _delete_file(path)


# ── documents ──────────────────────────────────────────────────────────────────

async def _delete_file(path: str) -> None:
    from core.knowledge import sync
    from core.vault_events import VaultChange

    await asyncio.to_thread(_store().delete, f"vault/{path}")
    await sync.on_vault_change(VaultChange(path=path, op="delete", source="route"))


async def delete_document(row: KnowledgeDocumentDB) -> None:
    """Delete the document's Vault file, and with it the document."""
    from core.knowledge import dispatch, documents

    await _delete_file(row.path)
    current = await documents.get(row.id)
    if current is not None and current.status != "deleting":  # its base was disabled: tombstone it directly
        gone = await documents.tombstone(row.id)
        if gone is not None:
            await dispatch.enqueue(row.kb_id, "delete", "vault_delete", path=row.path, doc_id=row.id)


async def retry_document(row: KnowledgeDocumentDB) -> KnowledgeDocumentDB | None:
    """Queue a resting document again. None when it is not resting (someone is working on it)."""
    from core.knowledge import dispatch, documents
    from core.knowledge.status import RESTING

    if row.status not in RESTING:
        return None
    again = await documents.transition(row.id, "queued", previous=row.status)
    if again is not None:
        await dispatch.enqueue(row.kb_id, "retry", "retry", path=again.path, doc_id=again.id)
    return again

