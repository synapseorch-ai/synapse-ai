# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's cost ledger — one row per priced call — and the per-KB monthly budget.

**Two ledgers, one rule** (Naveen, 2026-09-30): `usage_logs` records every
priced model call exactly once; `knowledge_costs` records every Knowledge cost
exactly once.

* A stage whose model call the chat path already logs — `enrich` and
  `rewrite` go through `generate_response` — gets its `knowledge_costs` row
  only (`usage_logged_by = "chat"`).
* Every other stage — `embed`, `rerank`, `ocr`, `parse` — is also written to
  `usage_logs` here, in the same transaction (`usage_logged_by = "ledger"`,
  `event_type = "knowledge"`), with the call's run and session ids. That is
  the whole mechanism by which retrieval shows up in a run's cost (the engine's
  finalizer and the fleet's sum `usage_logs` by run) and on both products'
  Usage screens: no finalizer changes.
* **A call a metering gateway charged (`cost_source = "platform"`) is in the run
  as what it settled, not as an estimate** (CCR-7, F330): its mirror's
  `estimated_cost` is 0, as for every `synapse.*` chat call, and its estimate
  and charge sit in `details`. Cloud adds the gateway's settles to the run's
  estimate, so an estimate here counted the same call twice.

An ingestion is attributed to `session_id = "knowledge:<kb_id>"` and source
"knowledge"; a search to the run or chat turn that made it.

**Money units.** `est_usd` is always there (the pricing table's estimate, 0 when
unpriced). `charged_microusd` is what a wallet actually settled — cloud's
gateway reports it — and wins over the estimate wherever both exist. BYOK and
OSS rows never have it.

**Never raises into the pipeline.** A cost row that cannot be written is
logged, not allowed to fail an ingestion or a search.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.knowledge.errors import BudgetExceeded
from core.knowledge.types import Attribution, Usage
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

#: Stages whose model call is already in `usage_logs` from the chat path.
CHAT_LOGGED_STAGES: frozenset[str] = frozenset({"enrich", "rewrite"})
USAGE_EVENT_TYPE = "knowledge"


@dataclass(frozen=True)
class CostRow:
    usage: Usage
    attribution: Attribution
    details: dict | None = None


def usage_logged_by(stage: str) -> str:
    return "chat" if stage in CHAT_LOGGED_STAGES else "ledger"


async def record_costs(rows: Sequence[CostRow]) -> None:
    """Write each row to `knowledge_costs` and, where the chat path did not, to `usage_logs`."""
    if not rows:
        return
    try:
        await _write(rows)
    except Exception as exc:  # noqa: BLE001 — a cost row never fails the work it priced
        _log.warning(f"knowledge cost rows not recorded ({len(rows)}): {exc}")


async def _write(rows: Sequence[CostRow]) -> None:
    from core.store import session
    from core.store.models import KnowledgeCostDB, UsageLogDB
    from core.tenancy import get_tenant

    tenant = get_tenant()
    now = datetime.now(timezone.utc)
    async with session() as s:
        for row in rows:
            u, a = row.usage, row.attribution
            logged_by = usage_logged_by(u.stage)
            cost = KnowledgeCostDB(
                tenant_id=tenant, at=now, kb_id=a.kb_id, doc_id=a.doc_id, query_id=a.query_id,
                snapshot_id=a.snapshot_id, run_id=a.run_id, session_id=a.session_id,
                stage=u.stage, cost_source=u.cost_source, model=u.model or "", provider=u.provider,
                input_tokens=u.input_tokens, output_tokens=u.output_tokens, units=u.units, unit=u.unit,
                est_usd=u.est_usd, charged_microusd=u.charged_microusd, priced=u.priced,
                usage_logged_by=logged_by, latency_ms=u.latency_ms, request_id=u.request_id,
                details=row.details,
            )
            s.add(cost)
            if logged_by != "ledger":
                continue
            await s.flush()
            s.add(UsageLogDB(
                tenant_id=tenant, timestamp=now, event_type=USAGE_EVENT_TYPE, source=a.source or "knowledge",
                model=u.model or "", provider=u.provider,
                session_id=a.session_id or Attribution.ingestion_session(a.kb_id),
                agent_id=a.agent_id or "unknown", run_id=a.run_id, tool_name=f"knowledge:{u.stage}",
                input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                total_tokens=u.input_tokens + u.output_tokens, context_chars=0,
                estimated_cost=0.0 if u.cost_source == "platform" else u.est_usd,
                latency_seconds=round(u.latency_ms / 1000.0, 2),
                cache_read_tokens=0, cache_write_tokens=0, estimated_savings=0.0, response_cache_hit=False,
                details={"stage": u.stage, "kb_id": a.kb_id, "units": u.units, "unit": u.unit,
                         "cost_source": u.cost_source, "knowledge_cost_id": cost.id,
                         "est_usd": u.est_usd, "charged_microusd": u.charged_microusd},
            ))


def month_start(now: datetime | None = None) -> datetime:
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def kb_month_spend(kb_id: str, *, now: datetime | None = None) -> float:
    """What a knowledge base has cost this UTC month, in USD, every cost source included.

    A settled wallet charge counts at what was charged, anything else at its
    estimate (BYOK counts at its estimate: a budget is about the customer's
    money, whichever account pays it; a local model is $0).
    """
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import KnowledgeCostDB
    from core.tenancy import get_tenant

    amount = func.coalesce(KnowledgeCostDB.charged_microusd / 1_000_000.0, KnowledgeCostDB.est_usd)
    stmt = select(func.coalesce(func.sum(amount), 0.0)).where(
        KnowledgeCostDB.tenant_id == get_tenant(),
        KnowledgeCostDB.kb_id == kb_id,
        KnowledgeCostDB.at >= month_start(now),
    )
    async with session() as s:
        return float((await s.execute(stmt)).scalar_one() or 0.0)


async def check_budget(kb: KnowledgeBase, estimate_usd: float, *, now: datetime | None = None) -> None:
    """Raise `BudgetExceeded` if `estimate_usd` more would take `kb` past its monthly cap.

    Checked before enrich, embed and OCR with the cost preview's estimate. The
    caller pauses the document (`paused`, reason `budget_cap`); nothing retries it.
    """
    cap = kb.budget_monthly_usd
    if cap is None:
        return
    spent = await kb_month_spend(kb.id, now=now)
    if spent + max(0.0, estimate_usd) > cap:
        raise BudgetExceeded(kb.id, spent, cap, estimate_usd)
