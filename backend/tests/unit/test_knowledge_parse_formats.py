"""Which reader a file gets, and the parts every structured reader shares (A2, D162).

A file's extension picks its format and its bytes confirm it; a mislabelled
file we can read is read as what it is, one we cannot is refused saying so.
Zips are checked before any reader opens them. `DocBuilder` makes the
canonical text out of blocks so every offset slices its own text, and OCR text
merges into the pages that had none without moving any other page's blocks.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from core.knowledge.parse import ACCEPTED_EXTENSIONS, UnsupportedFormat, parse_bytes, supports
from core.knowledge.parse import formats as F
from core.knowledge.parse import limits
from core.knowledge.parse.limits import Encrypted, ExpandsTooFar, ParseRefused
from core.knowledge.parse.tree import DocBuilder, Headings, merge_ocr, source_info
from core.knowledge.parse.zipguard import SafeZip
from core.knowledge.parsed import ParsedDocument, ParserInfo

BACKEND = Path(__file__).resolve().parents[2]
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _zip(members: dict[str, bytes | str], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


DOCX = _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": "<w:document/>"})
XLSX = _zip({"[Content_Types].xml": "<Types/>", "xl/workbook.xml": "<workbook/>"})
ODT = _zip({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": "<x/>"},
           compression=zipfile.ZIP_STORED)
EPUB = _zip({"mimetype": "application/epub+zip"}, compression=zipfile.ZIP_STORED)


def _valid(doc: ParsedDocument) -> ParsedDocument:
    again = ParsedDocument.model_validate_json(doc.model_dump_json())  # the validator checks every offset
    assert again == doc
    return doc


def _source(path: str = "kb/x") -> object:
    return source_info(path=path, sha256="0" * 64, size=1, mime="x", source="vault")


class TestSniffing:
    @pytest.mark.parametrize("data,kind", [
        (b"%PDF-1.7\n", "pdf"), (b"\n\n  junk %PDF-1.4", "pdf"), (DOCX, "zip"), (OLE, "ole"),
        (b"{\\rtf1\\ansi hello}", "rtf"), (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, "png"), (b"\xff\xd8\xff\xe0", "jpeg"),
        (b"II*\x00rest", "tiff"), (b"MM\x00*rest", "tiff"), (b"RIFF\0\0\0\0WEBPVP8 ", "webp"),
        (b"\0\0\0\x18ftypheic\0\0\0\0", "heic"), (b"GIF89a", "gif"), (b"# hello", None),
    ])
    def test_magic(self, data, kind):
        assert F.sniff(data) == kind

    @pytest.mark.parametrize("data,name", [(DOCX, "docx"), (XLSX, "xlsx"), (ODT, "odt"), (EPUB, "epub"),
                                           (_zip({"a.txt": "x"}), None), (b"PK\x03\x04broken", None)])
    def test_a_zip_is_told_apart_by_its_members(self, data, name):
        assert F.zip_format(data) == name


class TestResolving:
    def test_the_extension_picks_the_format_when_the_bytes_agree(self):
        assert F.resolve("kb/a.PDF", b"%PDF-1.7").name == "pdf"
        assert F.resolve("kb/a.docx", DOCX).name == "docx"
        assert F.resolve("kb/a.jpg", b"\x89PNG\r\n\x1a\n").name == "image"  # any image kind reads as an image
        assert F.resolve("kb/notes.md", b"\x89PNG").name == "markdown"     # text: decode refuses binary itself

    def test_a_mislabelled_file_we_read_is_read_as_what_it_is(self):
        assert F.resolve("kb/old.doc", DOCX).name == "docx"               # Word saves .docx content as .doc
        assert F.resolve("kb/old.doc", b"{\\rtf1 x}").name == "rtf"       # and RTF as .doc
        assert F.resolve("kb/a.pdf", XLSX).name == "xlsx"

    def test_an_office_file_in_an_ole_container_is_encrypted_or_a_97_2003_file(self):
        with pytest.raises(Encrypted):
            F.resolve("kb/secret.docx", OLE + "EncryptionInfo".encode("utf-16-le"))
        assert F.resolve("kb/renamed.xlsx", OLE).name == "xls"

    def test_content_that_is_not_what_the_name_says_is_refused(self):
        with pytest.raises(UnsupportedFormat, match="named as a PDF"):
            F.resolve("kb/a.pdf", b"hello, not a pdf")
        with pytest.raises(UnsupportedFormat, match="not a format Knowledge reads"):
            F.resolve("kb/a.exe", b"MZ")

    def test_every_accepted_extension_has_a_format_and_secrets_are_not_accepted(self):
        assert ACCEPTED_EXTENSIONS == frozenset(F.BY_EXTENSION)
        assert {".pdf", ".docx", ".py", ".tsx", ".log", ".eml", ".heic"} <= ACCEPTED_EXTENSIONS
        assert not {".env", ".pem", ".key", ".exe", ".zip"} & ACCEPTED_EXTENSIONS

    def test_a_format_that_needs_what_is_missing_says_what(self, monkeypatch):
        monkeypatch.setenv("SYNAPSE_SOFFICE", "/nonexistent/soffice")
        assert F.soffice_path() is None and not F.has_capability("libreoffice")
        assert "LibreOffice" in F.missing_capability_message(F.BY_NAME["doc"])
        assert not F.has_capability("ocr", ocr=False) and F.has_capability("ocr", ocr=True)


class TestCodeAndText:
    def _parse(self, text: str, path: str) -> ParsedDocument:
        data = text.encode()
        return _valid(parse_bytes(data, path=path, source="vault", sha256=hashlib.sha256(data).hexdigest(), mime="x"))

    def test_source_code_is_read_as_code_blocks(self):
        doc = self._parse("def a():\n    return 1\n\n\ndef b():\n    return 2\n", "kb/src/m.py")
        assert [b.kind for b in doc.blocks] == ["code", "code"] and doc.blocks[1].text.startswith("def b")
        assert supports("kb/src/m.py") and supports("kb/app.tsx")

    def test_a_log_is_plain_paragraphs(self):
        doc = self._parse("line one\nline two\n\nnext", "kb/run.log")
        assert [b.kind for b in doc.blocks] == ["paragraph", "paragraph"]


class TestZipGuard:
    def test_a_normal_office_zip_opens(self):
        z = SafeZip(DOCX)
        assert z.has("word/document.xml") and z.read_text("word/document.xml") == "<w:document/>"

    def test_too_many_members(self, monkeypatch):
        monkeypatch.setattr(limits, "ZIP_MAX_MEMBERS", 3)
        with pytest.raises(ExpandsTooFar, match="parts"):
            SafeZip(_zip({f"m{i}": "x" for i in range(4)}))

    def test_a_member_that_expands_like_a_bomb(self):
        bomb = _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": b"\0" * (8 * 1024 * 1024)})
        assert len(bomb) < 50_000
        with pytest.raises(ExpandsTooFar, match="expands"):
            SafeZip(bomb)

    def test_too_much_in_total(self, monkeypatch):
        monkeypatch.setattr(limits, "ZIP_MAX_EXPANDED", 1000)
        with pytest.raises(ExpandsTooFar, match="expands to"):
            SafeZip(_zip({"a": "x" * 600, "b": "y" * 600}))

    def test_a_password_protected_member_and_a_damaged_zip(self):
        data = bytearray(_zip({"a.xml": "<a/>"}, compression=zipfile.ZIP_STORED))
        for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # set the encryption flag in both headers
            at = data.find(sig)
            data[at + (6 if sig == b"PK\x03\x04" else 8)] |= 0x1
        with pytest.raises(Encrypted):
            SafeZip(bytes(data))
        with pytest.raises(ParseRefused, match="damaged"):
            SafeZip(b"PK\x03\x04 not a zip at all")


class TestDocBuilder:
    def test_blocks_are_joined_and_every_offset_slices_its_text(self):
        b = DocBuilder()
        b.page(1)
        b.page(2)
        heads = Headings()
        b.add("heading", "Terms", page=1, level=1, heading_path=heads.push(1, "Terms"))
        b.add("paragraph", "  The supplier​ provides hosting.  ", page=1, heading_path=heads.path)
        b.add("paragraph", "   ", page=1)  # nothing visible: no block
        b.add("paragraph", "Payment in 30 days.", page=2, heading_path=heads.path)
        doc = _valid(b.build(_source(), ParserInfo(name="t", version="1")))
        assert doc.text == "Terms\n\nThe supplier provides hosting.\n\nPayment in 30 days."
        assert [x.id for x in doc.blocks] == ["b0001-0000", "b0001-0001", "b0002-0000"]
        assert doc.blocks[1].heading_path == ["Terms"]
        assert [(w.code, w.count) for w in doc.warnings] == [("zero_width_removed", 1)]
        assert [p.text_chars for p in doc.pages] == [len("Terms") + len("The supplier provides hosting."), 19]

    def test_headings_close_at_their_level(self):
        h = Headings()
        h.push(1, "A")
        h.push(2, "B")
        h.push(3, "C")
        assert h.push(2, "D") == ["A", "D"] and h.push(1, "E") == ["E"]

    def test_a_cut_at_the_size_cap_is_a_warning(self):
        b = DocBuilder(max_chars=30)
        for n in range(5):
            b.add("paragraph", f"paragraph number {n}")
        doc = _valid(b.build(_source(), ParserInfo(name="t", version="1")))
        assert len(doc.blocks) == 1 and doc.stats.truncated and doc.stats.truncated_reason == "truncated_size"
        assert "truncated_size" in [w.code for w in doc.warnings]

    def test_hidden_content_is_counted_and_an_empty_build_says_so(self):
        b = DocBuilder()
        b.hidden(3)
        doc = _valid(b.build(_source(), ParserInfo(name="t", version="1")))
        assert [(w.code, w.count) for w in doc.warnings] == [("hidden_text_removed", 3), ("empty_document", 1)]

    def test_sheets_are_numbered_apart_from_pages(self):
        b = DocBuilder()
        b.add("sheet", "Sheet A: x (1 rows)", sheet_index=0)
        b.add("table", "x\n1", sheet_index=0)
        b.add("sheet", "Sheet B: y (0 rows)", sheet_index=1)
        doc = _valid(b.build(_source(), ParserInfo(name="t", version="1")))
        assert [x.id for x in doc.blocks] == ["s0-0000", "s0-0001", "s1-0000"]


class TestMergeOcr:
    def _scanned_middle(self) -> ParsedDocument:
        b = DocBuilder()
        for n in (1, 2, 3):
            b.page(n)
        b.page_quality(2, no_text_layer=True, image_only=True)
        b.add("paragraph", "First page text.", page=1)
        b.add("paragraph", "Third page text.", page=3)
        b.warn("no_text_layer", "Page 2 has no text layer.", page=2)
        return _valid(b.build(_source(), ParserInfo(name="t", version="1")))

    def test_ocr_text_lands_on_its_page_and_no_other_block_moves_id(self):
        before = self._scanned_middle()
        after = _valid(merge_ocr(before, {2: ("Scanned line one\nline two\n\nSecond paragraph.", 0.91)},
                                 engine="fake"))
        assert [(x.id, x.page) for x in after.blocks] == [
            ("b0001-0000", 1), ("b0002-0000", 2), ("b0002-0001", 2), ("b0003-0000", 3)]
        assert after.blocks[1].text == "Scanned line one\nline two"
        quality = after.pages[1].quality
        assert quality.ocr_applied and quality.ocr_engine == "fake" and quality.ocr_confidence == 0.91
        assert after.stats.pages_no_text_layer == 1 and after.stats.pages_ocr == 1
        assert after.warnings == []  # read now: the page no longer makes the document partial

    def test_low_confidence_is_a_warning(self):
        after = merge_ocr(self._scanned_middle(), {2: ("blurry", 0.2)}, engine="fake")
        assert "ocr_low_confidence" in [w.code for w in after.warnings]

    def test_an_empty_ocr_answer_does_not_mark_the_page_read(self):
        after = _valid(merge_ocr(self._scanned_middle(), {2: ("  ", 0.9)}, engine="fake"))
        assert not after.pages[1].quality.ocr_applied and after.stats.pages_ocr == 0
        (warning,) = after.warnings
        assert (warning.code, warning.count, warning.page) == ("no_text_layer", 1, 2)

    def test_the_pipeline_sees_a_fully_read_scan_as_whole(self):
        from core.knowledge.pipeline import partial_reason

        before = self._scanned_middle()
        assert partial_reason(before) == "no_text_layer"
        assert partial_reason(merge_ocr(before, {2: ("read", 0.9)}, engine="fake")) is None
        assert partial_reason(merge_ocr(before, {2: ("", 0.9)}, engine="fake")) == "no_text_layer"


def test_importing_the_package_loads_no_parser_library():
    """Cloud's API process imports `core.knowledge.parse` (`supports`, `ACCEPTED_EXTENSIONS`); the
    readers' libraries load only where parsing runs."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(BACKEND)!r})
        import core.knowledge.parse, core.knowledge.parse.formats, core.knowledge.parse.limits
        core.knowledge.parse.supports("a/b.pdf", ocr=False)
        heavy = ("pdfplumber", "pypdfium2", "openpyxl", "PIL", "bs4", "docling", "yaml", "psutil", "olefile",
                 "defusedxml", "lxml", "numpy", "httpx")
        print(";".join(sorted(m for m in sys.modules if m.split(".")[0] in heavy)))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == ""
