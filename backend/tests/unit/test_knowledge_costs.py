"""What a knowledge base costs (B2b, D167): the preview of a rebuild under a draft of its settings, the spend
grouped by day, stage, document or model, and the check that refuses an impossible model choice.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from core.knowledge import bases, costs, dispatch, documents, seams
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.jobs import run_ingest_job
from core.knowledge.kb_settings import Embedding, Rerank
from core.knowledge.ledger import CostRow, month_start, record_costs
from core.knowledge.testing.fakes import FakeOcr, RecordingDispatcher
from core.knowledge.types import Attribution, Usage
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeCostDB, KnowledgeDocumentDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

PRICED = "text-embedding-3-small"  # $0.02 per 1M tokens


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    from core import usage_tracker

    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    monkeypatch.setattr(usage_tracker, "_pricing_snapshot", {})  # no operator rates: the profiles' prices
    yield
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


TEXT = ("# Refunds\n\n" + "A refund is paid within thirty days of the request. " * 40 + "\n\n"
        "# Hosting\n\n" + "The hosting fee is billed monthly in advance. " * 40)


async def _indexed(jobs, n: int = 2, **kw):
    """A base with `n` documents indexed keyword-only by B1's real pipeline."""
    kb = await bases.create_base(KnowledgeBaseCreate(name="Policies", folder="kb", **kw))
    for i in range(n):
        write_vault(f"kb/doc{i}.md", TEXT)
        row, _ = await documents.upsert_for_path(kb, f"kb/doc{i}.md", source="vault", sha256=None, size=None)
        await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
    await jobs.drain(run_ingest_job)
    assert {r.status for r in await documents.list_for(kb.id)} == {"indexed"}
    return kb


async def _chunk_tokens(kb_id: str) -> tuple[int, int, int]:
    async with session() as s:
        rows = (await s.execute(select(KnowledgeChunkDB.tokens, KnowledgeChunkDB.context).where(
            KnowledgeChunkDB.kb_id == kb_id))).all()
    return len(rows), sum(t for t, _ in rows), sum(len(c) for _, c in rows) // 4


def _stage(preview, name):
    return next((s for s in preview.stages if s.stage == name), None)


# ── the preview ────────────────────────────────────────────────────────────────

class TestThePreview:
    async def test_a_new_model_is_priced_by_the_chunks_it_would_embed(self, jobs):
        kb = await _indexed(jobs)
        n, content, prefix = await _chunk_tokens(kb.id)
        preview = await costs.preview(kb, {"embedding": {"model": PRICED}})
        assert (preview.docs, preview.pages) == (2, 0)
        embed = _stage(preview, "embed")
        assert embed.model == PRICED and embed.unit == "tokens" and embed.cost_source == "byok"
        assert embed.units == pytest.approx(content + prefix, abs=n)
        assert embed.est_usd == pytest.approx(embed.units * 0.02 / 1_000_000)
        assert preview.total_est_usd == embed.est_usd and preview.budget_remaining_usd is None

    async def test_no_model_means_keyword_only_and_nothing_to_price(self, jobs):
        kb = await _indexed(jobs)
        preview = await costs.preview(kb, None)
        assert preview.stages == [] and preview.total_est_usd == 0.0 and preview.docs == 2

    async def test_a_local_model_costs_nothing_but_still_counts_its_tokens(self, jobs):
        kb = await _indexed(jobs)
        embed = _stage(await costs.preview(kb, {"embedding": {"model": "ollama.nomic-embed-text"}}), "embed")
        assert embed.cost_source == "local" and embed.est_usd == 0.0 and embed.units > 0

    async def test_more_overlap_and_smaller_chunks_cost_more_tokens(self, jobs):
        kb = await _indexed(jobs, embedding=Embedding(model=PRICED))
        same = _stage(await costs.preview(kb, None), "embed")
        smaller = _stage(await costs.preview(kb, {"chunking": {"target_tokens": 100, "max_tokens": 200,
                                                               "overlap_pct": 0.2}}), "embed")
        assert smaller.units > same.units * 1.15, "20% overlap, and a prefix on more chunks"

    async def test_contextual_retrieval_is_priced_one_call_per_chunk(self, jobs, monkeypatch):
        from core import usage_tracker

        monkeypatch.setattr(usage_tracker, "_pricing_snapshot", {  # the process's rate card, never the store's
            "claude-haiku-4-5": {"provider": "anthropic", "input_per_1m": 1.0, "output_per_1m": 5.0}})
        kb = await _indexed(jobs)
        n, content, _ = await _chunk_tokens(kb.id)
        preview = await costs.preview(kb, {"enrichment": {"mode": "contextual", "contextual_model": "claude-haiku-4-5"}})
        enrich = _stage(preview, "enrich")
        assert enrich.unit == "requests" and enrich.units == n and enrich.model == "claude-haiku-4-5"
        assert enrich.cost_source == "byok" and enrich.est_usd > 0
        async with session() as s:  # each call re-reading its whole document at the full input rate
            per_doc = (await s.execute(select(func.count(), func.sum(KnowledgeChunkDB.tokens)).where(
                KnowledgeChunkDB.kb_id == kb.id).group_by(KnowledgeChunkDB.doc_id))).all()
        uncached = sum(k * (t + t / k + 100) * 1.0 + k * 75 * 5.0 for k, t in per_doc) / 1_000_000
        assert enrich.est_usd < uncached, "the document is read from the cache after the first call"

    async def test_pages_no_ocr_has_read_are_priced_at_the_providers_rate(self, jobs):
        kb = await _indexed(jobs)
        first = sorted(r.id for r in await documents.list_for(kb.id))[0]
        row = await documents.get(first)
        report = dict(row.report)
        report["pages"] = {**report.get("pages", {}), "total": 5, "no_text_layer": 3, "ocr_applied": 1}
        async with session() as s:
            await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.id == first)
                            .values(report=report, pages=5))
        assert _stage(await costs.preview(kb, None), "ocr") is None, "no OCR provider: nothing to price"
        ocr = FakeOcr({})
        ocr.price_per_page_usd = 0.0015
        seams.set_ocr_provider(ocr)
        preview = await costs.preview(kb, None)
        assert _stage(preview, "ocr").units == 2 and _stage(preview, "ocr").est_usd == pytest.approx(0.003)
        assert preview.pages == 5

    async def test_the_budget_left_is_the_drafts_cap_less_this_months_spend(self, jobs):
        kb = await _indexed(jobs, budget_monthly_usd=1.0)
        await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p", est_usd=0.25),
                                    Attribution(kb_id=kb.id))])
        assert (await costs.preview(kb, None)).budget_remaining_usd == pytest.approx(0.75)
        assert (await costs.preview(kb, {"budget_monthly_usd": 2.0})).budget_remaining_usd == pytest.approx(1.75)
        assert (await costs.preview(kb, {"budget_monthly_usd": 0.1})).budget_remaining_usd == 0.0

    async def test_a_draft_cannot_change_what_a_preview_does_not_price(self, jobs):
        kb = await _indexed(jobs)
        with pytest.raises(ValueError, match="folder"):
            await costs.preview(kb, {"folder": "elsewhere"})
        with pytest.raises(ValueError):
            await costs.preview(kb, {"chunking": {"target_tokens": 1}})

    async def test_a_preview_starts_nothing(self, jobs):
        kb = await _indexed(jobs)
        jobs.jobs.clear()
        await costs.preview(kb, {"embedding": {"model": PRICED}})
        assert jobs.jobs == []
        async with session() as s:
            assert (await s.execute(select(KnowledgeCostDB))).all() == []


class TestTheDeploymentsRate:
    """F334 (W1, D179): a model our gateway serves is quoted at what the deployment charges (cloud: its catalogue ×
    the org's plan), never at the list price the gateway marks up."""

    @pytest.fixture(autouse=True)
    def _pricer(self):
        from core.knowledge.costs import Rate

        rates = {"synapse.titan-embed-v2": Rate(input_per_mtok=0.025),
                 "synapse.nova-micro": Rate(input_per_mtok=0.04375, output_per_mtok=0.175),
                 "textract-detect-text-v1": Rate(per_unit=0.001875, unit="pages")}

        async def pricer(model):
            return rates.get(model)

        costs.set_platform_pricer(pricer)
        yield
        costs.set_platform_pricer(None)

    async def test_a_platform_embedding_model_is_previewed_at_the_charge(self, jobs):
        kb = await _indexed(jobs)
        embed = _stage(await costs.preview(kb, {"embedding": {"model": "synapse.titan-embed-v2"}}), "embed")
        assert embed.cost_source == "platform" and embed.units > 0
        assert embed.est_usd == pytest.approx(embed.units * 0.025 / 1_000_000), "list × 1.25, not the $0.02 list"
        costs.set_platform_pricer(None)
        listed = _stage(await costs.preview(kb, {"embedding": {"model": "synapse.titan-embed-v2"}}), "embed")
        assert listed.est_usd == pytest.approx(listed.units * 0.02 / 1_000_000), "no pricer: the list price"

    async def test_a_platform_ocr_page_is_previewed_at_the_charge(self, jobs):
        kb = await _indexed(jobs)
        first = sorted(r.id for r in await documents.list_for(kb.id))[0]
        report = dict((await documents.get(first)).report)
        report["pages"] = {**report.get("pages", {}), "total": 5, "no_text_layer": 3, "ocr_applied": 1}
        async with session() as s:
            await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.id == first)
                            .values(report=report, pages=5))
        ocr = FakeOcr({}, engine="textract-detect-text-v1")
        ocr.price_per_page_usd, ocr.cost_source = 0.0015, "platform"
        seams.set_ocr_provider(ocr)
        stage = _stage(await costs.preview(kb, None), "ocr")
        assert (stage.units, stage.cost_source) == (2, "platform")
        assert stage.est_usd == pytest.approx(2 * 0.001875)

    async def test_contextual_retrieval_on_a_platform_model_is_priced_not_free(self, jobs):
        kb = await _indexed(jobs)
        draft = {"enrichment": {"mode": "contextual", "contextual_model": "synapse.nova-micro"}}
        enrich = _stage(await costs.preview(kb, draft), "enrich")
        assert enrich.cost_source == "platform" and enrich.est_usd > 0
        costs.set_platform_pricer(None)
        assert _stage(await costs.preview(kb, draft), "enrich").est_usd == 0.0, "no rate card prices it"


# ── the settings check ─────────────────────────────────────────────────────────

class TestTheSettingsCheck:
    def test_a_width_the_model_cannot_answer_is_refused(self):
        with pytest.raises(ValueError, match="text-embedding-3-small"):
            costs.check_settings(embedding=Embedding(model=PRICED, dims=4096))
        costs.check_settings(embedding=Embedding(model=PRICED, dims=512))  # a Matryoshka width it offers
        costs.check_settings(embedding=Embedding(model="oaic.house-embedder", dims=4096))  # unknown: its probe decides

    def test_a_rerank_model_without_a_provider_is_refused(self):
        with pytest.raises(ValueError, match="cohere."):
            costs.check_settings(rerank=Rerank(model="rerank-v3.5"))
        for model in ("cohere.rerank-v3.5", "voyage.rerank-2.5", "jina.jina-reranker-v2", "tei.bge", "synapse.x"):
            costs.check_settings(rerank=Rerank(model=model))
        costs.check_settings(rerank=Rerank())


# ── the spend ──────────────────────────────────────────────────────────────────

async def _row(kb_id, *, at, stage="embed", model="m", doc_id=None, query_id=None, est=0.01, charged=None,
               tokens=100, units=0.0):
    async with session() as s:
        s.add(KnowledgeCostDB(tenant_id="default", kb_id=kb_id, at=at, stage=stage, cost_source="byok", model=model,
                              doc_id=doc_id, query_id=query_id, est_usd=est, charged_microusd=charged,
                              input_tokens=tokens, units=units))


class TestTheSpend:
    async def test_grouped_by_day_stage_document_and_model(self):
        start = month_start()
        day1, day2 = start + timedelta(hours=1), start + timedelta(days=1, hours=23, minutes=30)
        await _row("kb_a", at=day1, doc_id="kdoc_1", est=0.02, tokens=100)
        await _row("kb_a", at=day1, doc_id="kdoc_2", est=0.03, tokens=200, charged=30_000)
        await _row("kb_a", at=day2, stage="rerank", model="cohere.rerank-v3.5", query_id="kq_1", est=0.002,
                   tokens=0, units=1.0)
        await _row("kb_b", at=day1, est=9.0)  # another base

        by_day = await costs.report("kb_a", group_by="day", until=start + timedelta(days=3))
        assert [(g.key, round(g.est_usd, 4), g.charged_microusd, g.input_tokens) for g in by_day] == [
            (day1.date().isoformat(), 0.05, 30_000, 300), (day2.date().isoformat(), 0.002, None, 0)]
        by_stage = {g.key: g for g in await costs.report("kb_a", group_by="stage", until=start + timedelta(days=3))}
        assert set(by_stage) == {"embed", "rerank"} and by_stage["rerank"].units == 1.0
        assert by_stage["embed"].charged_microusd == 30_000 and by_stage["rerank"].charged_microusd is None
        by_doc = [g.key for g in await costs.report("kb_a", group_by="document", until=start + timedelta(days=3))]
        assert by_doc == ["kdoc_2", "kdoc_1", "search"], "most spent first; what searches spent is 'search'"
        by_model = [g.key for g in await costs.report("kb_a", group_by="model", until=start + timedelta(days=3))]
        assert by_model == ["m", "cohere.rerank-v3.5"]

    async def test_the_window_defaults_to_this_month(self):
        await _row("kb_a", at=month_start() - timedelta(days=2), est=5.0)
        await _row("kb_a", at=datetime.now(timezone.utc) - timedelta(seconds=5), est=0.01)
        assert [round(g.est_usd, 4) for g in await costs.report("kb_a", group_by="stage")] == [0.01]
        earlier = await costs.report("kb_a", group_by="stage", since=month_start() - timedelta(days=10))
        assert [round(g.est_usd, 4) for g in earlier] == [5.01]

    async def test_a_day_is_a_utc_day(self):
        start = month_start()
        await _row("kb_a", at=start + timedelta(hours=23, minutes=59))
        await _row("kb_a", at=start + timedelta(days=1, minutes=1))
        groups = await costs.report("kb_a", group_by="day", until=start + timedelta(days=2))
        assert [g.key for g in groups] == [start.date().isoformat(), (start + timedelta(days=1)).date().isoformat()]

    async def test_an_unknown_grouping_is_refused(self):
        with pytest.raises(ValueError, match="group_by"):
            await costs.report("kb_a", group_by="week")
