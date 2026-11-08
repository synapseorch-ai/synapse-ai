"""The chunker (B1a, D158): every chunk is an exact span of its document, in every mode."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.knowledge.chunker import (
    ATTACHMENT_KEY, CHUNKER_VERSION, chunk_document, chunker_version, estimate_tokens, section_key,
)
from core.knowledge.ids import chunk_id
from core.knowledge.kb_settings import Chunking
from core.knowledge.parsed import ParsedDocument

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"
MODES = ("structure", "fixed", "page", "none")


def _fixture(name: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))


def _chunk(doc: ParsedDocument, **settings):
    return chunk_document(doc, doc_id="kdoc_" + "1" * 32, kb_id="kb_00000000000a",
                          snapshot_id="ksnap_" + "2" * 32, doc_version=3, settings=Chunking(**settings))


def _doc(*blocks, pages: int = 0) -> ParsedDocument:
    """A ParsedDocument from (kind, text, page, heading_path, furniture) tuples joined by blank lines."""
    parts, out, pos = [], [], 0
    for i, (kind, text, page, path, furniture) in enumerate(blocks):
        if parts:
            pos += 2
        out.append({"id": f"b{(page or 0):04d}-{i:04d}", "kind": kind, "text": text, "page": page,
                    "char_start": pos, "char_end": pos + len(text), "heading_path": path, "furniture": furniture})
        parts.append(text)
        pos += len(text)
    return ParsedDocument.model_validate({
        "source": {"path": "a/doc.md", "sha256": "f" * 64, "size_bytes": pos, "mime": "text/markdown",
                   "source": "vault"},
        "parser": {"name": "test", "version": "1"},
        "text": "\n\n".join(parts), "blocks": out,
        "pages": [{"number": n} for n in range(1, pages + 1)],
    })


def _sentences(n: int, word: str = "alpha") -> str:
    return " ".join(f"{word} beta gamma delta sentence number {i} ends here." for i in range(n))


def _source_text(doc: ParsedDocument, chunk) -> str:
    """The text a chunk slices: the document's, or its attachment's."""
    name = chunk.metadata.get(ATTACHMENT_KEY)
    if not name:
        return doc.text
    current = doc
    for part in name.split("/"):
        current = next(a.parsed for a in current.attachments if a.name == part)
    return current.text


# ── the invariant ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json")))
def test_every_chunk_is_an_exact_span(name, mode):
    doc = _fixture(name)
    result = _chunk(doc, mode=mode)
    assert result.chunks, f"{name} ({mode}) gave no chunks"
    for c in result.chunks:
        assert c.content == _source_text(doc, c)[c.char_start:c.char_end]
        assert c.content.strip() == c.content and c.content
        assert c.role == "leaf" and c.parent_id is None
        assert c.id == chunk_id(c.doc_id, c.snapshot_id, 3, c.ordinal)
    assert [c.ordinal for c in result.chunks] == list(range(len(result.chunks)))


def test_the_same_document_chunks_the_same_way_twice():
    doc = _fixture("pdf_contract")
    assert _chunk(doc).chunks == _chunk(doc).chunks


def test_the_version_names_the_mode():
    assert chunker_version("structure") == f"structure-{CHUNKER_VERSION}" == "structure-1"


# ── structure ──────────────────────────────────────────────────────────────────

def test_a_heading_starts_a_chunk_and_travels_with_its_table():
    chunks = _chunk(_fixture("pdf_contract")).chunks
    assert [c.kind for c in chunks] == ["text", "table", "text"]
    table = chunks[1]
    assert table.content.startswith("Fees\n\nService | Monthly fee")
    assert table.heading_path == ["Master Services Agreement", "Fees"]
    assert table.page_start == table.page_end == 2


def test_a_long_paragraph_is_cut_at_sentences_within_the_cap():
    doc = _doc(("heading", "Terms", None, ["Terms"], False),
               ("paragraph", _sentences(200), None, ["Terms"], False))
    chunks = _chunk(doc, target_tokens=100, max_tokens=150).chunks
    assert len(chunks) > 5
    assert all(c.tokens <= 150 for c in chunks)
    assert chunks[0].content.startswith("Terms\n\n")
    assert all(c.content.endswith(".") for c in chunks), "cuts land on sentence ends"
    covered = "".join(c.content for c in chunks).replace(" ", "").replace("\n", "")
    assert covered == doc.text.replace(" ", "").replace("\n", ""), "no overlap: every character exactly once"


def test_blocks_gather_up_to_the_target_and_a_new_section_starts_fresh():
    doc = _doc(("heading", "One", None, ["One"], False),
               *[("paragraph", _sentences(3), None, ["One"], False) for _ in range(6)],
               ("heading", "Two", None, ["Two"], False),
               ("paragraph", "Short.", None, ["Two"], False))
    chunks = _chunk(doc, target_tokens=100, max_tokens=200).chunks
    assert chunks[-1].content == "Two\n\nShort." and chunks[-1].heading_path == ["Two"]
    assert all(c.tokens <= 100 or c.content.count("\n\n") == 0 for c in chunks)
    assert len({section_key(c) for c in chunks}) == 2


def test_overlap_repeats_the_end_only_across_a_size_cut():
    paragraphs = [("paragraph", _sentences(3, f"w{i}"), None, ["One"], False) for i in range(8)]
    doc = _doc(("heading", "One", None, ["One"], False), *paragraphs,
               ("heading", "Two", None, ["Two"], False), ("paragraph", _sentences(3, "z"), None, ["Two"], False))
    chunks = _chunk(doc, target_tokens=100, max_tokens=200, overlap_pct=0.2).chunks
    one = [c for c in chunks if c.heading_path == ["One"]]
    assert len(one) >= 2
    for a, b in zip(one, one[1:]):
        assert b.char_start < a.char_end, "consecutive chunks of one section overlap"
        assert a.char_end - b.char_start <= 20 * 4 + 10
    two = next(c for c in chunks if c.heading_path == ["Two"])
    assert two.content.startswith("Two"), "a new section never repeats the last one's end"


def test_furniture_ends_a_chunk_and_is_never_inside_one():
    doc = _doc(("paragraph", "First page body.", 1, [], False),
               ("paragraph", "ACME CONFIDENTIAL — page 1", 1, [], True),
               ("paragraph", "Second page body.", 2, [], False), pages=2)
    chunks = _chunk(doc).chunks
    assert [c.content for c in chunks] == ["First page body.", "Second page body."]
    assert [(c.page_start, c.page_end) for c in chunks] == [(1, 1), (2, 2)]


def test_a_table_under_the_cap_is_never_split():
    rows = "\n".join(["Item | Amount"] + [f"Row {i} | {i * 10}" for i in range(60)])
    doc = _doc(("table", rows, None, [], False))
    chunks = _chunk(doc, target_tokens=100, max_tokens=800).chunks
    assert len(chunks) == 1 and chunks[0].kind == "table" and chunks[0].content == rows


def test_a_table_over_the_cap_splits_into_row_groups_carrying_its_header():
    rows = "\n".join(["Item | Amount"] + [f"Row {i} with a longer description | {i * 10}" for i in range(200)])
    doc = _doc(("table", rows, None, [], False))
    result = _chunk(doc, target_tokens=100, max_tokens=150)
    chunks = result.chunks
    assert len(chunks) > 3 and all(c.kind == "table" and c.tokens <= 150 for c in chunks)
    assert chunks[0].content.startswith("Item | Amount") and chunks[0].context == ""
    assert all(c.context == "Table columns: Item | Amount" for c in chunks[1:])
    assert all("\n" not in c.content[:1] for c in chunks)
    assert [w.code for w in result.warnings] == ["table_too_large"]


def test_a_sheet_is_a_sheet_card():
    chunks = _chunk(_fixture("xlsx_two_sheets")).chunks
    assert [c.kind for c in chunks] == ["sheet_card", "sheet_card"]
    assert chunks[0].content.startswith("Sheet Revenue")


def test_attachments_follow_the_body_with_their_own_offsets():
    doc = _fixture("eml_with_attachment")
    chunks = _chunk(doc).chunks
    body, attachment = chunks
    assert ATTACHMENT_KEY not in body.metadata
    assert attachment.metadata[ATTACHMENT_KEY] == "invoice.txt"
    assert attachment.metadata["_attachment_sha256"] == doc.attachments[0].sha256
    assert attachment.heading_path[0] == "invoice.txt"
    assert attachment.content == doc.attachments[0].parsed.text[attachment.char_start:attachment.char_end]


# ── the other modes ────────────────────────────────────────────────────────────

def test_fixed_windows_stay_under_the_cap():
    doc = _doc(("paragraph", _sentences(120), None, [], False))
    chunks = _chunk(doc, mode="fixed", target_tokens=100, max_tokens=120).chunks
    assert len(chunks) > 5 and all(c.tokens <= 120 for c in chunks)


def test_page_mode_gives_a_chunk_per_page():
    doc = _doc(("heading", "A", 1, ["A"], False), ("paragraph", "one.", 1, ["A"], False),
               ("paragraph", "two.", 2, ["A"], False), ("paragraph", "three.", 3, ["A"], False), pages=3)
    chunks = _chunk(doc, mode="page").chunks
    assert [c.content for c in chunks] == ["A\n\none.", "two.", "three."]
    assert [c.page_start for c in chunks] == [1, 2, 3]


def test_none_keeps_a_small_document_whole():
    doc = _fixture("docx_contract")
    chunks = _chunk(doc, mode="none").chunks
    assert len(chunks) == 1 and chunks[0].content == doc.text


# ── the rest ───────────────────────────────────────────────────────────────────

def test_an_empty_document_says_so():
    doc = _doc(("paragraph", "footer only", 1, [], True), pages=1)
    result = _chunk(doc)
    assert result.chunks == [] and [w.code for w in result.warnings] == ["empty_document"]


def test_the_language_is_normalized():
    doc = _fixture("record")
    chunk = chunk_document(doc, doc_id="kdoc_1", kb_id="kb_1", snapshot_id="ksnap_1", doc_version=1,
                           lang="English").chunks[0]
    assert chunk.lang == "english"
    other = chunk_document(doc, doc_id="kdoc_1", kb_id="kb_1", snapshot_id="ksnap_1", doc_version=1,
                           lang="klingon").chunks[0]
    assert other.lang == "simple"


def test_tokens_count_wide_characters_one_each():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("日本語のテキスト") == 8
    assert estimate_tokens("한국어") == 3
