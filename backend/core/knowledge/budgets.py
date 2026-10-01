"""A knowledge base's monthly budget: what a call will cost before it is made, the pause at the cap,
and the resume (B2b, D167).

`ledger.check_budget(kb, estimate)` is the frozen check; this module gives it a real estimate and
decides what happens around it:

* **Before embedding**, the pipeline and the re-embed backfill pass the call's estimate:
  `embed_estimate_usd` — the texts' tokens at the model's rate (the operator's `model_pricing` entry,
  else the model's profile). A model on the customer's own machine is $0, and an unpriced model is
  estimated at $0: a budget never blocks what it cannot price. OCR passes its own estimate (A2).
* **A pause is never a loop.** A document that meets the cap rests `paused` / `budget_cap`
  (`jobs.failure_status`); it is queued again only when the cap is raised or cleared (the PATCH calls
  `resume`) or when a new month begins (`tend`, at the end of each reconcile pass: OSS's loop, cloud's
  job). `wallet_402` pauses wait for cloud's top-up, not for this module.
* **Events:** `knowledge.base.paused` when a knowledge base's first document pauses on its budget,
  `knowledge.base.resumed` when paused work is queued again.

Imports are function-local, as in the rest of the package: cloud's API process imports Knowledge.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

#: The pause a budget causes; `wallet_402` is cloud's and resumes on a top-up.
BUDGET_REASON = "budget_cap"


# ── estimates ──────────────────────────────────────────────────────────────────

async def embed_rate(model: str) -> tuple[float | None, str]:
    """`model`'s rate in USD per 1M input tokens, and who pays (`platform`, `byok` or `local`).

    A local model's rate is 0.0; a model nothing prices is None. No call is made: an unknown model's
    profile is not probed here."""
    from core.knowledge import embedders, profiles
    from core.providers.registry import provider_for

    if not model:
        return None, "local"
    source = embedders.cost_source(provider_for(model).id)
    if source == "local":
        return 0.0, source
    return await embedders.usd_per_mtok(model, profiles.profile_for(model) or profiles.UNKNOWN), source


async def embed_estimate_usd(model: str, texts: Sequence[str]) -> float:
    """What embedding `texts` with `model` is expected to cost, in USD (0.0 when unpriced or local)."""
    from core.knowledge.profiles import estimate_tokens

    if not texts:
        return 0.0
    rate, _source = await embed_rate(model)
    return estimate_tokens(texts) * (rate or 0.0) / 1_000_000


async def capped(kbs: Sequence[KnowledgeBase], *, query: str, models: dict[str, str | None],
                 rerank_usd: float = 0.0) -> set[str]:
    """The knowledge bases a search may not spend on: their cap would be crossed by embedding `query` with
    their snapshot's model (`models`: kb_id → model) plus a rerank they might pay for (Naveen, D167 #3).
    Bases without a cap are never checked (no query is made for them)."""
    from core.knowledge.errors import BudgetExceeded
    from core.knowledge.ledger import check_budget

    out: set[str] = set()
    for kb in kbs:
        if kb.budget_monthly_usd is None:
            continue
        estimate = await embed_estimate_usd(models.get(kb.id) or "", [query]) + max(0.0, rerank_usd)
        try:
            await check_budget(kb, estimate)
        except BudgetExceeded:
            out.add(kb.id)
    return out


# ── pausing and resuming ───────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def paused_at(row) -> datetime | None:
    """When a paused document came to rest: its report's `finished_at`, written in the same UPDATE as the
    pause. Not `updated_at`, which any later column write (reconcile noting a new mtime) moves."""
    report = row.report if isinstance(row.report, dict) else {}
    try:
        at = datetime.fromisoformat(str(report.get("finished_at")))
    except ValueError:
        return _aware(row.updated_at)
    return _aware(at)


async def announce(event_type: str, kb_id: str, *, status: str | None = None, status_reason: str | None = None,
                   snapshot_id: str | None = None, est_usd: float | None = None) -> None:
    """A knowledge-base-wide lifecycle event (`knowledge.base.*`, `knowledge.snapshot.failed`). Never raises."""
    from core.knowledge.events import KnowledgeLifecycleEvent, emit
    from core.knowledge.ids import EVENT_PREFIX, new_id

    try:
        event = KnowledgeLifecycleEvent(id=new_id(EVENT_PREFIX), type=event_type, created_at=_now().isoformat(),
                                        kb_id=kb_id, status=status, status_reason=status_reason,
                                        snapshot_id=snapshot_id, est_usd=est_usd)
    except Exception as exc:  # noqa: BLE001 — an event never fails the change it announces
        _log.warning(f"knowledge event {event_type} for {kb_id} not built: {exc}")
        return
    await emit(event)


async def on_document_paused(kb_id: str, reason: str | None) -> None:
    """A document of `kb_id` just paused. The knowledge base's first pause is announced as the base's."""
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D
    from core.tenancy import get_tenant

    try:
        async with session() as s:
            paused = (await s.execute(select(func.count()).where(
                D.tenant_id == get_tenant(), D.kb_id == kb_id, D.status == "paused"))).scalar_one()
    except Exception as exc:  # noqa: BLE001 — a missed announcement never fails the document
        _log.warning(f"knowledge base {kb_id}: pause not announced: {exc}")
        return
    if paused == 1:
        await announce("knowledge.base.paused", kb_id, status="paused", status_reason=reason)


def cap_raised(before: KnowledgeBase, after: KnowledgeBase) -> bool:
    """Whether an edit gave the budget more room: a cap cleared, or raised."""
    old, new = before.budget_monthly_usd, after.budget_monthly_usd
    if old is None:
        return False
    return new is None or new > old


async def resume(kb: KnowledgeBase, *, reason: str, paused_before: datetime | None = None) -> int:
    """Queue `kb`'s documents paused on its budget again (only those paused before `paused_before`,
    when given), and a rebuild waiting for budget. Returns how many documents were queued."""
    from core.knowledge import dispatch, documents

    queued = 0
    for row in await documents.list_for(kb.id, status="paused"):
        if row.status_reason != BUDGET_REASON:
            continue
        if paused_before is not None and (paused_at(row) or paused_before) >= paused_before:
            continue
        again = await documents.transition(row.id, "queued", previous="paused", queued_at=_now())
        if again is not None:
            await dispatch.enqueue(kb.id, "upsert", "budget_resume", path=again.path, doc_id=again.id)
            queued += 1
    from core.knowledge.snapshots import resume_build

    rebuilt = await resume_build(kb, reasons=(BUDGET_REASON,), paused_before=paused_before)
    if queued or rebuilt:
        _log.info(f"knowledge base {kb.id}: {queued} paused document(s) queued again"
                  f"{' and its rebuild resumed' if rebuilt else ''} ({reason})")
        await announce("knowledge.base.resumed", kb.id, status="queued", status_reason=reason)
    return queued


async def tend(kb: KnowledgeBase, *, now: datetime | None = None) -> int:
    """At a month's start, queue again what this knowledge base's budget paused in an earlier month.
    Called at the end of each reconcile pass; never raises."""
    from core.knowledge.ledger import month_start

    try:
        if kb.status != "active":
            return 0
        return await resume(kb, reason="month_start", paused_before=month_start(now))
    except Exception as exc:  # noqa: BLE001 — reconcile goes on
        _log.warning(f"knowledge base {kb.id}: budget tending failed: {exc}")
        return 0
