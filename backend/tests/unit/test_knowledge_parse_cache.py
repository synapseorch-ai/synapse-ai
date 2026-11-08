"""The parse cache (A2, D162): a file parsed once is not parsed again by the same reader with the
same OCR; only a clean parse is kept; tenants never share an entry; and a deleted file's text
leaves the cache with its last document.
"""
from __future__ import annotations

import hashlib

import pytest

from _fakes.knowledge_db import knowledge_store, multi_tenant  # noqa: F401 — fixtures
from _fakes.knowledge_env import FakeIndexer, enable_knowledge, install_fake_indexing, use_saved_settings, write_vault
from _fakes.knowledge_formats import PdfPage, pdf, text_page
from core.knowledge import bases, dispatch, documents, seams
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.jobs import delete_document, drop_base, run_ingest_job
from core.knowledge.parse import cache, limits, runner
from core.knowledge.parse import formats as F
from core.knowledge.parse.service import parse_document
from core.knowledge.parse.tree import source_info
from core.knowledge.parsed import Attachment, ParsedDocument, ParserInfo, ParseWarning
from core.knowledge.testing.fakes import FakeOcr
from core.knowledge.types import IngestJob
from core.storage import get_blob_store
from core.tenancy import tenant_scope

pytestmark = pytest.mark.usefixtures("knowledge_store")

DATA = pdf([text_page("Hosting terms for the year."), PdfPage().image()])
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    yield
    seams.set_ocr_provider(None)
    dispatch.reset()


@pytest.fixture
def spawns(monkeypatch):
    """Counts the subprocesses started."""
    real, calls = runner.run_parse, []

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(runner, "run_parse", counting)
    return calls


async def _parse(path: str = "kb/a.pdf", data: bytes = DATA):
    return await parse_document(data, path=path, source="vault", sha256=hashlib.sha256(data).hexdigest(),
                                mime="application/pdf")


def _entries() -> list[str]:
    return [s.key for s in get_blob_store().list_stats(cache.PREFIX + "/")]


class TestHitsAndMisses:
    async def test_a_second_parse_of_the_same_content_is_a_hit_and_spawns_nothing(self, spawns):
        first = await _parse()
        second = await _parse("kb/copy/elsewhere.pdf")
        assert len(spawns) == 1 and second.detail == {"runner": "cache"}
        assert second.parsed.text == first.parsed.text and second.parsed.source.path == "kb/copy/elsewhere.pdf"
        assert _entries() == [cache.key(SHA, cache.signature(F.BY_NAME["pdf"], path="kb/a.pdf", docling=False,
                                                               ocr_engine=None))]
        assert _entries()[0].startswith(f"knowledge/parse-cache/{SHA[:2]}/")  # outside vault/

    async def test_configuring_ocr_parses_again(self, spawns):
        await _parse()
        seams.set_ocr_provider(FakeOcr({2: "Scanned."}))
        read = await _parse()
        assert len(spawns) == 2 and "Scanned." in read.parsed.text and len(_entries()) == 2

    def test_the_signature_names_what_the_result_depends_on(self):
        pdf_fmt, csv_fmt = F.BY_NAME["pdf"], F.BY_NAME["csv"]
        base = cache.signature(pdf_fmt, path="a/x.pdf", docling=False, ocr_engine=None)
        assert cache.signature(pdf_fmt, path="b/renamed.pdf", docling=False, ocr_engine=None) == base
        assert cache.signature(pdf_fmt, path="a/x.pdf", docling=False, ocr_engine="textract") != base
        assert cache.signature(pdf_fmt, path="a/x.pdf", docling=True, ocr_engine=None) != base
        bumped = F.Format(**{**pdf_fmt.__dict__, "version": "2"})
        assert cache.signature(bumped, path="a/x.pdf", docling=False, ocr_engine=None) != base
        assert (cache.signature(csv_fmt, path="a/x.csv", docling=False, ocr_engine=None)
                != cache.signature(csv_fmt, path="a/y.csv", docling=False, ocr_engine=None))  # the card names it

    async def test_a_text_file_is_never_cached(self, monkeypatch):
        monkeypatch.setattr(limits, "IN_PROCESS_MAX_BYTES", 4)
        data = b"# Notes\n\nA paragraph."
        await parse_document(data, path="kb/n.md", source="vault", sha256=hashlib.sha256(data).hexdigest(), mime="x")
        assert _entries() == []

    async def test_a_damaged_entry_is_dropped_and_parsed_again(self, spawns):
        await _parse()
        (entry,) = _entries()
        get_blob_store().put_bytes(entry, b"{not json")
        again = await _parse()
        assert len(spawns) == 2 and again.detail["runner"] == "subprocess" and len(_entries()) == 1

    async def test_tenants_never_share_an_entry(self, spawns, multi_tenant):  # noqa: F811
        await _parse()
        with tenant_scope("tenant-b"):
            other = await _parse()
            assert other.detail["runner"] == "subprocess"
        assert len(spawns) == 2


class TestOnlyACleanParseIsKept:
    def _doc(self, *codes: str) -> ParsedDocument:
        return ParsedDocument(source=source_info(path="a", sha256="0" * 64, size=1, mime="x", source="vault"),
                              parser=ParserInfo(name="t", version="1"), text="",
                              warnings=[ParseWarning(code=c, message=c) for c in codes])

    @pytest.mark.parametrize("codes,cut,kept", [
        ((), None, True), (("no_text_layer", "hidden_text_removed"), None, True), ((), "parser_timeout", False),
        (("ocr_failed",), None, False), (("parser_memory",), None, False)])
    def test_what_is_cacheable(self, codes, cut, kept):
        assert cache.cacheable(self._doc(*codes), cut=cut) is kept

    async def test_a_failed_ocr_is_not_kept_so_a_retry_tries_again(self, spawns):
        seams.set_ocr_provider(FakeOcr({}))  # fails every page
        await _parse()
        assert _entries() == []


class TestRehoming:
    def test_attachments_follow_their_parent(self):
        inner = ParsedDocument(source=source_info(path="mail/a.eml#invoice.txt", sha256="1" * 64, size=1, mime="x",
                                                  source="vault"), parser=ParserInfo(name="t", version="1"), text="")
        doc = ParsedDocument(source=source_info(path="mail/a.eml", sha256="2" * 64, size=1, mime="x", source="vault"),
                             parser=ParserInfo(name="eml", version="1"), text="",
                             attachments=[Attachment(name="invoice.txt", sha256="1" * 64, mime="x", size_bytes=1,
                                                     parsed=inner)])
        moved = cache.rehome(doc, source_info(path="kb/b.eml", sha256="2" * 64, size=1, mime="y", source="upload"))
        assert moved.source.path == "kb/b.eml" and moved.source.source == "upload"
        assert moved.attachments[0].parsed.source.path == "kb/b.eml#invoice.txt"


class TestADeletedFileLeavesTheCache:
    async def _ingest(self, monkeypatch, kb, path: str):
        install_fake_indexing(monkeypatch, FakeIndexer())
        write_vault(path, DATA)
        row, _ = await documents.upsert_for_path(kb, path, source="vault", sha256=None, size=None)
        await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="upsert", reason="vault_write",
                                       path=path, doc_id=row.id))
        return await documents.get(row.id)

    async def test_with_its_last_document(self, monkeypatch):
        kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
        one = await self._ingest(monkeypatch, kb, "kb/one.pdf")
        two = await self._ingest(monkeypatch, kb, "kb/two.pdf")  # the same content: a hit, one entry
        assert len(_entries()) == 1
        get_blob_store().delete("vault/kb/one.pdf")  # a delete takes the file first, then the document
        await delete_document(kb, one)
        assert len(_entries()) == 1  # two still has that content
        get_blob_store().delete("vault/kb/two.pdf")
        await delete_document(kb, await documents.get(two.id))
        assert _entries() == []

    async def test_with_its_knowledge_base(self, monkeypatch):
        kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
        await self._ingest(monkeypatch, kb, "kb/one.pdf")
        assert len(_entries()) == 1
        await drop_base(kb.id)
        assert _entries() == []

    async def test_the_sweep_takes_what_no_document_has(self, monkeypatch):
        kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
        await self._ingest(monkeypatch, kb, "kb/one.pdf")
        orphan = cache.key("f" * 64, "0" * 16)
        get_blob_store().put_bytes(orphan, b"{}")
        assert await cache.sweep(force=True) == 1
        assert orphan not in _entries() and len(_entries()) == 1
        assert await cache.sweep() == 0  # at most hourly
