"""The documents of a knowledge base: one `knowledge_documents` row per Vault file.

**Every status change is a compare-and-set** (`transition`):
`UPDATE … WHERE status IN status.allowed_from(target)`, the first statement of
its own transaction. A worker that lost a race — to a delete, or to a newer
write that queued the document again — sees `None` and stops; it never writes
over what the winner decided.

**What the fingerprint columns mean.** `sha256`, `size_bytes`,
`source_modified_at` and `mime` describe the content the document was last
queued or attempted with, whatever the outcome. Reconcile compares a file with
them, so a document that failed on some content is not queued again every
minute: only new content, a retry, or a newer parser queues it again.
`version` is the index version of the content last indexed, and bumps only on
success (`jobs.py`).

Lifecycle events (`events.emit`) go out on `queued`, on every resting status and
on `deleted`; they never fail the change.
"""
from __future__ import annotations

import mimetypes
import posixpath
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from core.knowledge.ids import DOCUMENT_PREFIX, EVENT_PREFIX, RECORDS_DIR, WEB_DIR, new_id
from core.knowledge.status import REASONS, RESTING, allowed_from
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import DocumentDetail, DocumentOut
    from core.knowledge.kb_settings import KnowledgeBase
    from core.store.models import KnowledgeDocumentDB

_log = get_logger(__name__)

#: Lifecycle statuses announced on a transition. `deleted` is announced by `delete_row`.
_ANNOUNCED = frozenset(RESTING | {"queued"})

_MIMES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".csv": "text/csv",
    ".tsv": "text/tab-separated-values", ".json": "application/json", ".yaml": "application/yaml",
    ".yml": "application/yaml", ".eml": "message/rfc822", ".msg": "application/vnd.ms-outlook",
    ".heic": "image/heic", ".webp": "image/webp",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mime_for(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    return _MIMES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def source_for(path: str, kb_folder: str, fallback: str) -> str:
    """`record` under `<folder>/_records/`, `web` under `<folder>/_web/`, else `fallback`."""
    rest = path[len(kb_folder):].lstrip("/") if path.startswith(kb_folder + "/") else path
    top = rest.split("/", 1)[0]
    if top == RECORDS_DIR:
        return "record"
    if top == WEB_DIR:
        return "web"
    return fallback


def _lift(meta: Mapping[str, Any] | None) -> tuple[dict | None, str | None, str | None]:
    """Metadata as stored, and the title and date it names (lifted into their columns)."""
    if meta is None:
        return None, None, None
    out = dict(meta)
    title = out.pop("title", None)
    doc_date = out.pop("doc_date", None)
    return out, (str(title) if title is not None else None), (str(doc_date) if doc_date is not None else None)


# ── reads ──────────────────────────────────────────────────────────────────────

async def get(doc_id: str) -> KnowledgeDocumentDB | None:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeDocumentDB
    from core.tenancy import get_tenant

    async with session() as s:
        return (await s.execute(select(KnowledgeDocumentDB).where(
            KnowledgeDocumentDB.tenant_id == get_tenant(), KnowledgeDocumentDB.id == doc_id))).scalar_one_or_none()


async def get_by_path(kb_id: str, path: str) -> KnowledgeDocumentDB | None:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeDocumentDB
    from core.tenancy import get_tenant

    async with session() as s:
        return (await s.execute(select(KnowledgeDocumentDB).where(
            KnowledgeDocumentDB.tenant_id == get_tenant(), KnowledgeDocumentDB.kb_id == kb_id,
            KnowledgeDocumentDB.path == path))).scalar_one_or_none()


async def list_for(kb_id: str, *, after: str | None = None, limit: int | None = None, status: str | None = None,
                   path_prefix: str | None = None) -> list[KnowledgeDocumentDB]:
    """A base's documents by id, paged with `after` (the last id seen)."""
    from sqlalchemy import select

    from core.knowledge.filters_sql import path_prefix_sql
    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    stmt = select(D).where(D.tenant_id == get_tenant(), D.kb_id == kb_id).order_by(D.id)
    if after:
        stmt = stmt.where(D.id > after)
    if status:
        stmt = stmt.where(D.status == status)
    if path_prefix:
        stmt = stmt.where(path_prefix_sql(D.path, path_prefix))
    if limit:
        stmt = stmt.limit(limit)
    async with session() as s:
        return list((await s.execute(stmt)).scalars().all())


# ── writes ─────────────────────────────────────────────────────────────────────

async def upsert_for_path(kb: KnowledgeBase, path: str, *, source: str, sha256: str | None, size: int | None,
                          modified_at: datetime | None = None, mime: str | None = None,
                          meta: Mapping[str, Any] | None = None, source_url: str | None = None,
                          ) -> tuple[KnowledgeDocumentDB, bool]:
    """The document for `path`, queued if its content (or its metadata) changed.

    Returns (row, queued). `queued` is False when nothing changed, and when the
    document is being deleted (its delete job looks at the file again when it
    finishes). A new document takes its source from the path first
    (`source_for`); an existing one keeps its own.
    """
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    stored_meta, title, doc_date = _lift(meta)
    now = _now()
    existing = await get_by_path(kb.id, path)
    if existing is None:
        row = D(id=new_id(DOCUMENT_PREFIX), tenant_id=get_tenant(), kb_id=kb.id, path=path,
                source=source_for(path, kb.folder, source), source_url=source_url, status="queued", attempts=0,
                sha256=sha256, size_bytes=size, mime=mime or mime_for(path), source_modified_at=modified_at,
                version=0, meta=stored_meta or {}, title=title, doc_date=doc_date, language=kb.language,
                queued_at=now, created_at=now, updated_at=now)
        try:
            async with session() as s:
                s.add(row)
            await emit_for(row, "queued")
            return row, True
        except IntegrityError:  # a concurrent write created it first
            existing = await get_by_path(kb.id, path)
            if existing is None:
                raise
    if existing.status == "deleting":
        return existing, False
    same_content = sha256 is not None and existing.sha256 == sha256
    same_extras = ((stored_meta is None or stored_meta == (existing.meta or {}))
                   and (title is None or title == existing.title)
                   and (doc_date is None or doc_date == existing.doc_date)
                   and (source_url is None or source_url == existing.source_url))
    if same_content and same_extras:
        return existing, False
    values: dict[str, Any] = {"status": "queued", "status_reason": None, "error": None, "queued_at": now,
                              "updated_at": now, "sha256": sha256, "size_bytes": size,
                              "mime": mime or existing.mime or mime_for(path)}
    if modified_at is not None:
        values["source_modified_at"] = modified_at
    if stored_meta is not None:
        values["meta"] = stored_meta
    if title is not None:
        values["title"] = title
    if doc_date is not None:
        values["doc_date"] = doc_date
    if source_url is not None:
        values["source_url"] = source_url
    async with session() as s:
        row = (await s.execute(
            update(D).where(D.tenant_id == get_tenant(), D.id == existing.id,
                            D.status.in_(allowed_from("queued") | {"queued"}))
            .values(**values).returning(D).execution_options(synchronize_session=False))).scalar_one_or_none()
    if row is None:  # it went to `deleting` between the read and the write
        return existing, False
    if existing.status != "queued":
        await emit_for(row, "queued", previous=existing.status)
    return row, True


async def transition(doc_id: str, target: str, *, reason: str | None = None, error: str | None = None,
                     previous: str | None = None, **columns: Any) -> KnowledgeDocumentDB | None:
    """Move a document to `target` if its current status allows it; the updated row, or None
    when the race was lost. `columns` are written in the same UPDATE."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    if reason is not None and reason not in REASONS.get(target, ()):
        raise ValueError(f"{reason!r} is not a reason a {target!r} document can carry")
    values = {"status": target, "status_reason": reason, "error": error, "updated_at": _now(), **columns}
    async with session() as s:
        row = (await s.execute(
            update(D).where(D.tenant_id == get_tenant(), D.id == doc_id, D.status.in_(allowed_from(target)))
            .values(**values).returning(D).execution_options(synchronize_session=False))).scalar_one_or_none()
    if row is not None and target in _ANNOUNCED:
        await emit_for(row, target, previous=previous)
    return row


async def tombstone(doc_id: str) -> KnowledgeDocumentDB | None:
    """Mark one document `deleting`."""
    return await transition(doc_id, "deleting")


async def tombstone_under(kb_id: str, prefix: str = "") -> list[KnowledgeDocumentDB]:
    """Mark every document at or under `prefix` (all of the base for "") `deleting`."""
    from sqlalchemy import update

    from core.knowledge.filters_sql import path_prefix_sql
    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    stmt = update(D).where(D.tenant_id == get_tenant(), D.kb_id == kb_id, D.status != "deleting")
    if prefix.strip("/"):
        stmt = stmt.where(path_prefix_sql(D.path, prefix))
    async with session() as s:
        return list((await s.execute(stmt.values(status="deleting", status_reason=None, updated_at=_now())
                                     .returning(D).execution_options(synchronize_session=False))).scalars().all())


async def delete_row(row: KnowledgeDocumentDB) -> bool:
    """Forget a document (its chunks go with it, by the foreign key) and announce it."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    async with session() as s:
        gone = bool((await s.execute(delete(D).where(
            D.tenant_id == get_tenant(), D.id == row.id, D.status == "deleting"))).rowcount)
    if gone:
        await emit_for(row, "deleted")
    return gone


async def delete_all_rows(kb_id: str) -> int:
    """Forget every document of a base. Its chunks go by the foreign key; its snapshots are deleted here."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.store.models import KnowledgeSnapshotDB as S
    from core.tenancy import get_tenant

    tenant = get_tenant()
    async with session() as s:
        n = (await s.execute(delete(D).where(D.tenant_id == tenant, D.kb_id == kb_id))).rowcount
        await s.execute(delete(S).where(S.tenant_id == tenant, S.kb_id == kb_id))
    return int(n or 0)


async def set_columns(doc_id: str, **columns: Any) -> None:
    """Write columns without touching the status (a touched file's new mtime, say)."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    async with session() as s:
        await s.execute(update(D).where(D.tenant_id == get_tenant(), D.id == doc_id).values(**columns))


# ── events and API shapes ──────────────────────────────────────────────────────

async def emit_for(row: KnowledgeDocumentDB, status: str, *, previous: str | None = None) -> None:
    from core.knowledge.events import KnowledgeLifecycleEvent, emit

    report = row.report if isinstance(row.report, dict) else {}
    try:
        event = KnowledgeLifecycleEvent(
            id=new_id(EVENT_PREFIX), type=f"knowledge.document.{status}", created_at=_now().isoformat(),
            kb_id=row.kb_id, doc_id=row.id, path=row.path, status=None if status == "deleted" else status,
            previous_status=previous, status_reason=row.status_reason, version=row.version, sha256=row.sha256,
            chunks=row.chunks, pages=row.pages, warnings_count=len(report.get("warnings") or []) or None,
            est_usd=(report.get("cost") or {}).get("est_usd"), snapshot_id=row.snapshot_id)
    except Exception as exc:  # noqa: BLE001 — an event never fails the change it announces
        _log.warning(f"knowledge event for {row.id} not built: {exc}")
        return
    await emit(event)


def _iso(at: datetime | None) -> str | None:
    if at is None:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat()


def _fields(row: KnowledgeDocumentDB) -> dict[str, Any]:
    return dict(id=row.id, kb_id=row.kb_id, path=row.path, source=row.source, source_url=row.source_url,
                status=row.status, status_reason=row.status_reason, error=row.error, version=row.version or 0,
                sha256=row.sha256, size_bytes=row.size_bytes, mime=row.mime, title=row.title,
                doc_date=row.doc_date, metadata=row.meta or {}, pages=row.pages, chunks=row.chunks,
                tokens=row.tokens, created_at=_iso(row.created_at), updated_at=_iso(row.updated_at),
                indexed_at=_iso(row.indexed_at))


def to_out(row: KnowledgeDocumentDB) -> DocumentOut:
    from core.knowledge.api_models import DocumentOut

    return DocumentOut(**_fields(row))


def to_detail(row: KnowledgeDocumentDB) -> DocumentDetail:
    from core.knowledge.api_models import DocumentDetail
    from core.knowledge.status import IngestionReport

    report = None
    if isinstance(row.report, dict):
        try:
            report = IngestionReport.model_validate(row.report)
        except Exception as exc:  # noqa: BLE001 — a report from an older shape is left out, not a 500
            _log.warning(f"report of {row.id} does not validate: {exc}")
    return DocumentDetail(**_fields(row), report=report)
