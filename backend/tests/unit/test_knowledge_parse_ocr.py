"""OCR through the seam (A2, D162): only the pages with no text layer are sent, their text lands
on its pages, every call is a cost row, a failure is reported and never silent, a spent
budget or an empty wallet pauses, and a document read before OCR existed is read again
once it does.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import select

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import FakeIndexer, enable_knowledge, install_fake_indexing, use_saved_settings, write_vault
from _fakes.knowledge_formats import PdfPage, pdf, text_page
from core.knowledge import bases, dispatch, documents, seams, sync
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.errors import WalletExhausted
from core.knowledge.jobs import run_ingest_job
from core.knowledge.parse import limits
from core.knowledge.parse.ocr import png_size
from core.knowledge.parse.service import parse_document
from core.knowledge.pipeline import partial_reason
from core.knowledge.testing.fakes import FakeOcr, RecordingDispatcher
from core.knowledge.types import IngestJob
from core.store import session
from core.store.models import KnowledgeCostDB

pytestmark = pytest.mark.usefixtures("knowledge_store")

SCANNED = pdf([text_page("Typed page."), PdfPage().image(), PdfPage().image()])


class Recording:
    """An OCR provider that records which pages each call was sent, and can fail on demand."""

    def __init__(self, inner, *, fail_on_call: int | None = None, error: BaseException | None = None,
                 delay: float = 0.0):
        self.inner, self.calls, self.fail_on_call, self.error, self.delay = inner, [], fail_on_call, error, delay
        self.max_pages_per_call = inner.max_pages_per_call
        self.engine = inner.engine
        self.price_per_page_usd = 0.0015

    async def ocr(self, pages, **kw):
        self.calls.append([p.page for p in pages])
        assert all(p.png.startswith(b"\x89PNG") and p.width == 1700 and p.dpi == limits.OCR_DPI for p in pages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise self.error
        return await self.inner.ocr(pages, **kw)


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    yield
    seams.set_ocr_provider(None)
    dispatch.reset()


def _provider(pages: dict[int, str], **kw) -> Recording:
    per_call = kw.pop("per_call", 10)
    provider = Recording(FakeOcr(pages, max_pages_per_call=per_call), **kw)
    seams.set_ocr_provider(provider)
    return provider


async def _parse(data: bytes = SCANNED, **kw):
    return await parse_document(data, path="kb/scan.pdf", source="vault", sha256=hashlib.sha256(data).hexdigest(),
                                mime="application/pdf", **kw)


async def _ingest(path: str, data: bytes, monkeypatch, *, real: bool = False, **kb_fields):
    """`real`: B1's own pipeline decides indexed / partial (the fake indexer always says indexed)."""
    if not real:
        install_fake_indexing(monkeypatch, FakeIndexer())
    kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb", **kb_fields))
    write_vault(path, data)
    row, _ = await documents.upsert_for_path(kb, path, source="vault", sha256=None, size=None)
    report = await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="upsert", reason="vault_write",
                                            path=path, doc_id=row.id))
    return kb, report, await documents.get(row.id)


class TestTheGate:
    async def test_only_pages_without_a_text_layer_are_sent_and_their_text_lands_on_them(self):
        provider = _provider({2: "Scanned invoice 2025-117.", 3: "Signed by both parties."})
        result = await _parse()
        assert provider.calls == [[2, 3]]
        doc = result.parsed
        assert [(b.page, b.text) for b in doc.blocks] == [
            (1, "Typed page."), (2, "Scanned invoice 2025-117."), (3, "Signed by both parties.")]
        assert doc.stats.pages_ocr == 2 and doc.warnings == [] and partial_reason(doc) is None
        assert all(p.quality.ocr_engine == "fake-ocr" for p in doc.pages[1:])
        assert result.ocr_stage.status == "ok" and result.ocr_stage.detail == {
            "engine": "fake-ocr", "pages": 2, "read": 2, "failed": 0}
        assert [u.units for u in result.usage] == [2.0]

    async def test_without_a_provider_nothing_is_rendered_or_sent(self):
        result = await _parse()
        assert result.ocr_stage is None and partial_reason(result.parsed) == "no_text_layer"

    async def test_a_document_with_a_text_layer_sends_nothing(self):
        provider = _provider({})
        result = await _parse(pdf([text_page("All typed.")]))
        assert provider.calls == [] and result.ocr_stage is None

    def test_a_png_size_is_read_from_its_header(self):
        assert png_size(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x06\xa4\x00\x00\x08\x98") == (1700, 2200)
        assert png_size(b"not a png") == (0, 0)


class TestFailuresAreReported:
    async def test_a_failed_batch_leaves_its_pages_unread_and_says_so(self):
        provider = _provider({2: "Read."}, per_call=1)
        result = await _parse()
        assert provider.calls == [[2], [3]]
        doc = result.parsed
        assert doc.stats.pages_ocr == 1 and [b.text for b in doc.blocks] == ["Typed page.", "Read."]
        failed = [w for w in doc.warnings if w.code == "ocr_failed"]
        assert failed and failed[0].count == 1 and "page 3" in failed[0].message
        assert partial_reason(doc) == "no_text_layer"  # REASONS' order; the report also counts ocr_failed
        assert result.ocr_stage.status == "partial" and result.ocr_stage.error.code == "ocr_failed"

    async def test_a_provider_that_is_down_is_not_asked_page_after_page(self):
        many = pdf([PdfPage().image() for _ in range(5)])
        provider = _provider({}, per_call=1)
        result = await _parse(many)
        assert provider.calls == [[1], [2]]  # two failures in a row stop the rest
        (failed,) = [w for w in result.parsed.warnings if w.code == "ocr_failed"]
        assert failed.count == 5 and result.ocr_stage.status == "failed"

    async def test_the_deadline_bounds_the_whole_document(self, monkeypatch):
        monkeypatch.setattr(limits, "ocr_timeout_s", lambda: 0.3)
        provider = _provider({2: "a", 3: "b"}, per_call=1, delay=1.0)
        result = await _parse()
        assert len(provider.calls) == 1
        (failed,) = [w for w in result.parsed.warnings if w.code == "ocr_failed"]
        assert failed.count == 2 and "TimeoutError" in failed.message


class TestThroughTheJob:
    async def test_the_report_carries_the_ocr_stage_and_its_cost(self, monkeypatch):
        _provider({2: "Scanned page two.", 3: "Scanned page three."})
        kb, report, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch)
        assert row.status == "indexed" and [s.stage for s in report.stages][:2] == ["parse", "ocr"]
        assert report.pages.ocr_applied == 2 and report.pages.no_text_layer == 2
        assert report.cost.by_stage.get("ocr") == 0.0  # FakeOcr is local and unpriced
        async with session() as s:
            (cost,) = (await s.execute(select(KnowledgeCostDB).where(KnowledgeCostDB.stage == "ocr"))).scalars()
        assert (cost.kb_id, cost.doc_id, cost.unit, cost.units) == (kb.id, row.id, "pages", 2.0)

    async def test_a_spent_budget_pauses_before_any_page_is_sent(self, monkeypatch):
        provider = _provider({2: "a", 3: "b"})
        _, report, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch, budget_monthly_usd=0.001)
        assert (row.status, row.status_reason) == ("paused", "budget_cap") and provider.calls == []

    async def test_the_budget_is_checked_at_what_the_deployment_charges_a_page(self, monkeypatch):
        """F334 (W1, D179): two pages fit a $0.0032 cap at the $0.0015 list price, not at the $0.001875 charged."""
        from core.knowledge import costs
        from core.knowledge.costs import Rate

        provider = _provider({2: "a", 3: "b"})
        provider.engine, provider.cost_source = "textract-detect-text-v1", "platform"

        async def pricer(model):
            return Rate(per_unit=0.001875, unit="pages") if model == "textract-detect-text-v1" else None

        costs.set_platform_pricer(pricer)
        _, _, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch, budget_monthly_usd=0.0032)
        assert (row.status, row.status_reason) == ("paused", "budget_cap") and provider.calls == []

    async def test_at_its_list_price_the_same_scan_fits_the_same_cap(self, monkeypatch):
        provider = _provider({2: "a", 3: "b"})
        provider.engine, provider.cost_source = "textract-detect-text-v1", "platform"
        _, _, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch, budget_monthly_usd=0.0032)
        assert row.status == "indexed" and provider.calls == [[2, 3]]

    async def test_an_empty_wallet_pauses_and_what_was_read_is_still_recorded(self, monkeypatch):
        provider = _provider({2: "a", 3: "b"}, per_call=1, fail_on_call=2, error=WalletExhausted("402"))
        _, report, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch)
        assert (row.status, row.status_reason) == ("paused", "wallet_402") and provider.calls == [[2], [3]]
        async with session() as s:
            costs = (await s.execute(select(KnowledgeCostDB).where(KnowledgeCostDB.stage == "ocr"))).scalars().all()
        assert [c.units for c in costs] == [1.0]


class TestReadAgainOnceOcrExists:
    async def test_reconcile_queues_a_scan_indexed_before_ocr_and_only_once(self, monkeypatch):
        kb, report, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch, real=True)
        assert (row.status, row.status_reason) == ("partial", "no_text_layer")
        recorder = RecordingDispatcher()
        seams.set_ingest_dispatcher(recorder)
        assert (await sync.reconcile(kb)).supported == 0  # no provider yet: nothing to do

        _provider({2: "a", 3: "b"})
        assert (await sync.reconcile(kb)).supported == 1
        assert [(j.op, j.path) for j in recorder.jobs] == [("upsert", "kb/scan.pdf")]
        await recorder.drain(run_ingest_job)
        again = await documents.get(row.id)
        assert again.status == "indexed" and any(s["stage"] == "ocr" for s in again.report["stages"])
        assert (await sync.reconcile(kb)).supported == 0

    async def test_an_ocr_that_ran_and_failed_is_not_retried_by_reconcile(self, monkeypatch):
        _provider({}, per_call=5)
        kb, report, row = await _ingest("kb/scan.pdf", SCANNED, monkeypatch, real=True)
        assert row.status == "partial" and report.pages.ocr_failed == 2
        seams.set_ingest_dispatcher(RecordingDispatcher())
        assert (await sync.reconcile(kb)).supported == 0
