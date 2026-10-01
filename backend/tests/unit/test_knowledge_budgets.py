"""A knowledge base's monthly budget (B2b, D167): a real estimate before each embedding, a pause that
waits for a raised cap or a new month — never a loop — and the base-wide events.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import FakeIndexer, enable_knowledge, install_fake_indexing, use_saved_settings, write_vault
from core.knowledge import backfill, bases, budgets, dispatch, documents, events, seams, sync
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.errors import BudgetExceeded, WalletExhausted
from core.knowledge.jobs import run_ingest_job
from core.knowledge.kb_settings import Embedding, KnowledgeBase
from core.knowledge.ledger import CostRow, month_start, record_costs
from core.knowledge.parsed import ParsedDocument
from core.knowledge.pipeline import index_document
from core.knowledge.pipeline_contract import IndexContext
from core.knowledge.snapshots import serving_snapshot
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.knowledge.types import Attribution, IngestJob, Usage
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"
PRICED = "text-embedding-3-small"  # $0.02 per 1M tokens, by its profile


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    yield
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def heard():
    got: list = []
    events.knowledge_observers.append(got.append)
    yield got
    events.knowledge_observers.remove(got.append)


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


def _parsed(name: str = "pdf_contract") -> ParsedDocument:
    return ParsedDocument.model_validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))


async def _paused_since(doc_id: str, at: datetime) -> None:
    """Move a paused document's pause back to `at` (its report's `finished_at`)."""
    row = await documents.get(doc_id)
    report = dict(row.report)
    report["finished_at"] = at.isoformat()
    async with session() as s:
        await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.id == doc_id).values(report=report))


async def _spent(kb_id: str, usd: float) -> None:
    await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p", est_usd=usd),
                                Attribution(kb_id=kb_id))])


# ── estimates ──────────────────────────────────────────────────────────────────

class TestEstimates:
    async def test_a_priced_model_costs_its_tokens_at_its_rate(self):
        texts = ["x" * 4000]  # 1,000 tokens at four characters a token
        assert await budgets.embed_estimate_usd(PRICED, texts) == pytest.approx(1000 * 0.02 / 1_000_000)
        assert await budgets.embed_rate(PRICED) == (0.02, "byok")

    async def test_a_local_or_unpriced_model_is_estimated_at_nothing(self):
        assert await budgets.embed_rate("ollama.nomic-embed-text") == (0.0, "local")
        assert await budgets.embed_estimate_usd("ollama.nomic-embed-text", ["x" * 4000]) == 0.0
        rate, source = await budgets.embed_rate("oaic.house-embedder")
        assert rate is None and source == "byok"
        assert await budgets.embed_estimate_usd("oaic.house-embedder", ["x" * 4000]) == 0.0
        assert await budgets.embed_estimate_usd("", ["x"]) == 0.0
        assert await budgets.embed_estimate_usd(PRICED, []) == 0.0

    async def test_the_operators_rate_wins_over_the_profiles(self):
        from core import usage_tracker

        table = dict(await usage_tracker.get_pricing_table())
        table[PRICED] = {"input_per_1m": 1.0, "output_per_1m": 0.0}
        await usage_tracker.save_pricing_table(table)
        assert (await budgets.embed_rate(PRICED))[0] == 1.0


# ── the estimate is checked before the call ────────────────────────────────────

async def _indexing_document(kb: KnowledgeBase, doc_id: str = "kdoc_" + "1" * 32) -> IndexContext:
    async with session() as s:
        s.add(KnowledgeDocumentDB(id=doc_id, tenant_id="default", kb_id=kb.id, path="contracts/acme.pdf",
                                  source="upload", status="indexing", version=1))
    parsed = _parsed()
    return IndexContext(kb=kb, doc_id=doc_id, doc_version=1, sha256=parsed.source.sha256, path="contracts/acme.pdf",
                        source="upload", parsed=parsed, snapshot_id=await serving_snapshot(kb),
                        attribution=Attribution(kb_id=kb.id, doc_id=doc_id,
                                                session_id=Attribution.ingestion_session(kb.id)))


class TestTheCheckBeforeEmbedding:
    async def test_a_document_that_would_cross_the_cap_pauses_before_any_call(self):
        """Fails against `check_budget(kb, 0.0)`: nothing spent yet, so a zero estimate passes any cap."""
        embedder = HashingEmbedder(dims=16)
        seams.set_knowledge_embedder(embedder)
        kb = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts",
                           embedding=Embedding(model=PRICED), budget_monthly_usd=1e-9)
        with pytest.raises(BudgetExceeded) as caught:
            await index_document(await _indexing_document(kb))
        assert caught.value.needed_usd > 0 and caught.value.spent_usd == 0.0
        assert embedder.calls == []
        async with session() as s:
            assert (await s.execute(select(KnowledgeChunkDB))).scalars().all() == []

    async def test_room_for_the_estimate_lets_it_through(self):
        seams.set_knowledge_embedder(HashingEmbedder(dims=16))
        kb = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts",
                           embedding=Embedding(model=PRICED), budget_monthly_usd=1.0)
        outcome = await index_document(await _indexing_document(kb))
        assert outcome.stages[2].status == "ok"

    async def test_an_unpriced_model_never_blocks(self):
        seams.set_knowledge_embedder(HashingEmbedder(dims=16))
        kb = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts",
                           embedding=Embedding(model="oaic.house-embedder"), budget_monthly_usd=1e-9)
        outcome = await index_document(await _indexing_document(kb))
        assert outcome.stages[2].status == "ok"

    async def test_the_backfill_checks_its_pass_before_embedding(self):
        """Fails against `check_budget(kb, 0.0)`, the same way."""
        kb = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts")
        ctx = await _indexing_document(kb)
        await index_document(ctx)  # no embedder: keyword only
        async with session() as s:
            await s.execute(update(KnowledgeDocumentDB).values(status="indexed"))
        embedder = HashingEmbedder(dims=16)
        seams.set_knowledge_embedder(embedder)
        capped = kb.model_copy(update={"embedding": Embedding(model=PRICED), "budget_monthly_usd": 1e-9})
        assert await backfill.embed_missing(capped, settings={}) == 0
        assert embedder.calls == []
        roomy = capped.model_copy(update={"budget_monthly_usd": 1.0})
        assert await backfill.embed_missing(roomy, settings={}) == 3


# ── pausing and resuming ───────────────────────────────────────────────────────

async def _kb(**kw) -> KnowledgeBase:
    return await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb", **kw))


async def _paused(monkeypatch, kb, name: str, raised: Exception) -> KnowledgeDocumentDB:
    install_fake_indexing(monkeypatch, FakeIndexer(raises=raised))
    write_vault(f"kb/{name}", f"# {name}\n\nSome text.")
    row, _ = await documents.upsert_for_path(kb, f"kb/{name}", source="vault", sha256=None, size=None)
    await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="upsert", reason="vault_write",
                                   path=row.path, doc_id=row.id))
    return await documents.get(row.id)


class TestPauseAndResume:
    async def test_the_first_document_to_pause_announces_the_base(self, monkeypatch, heard):
        kb = await _kb()
        first = await _paused(monkeypatch, kb, "a.md", BudgetExceeded(kb.id, 1.0, 1.0))
        await _paused(monkeypatch, kb, "b.md", BudgetExceeded(kb.id, 1.0, 1.0))
        assert first.status == "paused" and first.status_reason == "budget_cap"
        base = [e for e in heard if e.type == "knowledge.base.paused"]
        assert len(base) == 1 and base[0].kb_id == kb.id and base[0].status_reason == "budget_cap"
        assert len([e for e in heard if e.type == "knowledge.document.paused"]) == 2

    async def test_resume_queues_only_what_the_budget_paused(self, monkeypatch, jobs, heard):
        kb = await _kb()
        capped = await _paused(monkeypatch, kb, "a.md", BudgetExceeded(kb.id, 1.0, 1.0))
        wallet = await _paused(monkeypatch, kb, "b.md", WalletExhausted())
        jobs.jobs.clear()
        assert await budgets.resume(kb, reason="budget_raised") == 1
        assert (await documents.get(capped.id)).status == "queued"
        assert (await documents.get(wallet.id)).status == "paused", "an empty wallet waits for a top-up"
        assert [(j.op, j.reason, j.doc_id) for j in jobs.jobs] == [("upsert", "budget_resume", capped.id)]
        resumed = [e for e in heard if e.type == "knowledge.base.resumed"]
        assert len(resumed) == 1 and resumed[0].status_reason == "budget_raised"

    async def test_nothing_paused_means_nothing_announced(self, heard):
        kb = await _kb()
        assert await budgets.resume(kb, reason="budget_raised") == 0
        assert not [e for e in heard if e.type == "knowledge.base.resumed"]

    async def test_a_new_month_resumes_what_an_earlier_month_paused(self, monkeypatch, jobs):
        kb = await _kb()
        old = await _paused(monkeypatch, kb, "old.md", BudgetExceeded(kb.id, 1.0, 1.0))
        new = await _paused(monkeypatch, kb, "new.md", BudgetExceeded(kb.id, 1.0, 1.0))
        await _paused_since(old.id, month_start() - timedelta(days=2))
        async with session() as s:  # a later column write (a touched mtime) does not move the pause
            await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.id == old.id)
                            .values(updated_at=datetime.now(timezone.utc)))
        jobs.jobs.clear()
        assert await budgets.tend(kb) == 1
        assert (await documents.get(old.id)).status == "queued"
        assert (await documents.get(new.id)).status == "paused", "paused this month: it waits for a raised cap"
        assert await budgets.tend(kb) == 0, "a second pass in the same month finds nothing more"

    async def test_a_month_boundary_resumes_everything_paused_before_it(self, monkeypatch, jobs):
        kb = await _kb()
        row = await _paused(monkeypatch, kb, "a.md", BudgetExceeded(kb.id, 1.0, 1.0))
        next_month = month_start() + timedelta(days=40)
        assert await budgets.tend(kb, now=next_month) == 1
        assert (await documents.get(row.id)).status == "queued"

    async def test_reconcile_tends_the_budget(self, monkeypatch, jobs):
        kb = await _kb()
        row = await _paused(monkeypatch, kb, "a.md", BudgetExceeded(kb.id, 1.0, 1.0))
        await _paused_since(row.id, month_start() - timedelta(days=1))
        drift = await sync.reconcile(kb)
        assert drift.resumed == 1 and (await documents.get(row.id)).status == "queued"

    def test_what_counts_as_more_room(self):
        def kb(cap):
            return KnowledgeBase(id="kb_00000000000a", name="K", folder="k", budget_monthly_usd=cap)

        assert budgets.cap_raised(kb(1.0), kb(2.0))
        assert budgets.cap_raised(kb(1.0), kb(None)), "a cleared cap"
        assert not budgets.cap_raised(kb(2.0), kb(1.0))
        assert not budgets.cap_raised(kb(1.0), kb(1.0))
        assert not budgets.cap_raised(kb(None), kb(5.0)), "there was no cap to pause anything"
        assert not budgets.cap_raised(kb(None), kb(None))


async def test_the_spend_counts_toward_the_check(monkeypatch):
    """A cap already reached pauses even a $0 document (B1a's rule, unchanged)."""
    seams.set_knowledge_embedder(HashingEmbedder(dims=16))
    kb = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts", budget_monthly_usd=0.01)
    await _spent(kb.id, 0.02)
    with pytest.raises(BudgetExceeded):
        await index_document(await _indexing_document(kb))


def test_the_month_starts_at_midnight_utc():
    at = datetime(2026, 10, 17, 15, 4, tzinfo=timezone.utc)
    assert month_start(at) == datetime(2026, 10, 1, tzinfo=timezone.utc)
