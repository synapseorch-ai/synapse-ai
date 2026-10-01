"""Images through the OCR seam, and 97–2003 Office files through LibreOffice (A2, D162).

An image is read only when an OCR provider is set (D162 #2); without one it is
refused saying so, and read once one is. A `.doc`/`.xls`/`.ppt` is converted by
LibreOffice when it is installed: a fake `soffice` proves the plumbing here,
and the real one converts and reads a file wherever it is installed (the
container run).
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import sys
import textwrap

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from _fakes.knowledge_formats import docx, ole_file, w_p
from core.knowledge import bases, dispatch, documents, seams, sync
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.jobs import run_ingest_job
from core.knowledge.parse import UnsupportedFormat, parse_bytes, supports
from core.knowledge.parse import images as I
from core.knowledge.parse import libreoffice as L
from core.knowledge.parse.limits import ExpandsTooFar, ParseRefused
from core.knowledge.parse.service import parse_document
from core.knowledge.parsed import ParsedDocument
from core.knowledge.testing.fakes import FakeOcr, RecordingDispatcher
from core.knowledge.types import IngestJob


def _image(fmt: str = "PNG", *, frames: int = 1, size=(200, 100)) -> bytes:
    from PIL import Image

    pictures = [Image.new("L", size, color=40 * k) for k in range(frames)]
    buf = io.BytesIO()
    pictures[0].save(buf, format=fmt, save_all=frames > 1, append_images=pictures[1:])
    return buf.getvalue()


def _valid(doc: ParsedDocument) -> ParsedDocument:
    assert ParsedDocument.model_validate_json(doc.model_dump_json()) == doc
    return doc


@pytest.fixture(autouse=True)
def _seams():
    yield
    seams.set_ocr_provider(None)


class TestImages:
    def test_without_ocr_an_image_is_refused_saying_why(self):
        assert not supports("kb/receipt.png")
        with pytest.raises(UnsupportedFormat, match="no OCR is configured"):
            parse_bytes(_image(), path="kb/receipt.png", source="vault", sha256="0" * 64, mime="image/png")

    def test_with_ocr_each_frame_is_a_page_read_by_it(self):
        rendered = {}
        doc = _valid(parse_bytes(_image("TIFF", frames=3), path="kb/scan.tiff", source="vault", sha256="0" * 64,
                                 mime="image/tiff", ocr=True, render=lambda n, png: rendered.__setitem__(n, png)))
        assert sorted(rendered) == [1, 2, 3] and all(p.quality.image_only for p in doc.pages)
        assert doc.blocks == [] and doc.warnings[0].code == "no_text_layer" and doc.warnings[0].count == 3
        assert all(png.startswith(b"\x89PNG") for png in rendered.values())

    async def test_through_the_service_the_text_lands_on_its_pages(self):
        seams.set_ocr_provider(FakeOcr({1: "Receipt\n\nTotal $12.40"}))
        data = _image("JPEG")
        result = await parse_document(data, path="kb/r.jpg", source="vault", sha256=hashlib.sha256(data).hexdigest(),
                                      mime="image/jpeg")
        doc = _valid(result.parsed)
        assert [b.text for b in doc.blocks] == ["Receipt", "Total $12.40"] and doc.stats.pages_ocr == 1
        assert result.ocr_stage.detail["read"] == 1 and doc.warnings == []

    def test_a_huge_image_is_refused_before_decoding(self, monkeypatch):
        monkeypatch.setattr(I, "MAX_PIXELS", 100)
        with pytest.raises(ExpandsTooFar, match="megapixels"):
            parse_bytes(_image(size=(400, 400)), path="kb/a.png", source="vault", sha256="0" * 64, mime="x", ocr=True)

    def test_a_damaged_image(self):
        with pytest.raises(ParseRefused, match="damaged"):
            parse_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 40, path="kb/a.png", source="vault", sha256="0" * 64,
                        mime="x", ocr=True)

    def test_heic_needs_pillow_heif_and_ocr(self):
        seams.set_ocr_provider(FakeOcr({}))
        import importlib.util

        assert supports("kb/p.heic") == (importlib.util.find_spec("pillow_heif") is not None)


@pytest.mark.usefixtures("knowledge_store")
class TestAnImageWaitsForOcr:
    async def test_skipped_without_ocr_then_read_once_a_provider_exists(self, monkeypatch):
        dispatch.reset()
        use_saved_settings(monkeypatch)
        await enable_knowledge()
        kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
        write_vault("kb/r.png", _image())
        row, _ = await documents.upsert_for_path(kb, "kb/r.png", source="vault", sha256=None, size=None)
        await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="upsert", reason="vault_write",
                                       path="kb/r.png", doc_id=row.id))
        skipped = await documents.get(row.id)
        assert (skipped.status, skipped.status_reason) == ("skipped", "unsupported_format")
        assert "OCR" in skipped.error

        recorder = RecordingDispatcher()
        seams.set_ingest_dispatcher(recorder)
        seams.set_ocr_provider(FakeOcr({1: "Receipt total 12.40"}))
        assert (await sync.reconcile(kb)).supported == 1
        await recorder.drain(run_ingest_job)
        done = await documents.get(row.id)
        assert done.status == "indexed" and done.parser == "image/1"
        dispatch.reset()


# ── 97–2003 files ──────────────────────────────────────────────────────────────

FAKE_SOFFICE = textwrap.dedent("""\
    #!{python}
    import shutil, sys
    from pathlib import Path
    here = Path(__file__).parent
    (here / "args.txt").write_text("\\n".join(sys.argv[1:]))
    target = sys.argv[sys.argv.index("--convert-to") + 1]
    outdir = Path(sys.argv[sys.argv.index("--outdir") + 1])
    source = Path(sys.argv[-1])
    if (here / "fail").exists():
        print("Error: source file could not be loaded", file=sys.stderr)
        sys.exit(1)
    shutil.copy(here / f"output.{{target}}", outdir / f"{{source.stem}}.{{target}}")
""")


@pytest.fixture
def fake_soffice(tmp_path, monkeypatch):
    script = tmp_path / "soffice"
    script.write_text(FAKE_SOFFICE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "output.docx").write_bytes(docx(w_p("Converted from Word 97.") + w_p("Second paragraph.")))
    monkeypatch.setenv("SYNAPSE_SOFFICE", str(script))
    return tmp_path


class TestLegacy:
    DOC = ole_file({("WordDocument",): b"\0" * 64, ("1Table",): b"\0" * 64})

    @pytest.mark.parametrize("stream,kind", [("WordDocument", "doc"), ("Workbook", "xls"), ("Book", "xls"),
                                             ("PowerPoint Document", "ppt"), ("Other", None)])
    def test_the_kind_comes_from_the_container(self, stream, kind):
        assert L.legacy_kind(ole_file({(stream,): b"x" * 10})) == kind
        assert L.legacy_kind(b"not ole") is None

    def test_without_libreoffice_it_waits_saying_why(self, monkeypatch):
        monkeypatch.setenv("SYNAPSE_SOFFICE", "/nonexistent/soffice")
        assert not supports("kb/old.doc")
        with pytest.raises(UnsupportedFormat, match="LibreOffice"):
            parse_bytes(self.DOC, path="kb/old.doc", source="vault", sha256="0" * 64, mime="x")

    def test_converted_then_read_by_the_ooxml_reader(self, fake_soffice):
        assert supports("kb/old.doc")
        doc = _valid(parse_bytes(self.DOC, path="kb/old.doc", source="vault", sha256="0" * 64, mime="x"))
        assert [b.text for b in doc.blocks] == ["Converted from Word 97.", "Second paragraph."]
        assert doc.parser.name == "docx+soffice" and [w.code for w in doc.warnings] == ["legacy_converted"]
        args = (fake_soffice / "args.txt").read_text().splitlines()
        assert args[0].startswith("-env:UserInstallation=file://") and "--headless" in args
        assert args[args.index("--convert-to") + 1] == "docx"

    def test_a_renamed_97_2003_file_is_converted_too(self, fake_soffice):
        doc = parse_bytes(self.DOC, path="kb/renamed.docx", source="vault", sha256="0" * 64, mime="x")
        assert doc.parser.name == "docx+soffice"

    def test_a_conversion_that_fails_says_so(self, fake_soffice):
        (fake_soffice / "fail").write_text("")
        with pytest.raises(ParseRefused, match="could not convert.*could not be loaded"):
            parse_bytes(self.DOC, path="kb/old.doc", source="vault", sha256="0" * 64, mime="x")

    @pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")),
                        reason="LibreOffice is not installed here; the container run covers it")
    def test_real_libreoffice_round_trip(self, tmp_path, monkeypatch):
        """A .docx written to .doc by LibreOffice, then read back through it (no .doc is committed: KB0)."""
        import subprocess

        monkeypatch.delenv("SYNAPSE_SOFFICE", raising=False)
        source = tmp_path / "memo.docx"
        source.write_bytes(docx(w_p("Quarterly memo", style="Heading1") + w_p("Revenue rose four percent.")))
        binary = shutil.which("soffice") or shutil.which("libreoffice")
        subprocess.run([binary, f"-env:UserInstallation={(tmp_path / 'p').as_uri()}", "--headless", "--convert-to",
                        "doc", "--outdir", str(tmp_path), str(source)], check=True, capture_output=True, timeout=180,
                       env={**os.environ})
        legacy = (tmp_path / "memo.doc").read_bytes()
        assert L.legacy_kind(legacy) == "doc"
        doc = _valid(parse_bytes(legacy, path="kb/memo.doc", source="vault", sha256="0" * 64, mime="x"))
        assert "Revenue rose four percent." in doc.text and "legacy_converted" in [w.code for w in doc.warnings]
