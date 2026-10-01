"""Word, Excel and PowerPoint 97–2003 (`.doc`, `.xls`, `.ppt`), converted by LibreOffice when it is installed.

LibreOffice headless converts the file to its OOXML form in a temp directory,
and the OOXML readers (`office.py`) read that — so hidden text, hidden sheets
and hidden slides are left out exactly as they are for a new-format file, and
the document carries a `legacy_converted` warning. Which of the three a file is
comes from its OLE container's streams, not its name (a `.xls` renamed `.xlsx`
reaches here too).

Each conversion runs with its own LibreOffice profile (`-env:UserInstallation`),
so two at once never share a lock, inside the parsing subprocess: its timeout,
its memory cap (LibreOffice's own memory included) and its allowlisted
environment hold for LibreOffice too. `SYNAPSE_SOFFICE` names the binary when
it is not `soffice` or `libreoffice` on PATH.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from core.knowledge.parse import limits
from core.knowledge.parse.formats import ReadContext, open_ole, soffice_path
from core.knowledge.parse.limits import ParseRefused
from core.knowledge.parsed import ParsedDocument, ParseWarning

#: How long one conversion may take, within the parse's own timeout.
CONVERT_TIMEOUT_S = 300
_TARGETS = {"doc": "docx", "xls": "xlsx", "ppt": "pptx"}
_LABELS = {"doc": "Word 97–2003 document", "xls": "Excel 97–2003 workbook", "ppt": "PowerPoint 97–2003 deck"}


def legacy_kind(data: bytes) -> str | None:
    """doc, xls or ppt, by the streams in the OLE container; None if it is none of them."""
    ole = open_ole(data)
    if ole is None:
        return None
    try:
        names = {"/".join(entry) for entry in ole.listdir()}
    finally:
        ole.close()
    if "WordDocument" in names:
        return "doc"
    if "Workbook" in names or "Book" in names:
        return "xls"
    if "PowerPoint Document" in names:
        return "ppt"
    return None


def convert(data: bytes, kind: str) -> bytes:
    """The OOXML form of a 97–2003 file. Raises `ParseRefused` when LibreOffice cannot convert it."""
    binary = soffice_path()
    if binary is None:
        raise ParseRefused("LibreOffice is not installed here.")
    target = _TARGETS[kind]
    with tempfile.TemporaryDirectory(prefix="synapse_soffice_") as tmp:
        root = Path(tmp)
        source = root / f"input.{kind}"
        source.write_bytes(data)
        (root / "out").mkdir()
        command = [binary, f"-env:UserInstallation={(root / 'profile').as_uri()}", "--headless", "--norestore",
                   "--nolockcheck", "--nodefault", "--convert-to", target, "--outdir", str(root / "out"),
                   str(source)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=min(CONVERT_TIMEOUT_S,
                                                                             limits.parse_timeout_s()))
        except subprocess.TimeoutExpired as exc:
            raise ParseRefused("LibreOffice took too long to convert the file.", reason="parser_timeout") from exc
        output = root / "out" / f"input.{target}"
        if result.returncode != 0 or not output.exists():
            detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()[-300:]
            raise ParseRefused(f"LibreOffice could not convert the file{': ' + detail if detail else ''}.")
        return output.read_bytes()


def read_legacy(data: bytes, ctx: ReadContext) -> ParsedDocument:
    from core.knowledge.parse import office

    kind = legacy_kind(data)
    if kind is None:
        raise ParseRefused("The file is not a Word, Excel or PowerPoint 97–2003 file.")
    converted = convert(data, kind)
    reader = {"doc": office.read_docx, "xls": office.read_xlsx, "ppt": office.read_pptx}[kind]
    doc = reader(converted, ctx)
    note = ParseWarning(code="legacy_converted", message=f"A {_LABELS[kind]}, read after LibreOffice converted it.")
    return doc.model_copy(update={"warnings": [*doc.warnings, note],
                                  "parser": doc.parser.model_copy(update={"name": f"{doc.parser.name}+soffice"})})
