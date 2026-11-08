"""ParsedDocument, Chunk, the status machine and the ingestion report (D153)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.knowledge import status
from core.knowledge.chunks import Chunk
from core.knowledge.errors import InvalidTransition
from core.knowledge.ids import chunk_id
from core.knowledge.parsed import ParsedDocument

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"


def _fixtures():
    return sorted(FIXTURES.glob("*.json"))


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_every_fixture_is_a_valid_parsed_document(path):
    doc = ParsedDocument.model_validate(json.loads(path.read_text("utf-8")))
    for block in doc.blocks:
        assert doc.text[block.char_start:block.char_end] == block.text
    again = ParsedDocument.model_validate_json(doc.model_dump_json())
    assert again == doc  # the JSON is the parse cache: it round-trips exactly


def test_there_is_a_fixture_per_source_shape():
    assert {p.stem for p in _fixtures()} == {"pdf_contract", "docx_contract", "xlsx_two_sheets",
                                             "eml_with_attachment", "web_page", "record"}


def test_a_page_without_text_is_reported_not_dropped():
    doc = ParsedDocument.model_validate_json((FIXTURES / "pdf_contract.json").read_text("utf-8"))
    assert [p.number for p in doc.pages if p.quality.no_text_layer] == [3]
    assert any(w.code == "no_text_layer" and w.page == 3 for w in doc.warnings)


def _pdf() -> dict:
    return json.loads((FIXTURES / "pdf_contract.json").read_text("utf-8"))


def test_offsets_that_do_not_slice_the_text_are_refused():
    doc = _pdf()
    doc["blocks"][1]["char_start"] += 1
    with pytest.raises(ValidationError, match="offsets"):
        ParsedDocument.model_validate(doc)


def test_blocks_out_of_reading_order_are_refused():
    doc = _pdf()
    doc["blocks"][0], doc["blocks"][1] = doc["blocks"][1], doc["blocks"][0]
    with pytest.raises(ValidationError, match="reading order"):
        ParsedDocument.model_validate(doc)


def test_a_block_on_a_page_the_document_lacks_is_refused():
    doc = _pdf()
    doc["blocks"][0]["page"] = 9
    with pytest.raises(ValidationError, match="page 9"):
        ParsedDocument.model_validate(doc)


def test_an_unknown_warning_code_or_field_is_refused():
    doc = _pdf()
    doc["warnings"][0]["code"] = "something_new"
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(doc)
    doc = _pdf()
    doc["surprise"] = 1
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(doc)


def test_attachments_nest_at_most_three_deep():
    eml = json.loads((FIXTURES / "eml_with_attachment.json").read_text("utf-8"))
    inner = eml["attachments"][0]["parsed"]
    nested = inner
    for _ in range(3):
        nested = {**inner, "attachments": [{**eml["attachments"][0], "parsed": nested}]}
    deep = {**eml, "attachments": [{**eml["attachments"][0], "parsed": nested}]}
    with pytest.raises(ValidationError, match="deeper"):
        ParsedDocument.model_validate(deep)


class TestChunk:
    def test_search_text_carries_the_context(self):
        c = Chunk(id=chunk_id("kdoc_1", "ksnap_1", 1, 0), doc_id="kdoc_1", kb_id="kb_1", snapshot_id="ksnap_1",
                  doc_version=1, doc_sha256="x", ordinal=0, content="Fees are monthly.", context="MSA > Fees:",
                  char_start=0, char_end=17)
        assert c.search_text() == "MSA > Fees: Fees are monthly."
        assert c.model_copy(update={"context": ""}).search_text() == "Fees are monthly."

    def test_a_chunk_kind_outside_the_contract_is_refused(self):
        with pytest.raises(ValidationError):
            Chunk(id="kch_x", doc_id="d", kb_id="k", snapshot_id="s", doc_version=0, doc_sha256="x", ordinal=0,
                  kind="image", content="", char_start=0, char_end=0)


class TestStatusMachine:
    def test_the_happy_path(self):
        path = [None, "queued", "parsing", "indexing", "indexed", "queued", "deleting"]
        for current, target in zip(path, path[1:]):
            status.check_transition(current, target)

    @pytest.mark.parametrize("current,target", [
        (None, "indexed"), ("indexed", "parsing"), ("paused", "indexing"), ("deleting", "queued"),
        ("failed", "indexed"), ("skipped", "partial"),
    ])
    def test_what_it_refuses(self, current, target):
        with pytest.raises(InvalidTransition):
            status.check_transition(current, target)

    def test_a_paused_document_only_resumes_by_being_queued(self):
        assert status.TRANSITIONS["paused"] == {"queued", "deleting"}

    def test_every_status_can_be_deleted_and_nothing_leaves_deleting(self):
        for s in status.STATUSES:
            assert status.can_transition(s, "deleting")
        assert status.TRANSITIONS["deleting"] == {"deleting"}

    def test_allowed_from_is_the_compare_and_set_set(self):
        assert status.allowed_from("indexing") == {"parsing"}
        assert "indexing" in status.allowed_from("queued") and "paused" in status.allowed_from("queued")

    def test_every_status_has_a_row(self):
        assert set(status.TRANSITIONS) == set(status.STATUSES)

    def test_a_report_says_what_happened(self):
        report = status.IngestionReport(doc_id="kdoc_1", path="a.pdf", outcome="partial",
                                        status_reason="truncated_pages",
                                        stages=[status.StageReport(stage="parse", status="partial")],
                                        pages=status.PagesReport(total=3, with_text=2, no_text_layer=1, skipped=[3]))
        assert status.IngestionReport.model_validate_json(report.model_dump_json()) == report
        with pytest.raises(ValidationError):
            status.IngestionReport(doc_id="kdoc_1", path="a.pdf", outcome="done")  # type: ignore[arg-type]


class TestTheIndexingHandOff:
    def test_it_resolves_to_b1s_index_document(self):
        from core.knowledge.pipeline import index_document
        from core.knowledge.pipeline_contract import resolve_index_entrypoint
        assert resolve_index_entrypoint() is index_document

    def test_without_the_indexing_module_it_is_an_honest_refusal(self, monkeypatch):
        from core.knowledge import seams
        from core.knowledge.errors import KnowledgeUnavailable
        from core.knowledge.pipeline_contract import resolve_index_entrypoint
        monkeypatch.setattr(seams, "_load", lambda dotted: None)
        with pytest.raises(KnowledgeUnavailable):
            resolve_index_entrypoint()

    async def test_a_fake_indexer_receives_the_context(self, monkeypatch):
        from core.knowledge import seams
        from core.knowledge.kb_settings import KnowledgeBase
        from core.knowledge.pipeline_contract import (
            INDEX_ENTRYPOINT, IndexContext, IndexOutcome, resolve_index_entrypoint,
        )
        from core.knowledge.types import Attribution

        seen = []

        async def index_document(ctx: IndexContext) -> IndexOutcome:
            seen.append(ctx)
            return IndexOutcome(status="indexed", chunks=len(ctx.parsed.blocks), tokens=0)

        monkeypatch.setattr(seams, "_load", lambda dotted: index_document if dotted == INDEX_ENTRYPOINT else None)
        parsed = ParsedDocument.model_validate_json((FIXTURES / "record.json").read_text("utf-8"))
        kb = KnowledgeBase(id="kb_00000000000a", name="Tickets", folder="tickets")
        ctx = IndexContext(kb=kb, doc_id="kdoc_1", doc_version=1, sha256="x", path="tickets/_records/t.json",
                           source="record", parsed=parsed, snapshot_id="ksnap_1",
                           attribution=Attribution(kb_id=kb.id, doc_id="kdoc_1"))
        outcome = await resolve_index_entrypoint()(ctx)
        assert outcome.status == "indexed" and outcome.chunks == 1 and seen == [ctx]
