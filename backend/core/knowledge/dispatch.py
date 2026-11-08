"""Handing ingestion work to whatever runs it — never failing the caller, never piling up.

`enqueue` builds an `IngestJob` (the tenant goes with it, because a queued job
runs where no ContextVar survives) and gives it to the installed dispatcher:
OSS's in-process one by default, ARQ in cloud.

**A burst of saves is one job.** An editor saving a file five times a second
would otherwise start five ingestions. While a job for a document is waiting
to start in this process, another request for the same work (`dedup_key`) is
dropped; the waiting job reads the file when it starts, so it sees the newest
content. The key is released the moment the job starts, so a write during
parsing queues one more — which then finds the document mid-flight, or reads
the newer file. A queue (ARQ) de-duplicates by job id itself, so only the
in-process mode keeps keys here. A key older than `STALE_AFTER_S` is ignored:
a job that was never started must not block its document forever.

Nothing here raises: a Vault write must not fail because indexing could not be
scheduled, and reconcile re-queues whatever was missed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from core.knowledge.types import DispatchReceipt, IngestJob, IngestOp, IngestReason
from core.log import get_logger

_log = get_logger(__name__)

STALE_AFTER_S = 600.0

_pending: dict[str, float] = {}


def is_pending(job: IngestJob) -> bool:
    at = _pending.get(job.dedup_key())
    return at is not None and time.monotonic() - at < STALE_AFTER_S


def started(job: IngestJob) -> None:
    """Called by the job as it starts: later work for the same document is queued again."""
    _pending.pop(job.dedup_key(), None)


def reset() -> None:
    """Forget every pending key (tests)."""
    _pending.clear()


def job_for(kb_id: str, op: IngestOp, reason: IngestReason, *, path: str | None = None, doc_id: str | None = None,
            snapshot_id: str | None = None) -> IngestJob:
    from core.tenancy import get_tenant

    return IngestJob(tenant=get_tenant(), kb_id=kb_id, op=op, reason=reason, path=path, doc_id=doc_id,
                     snapshot_id=snapshot_id, requested_at=datetime.now(timezone.utc).isoformat())


async def enqueue(kb_id: str, op: IngestOp, reason: IngestReason, *, path: str | None = None,
                  doc_id: str | None = None, snapshot_id: str | None = None) -> DispatchReceipt | None:
    """Schedule the work. Returns the dispatcher's receipt, or None when it was coalesced
    into a job already waiting or could not be scheduled (logged; reconcile repairs it)."""
    from core.knowledge.seams import get_ingest_dispatcher

    job = job_for(kb_id, op, reason, path=path, doc_id=doc_id, snapshot_id=snapshot_id)
    if is_pending(job):
        return None
    key = job.dedup_key()
    _pending[key] = time.monotonic()
    try:
        receipt = await get_ingest_dispatcher().dispatch(job)
    except Exception as exc:  # noqa: BLE001 — a dispatcher is meant never to raise; this one did
        _pending.pop(key, None)
        _log.warning(f"knowledge job {key} not scheduled: {exc}")
        return None
    if receipt.mode != "background":
        _pending.pop(key, None)
    return receipt
