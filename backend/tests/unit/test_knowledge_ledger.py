"""The Knowledge cost ledger: every call priced once, retrieval inside a run's cost, a per-KB budget (D153 #8)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from core.knowledge import ledger
from core.knowledge.errors import BudgetExceeded
from core.knowledge.kb_settings import KnowledgeBase
from core.knowledge.types import Attribution, Usage
from core.store import session
from core.store.models import KnowledgeCostDB, UsageLogDB

KB = "kb_00000000000a"


def _usage(stage: str, est: float = 0.01, charged: int | None = None, source: str = "platform") -> Usage:
    return Usage(stage=stage, cost_source=source, model="titan-embed-v2", provider="model_gateway",  # type: ignore[arg-type]
                 input_tokens=100, units=100.0, unit="tokens", est_usd=est, charged_microusd=charged)


async def _rows(model):
    async with session() as s:
        return (await s.execute(select(model))).scalars().all()


async def test_embed_rerank_ocr_parse_are_mirrored_into_usage_logs():
    run_attr = Attribution(kb_id=KB, query_id="kq_1", run_id="run_1", session_id="sess_1", agent_id="agent_1",
                           source="orchestration")
    await ledger.record_costs([ledger.CostRow(_usage(stage), run_attr) for stage in ("embed", "rerank", "ocr", "parse")])
    costs = await _rows(KnowledgeCostDB)
    usage = await _rows(UsageLogDB)
    assert len(costs) == 4 and all(c.usage_logged_by == "ledger" for c in costs)
    assert len(usage) == 4
    assert {u.event_type for u in usage} == {"knowledge"}
    assert {u.tool_name for u in usage} == {"knowledge:embed", "knowledge:rerank", "knowledge:ocr", "knowledge:parse"}
    assert all(u.run_id == "run_1" and u.source == "orchestration" for u in usage)
    assert {u.details["knowledge_cost_id"] for u in usage} == {c.id for c in costs}


async def test_enrich_and_rewrite_are_not_logged_twice():
    await ledger.record_costs([ledger.CostRow(_usage("enrich"), Attribution(kb_id=KB)),
                               ledger.CostRow(_usage("rewrite"), Attribution(kb_id=KB))])
    assert [c.usage_logged_by for c in await _rows(KnowledgeCostDB)] == ["chat", "chat"]
    assert await _rows(UsageLogDB) == []


async def test_a_search_in_a_run_is_part_of_the_runs_cost():
    """The engine's finalizer sums `get_usage_logs(run_id=…)`; retrieval is in that sum with no change to it."""
    from core.usage_tracker import get_usage_logs

    await ledger.record_costs([ledger.CostRow(_usage("embed", est=0.002),
                                              Attribution(kb_id=KB, run_id="run_7", source="orchestration")),
                               ledger.CostRow(_usage("rerank", est=0.002),
                                              Attribution(kb_id=KB, run_id="run_7", source="orchestration"))])
    spent = await get_usage_logs(run_id="run_7", limit=100)
    assert round(sum(float(r.get("estimated_cost") or 0) for r in spent), 6) == 0.004


async def test_ingestion_is_attributed_to_the_knowledge_base():
    await ledger.record_costs([ledger.CostRow(_usage("embed"), Attribution(kb_id=KB, doc_id="kdoc_1"))])
    [row] = await _rows(UsageLogDB)
    assert row.session_id == f"knowledge:{KB}" and row.source == "knowledge"


async def test_a_failure_to_record_never_raises(monkeypatch):
    async def broken(rows):
        raise RuntimeError("database down")

    monkeypatch.setattr(ledger, "_write", broken)
    await ledger.record_costs([ledger.CostRow(_usage("embed"), Attribution(kb_id=KB))])


async def test_month_spend_prefers_the_charge_and_counts_only_this_month_and_kb():
    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    await ledger.record_costs([
        ledger.CostRow(_usage("embed", est=0.50, charged=400_000), Attribution(kb_id=KB)),  # charged $0.40
        ledger.CostRow(_usage("ocr", est=0.25, source="byok"), Attribution(kb_id=KB)),
        ledger.CostRow(_usage("embed", est=9.0), Attribution(kb_id="kb_00000000000b")),
    ])
    async with session() as s:
        s.add(KnowledgeCostDB(tenant_id="default", kb_id=KB, stage="embed", cost_source="platform", unit="tokens",
                              est_usd=5.0, at=datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)))
    assert await ledger.kb_month_spend(KB, now=now) == pytest.approx(0.65)


async def test_the_budget_pauses_before_it_is_crossed():
    kb = KnowledgeBase(id=KB, name="Contracts", folder="contracts", budget_monthly_usd=1.0)
    await ledger.record_costs([ledger.CostRow(_usage("embed", est=0.90), Attribution(kb_id=KB))])
    await ledger.check_budget(kb, 0.05)
    with pytest.raises(BudgetExceeded) as caught:
        await ledger.check_budget(kb, 0.20)
    assert caught.value.code == "budget_cap" and caught.value.cap_usd == 1.0
    await ledger.check_budget(kb.model_copy(update={"budget_monthly_usd": None}), 1000.0)
