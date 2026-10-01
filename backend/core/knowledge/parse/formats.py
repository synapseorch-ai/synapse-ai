"""Which reader a file gets: by its extension, checked against what its bytes say it is.

The extension picks a format; sniffing (magic numbers, the zip's members, an
OLE container) confirms it, corrects a mislabelled file when the content is
something we read (a `.doc` that is really a `.docx` or an RTF, as Word saves
them), and refuses honestly when it is neither. An Office file that is an OLE
container instead of a zip is password-protected: Office encrypts OOXML that
way.

Readers are named, not imported: a reader's module loads only in the process
that parses, so importing this module stays cheap and dependency-free.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import posixpath
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.parsed import ParsedDocument, SourceInfo


@dataclass(frozen=True)
class Format:
    name: str
    label: str
    extensions: frozenset[str]
    reader: str                       # "module:function", called as reader(data, ctx)
    magic: str | None = None          # what `sniff` must say: pdf, zip, ole, rtf, an image kind; None = text
    text: bool = False                # cheap enough to parse in a thread when small
    needs: str | None = None          # a capability: "ocr", "libreoffice", "heic"
    paged: bool = False               # has pages (or slides) the page cap counts


def _f(name: str, label: str, exts: str, reader: str, **kw) -> Format:
    return Format(name=name, label=label, extensions=frozenset(exts.split()), reader=reader, **kw)


_T = "core.knowledge.parse"

#: Programming languages and their kin: read as text, cut into `code` blocks.
CODE_EXTENSIONS = frozenset(
    ".py .pyi .js .mjs .cjs .jsx .ts .tsx .java .kt .kts .scala .go .rs .rb .php .c .h .cc .cpp .cxx .hpp "
    ".cs .swift .m .r .jl .lua .pl .sh .bash .zsh .ps1 .bat .sql .graphql .proto .css .scss .less .vue "
    ".svelte .dart .ex .exs .erl .hs .clj .tf .hcl .gradle .cmake".split())
#: Other plain text: read as paragraphs.
PLAIN_EXTENSIONS = frozenset(".txt .text .log .rst .adoc .asciidoc .tex .ini .cfg .conf .toml .properties".split())

FORMATS: tuple[Format, ...] = (
    _f("markdown", "Markdown", ".md .markdown", f"{_T}:read_text", text=True),
    _f("plain", "text", " ".join(sorted(PLAIN_EXTENSIONS)), f"{_T}:read_text", text=True),
    _f("code", "source code", " ".join(sorted(CODE_EXTENSIONS)), f"{_T}:read_text", text=True),
    _f("json", "JSON", ".json", f"{_T}:read_text", text=True),
    _f("csv", "CSV", ".csv .tsv", f"{_T}:read_text", text=True),
    _f("pdf", "a PDF", ".pdf", f"{_T}.pdf:read_pdf", magic="pdf", paged=True),
    _f("docx", "a Word document", ".docx .docm .dotx", f"{_T}.office:read_docx", magic="zip"),
    _f("pptx", "a PowerPoint deck", ".pptx .pptm .ppsx", f"{_T}.office:read_pptx", magic="zip", paged=True),
    _f("xlsx", "an Excel workbook", ".xlsx .xlsm", f"{_T}.office:read_xlsx", magic="zip"),
    _f("odt", "an OpenDocument text", ".odt", f"{_T}.odf:read_odt", magic="zip"),
    _f("odp", "an OpenDocument presentation", ".odp", f"{_T}.odf:read_odp", magic="zip", paged=True),
    _f("ods", "an OpenDocument spreadsheet", ".ods", f"{_T}.odf:read_ods", magic="zip"),
    _f("epub", "an EPUB book", ".epub", f"{_T}.epub:read_epub", magic="zip"),
    _f("rtf", "an RTF document", ".rtf", f"{_T}.rtf:read_rtf", magic="rtf"),
    _f("html", "a web page", ".html .htm .xhtml", f"{_T}.html:read_html"),
    _f("xml", "XML", ".xml", f"{_T}.structured:read_xml"),
    _f("yaml", "YAML", ".yaml .yml", f"{_T}.structured:read_yaml"),
    _f("eml", "an email", ".eml", f"{_T}.eml:read_eml"),
    _f("msg", "an Outlook message", ".msg", f"{_T}.eml:read_msg", magic="ole"),
    _f("image", "an image", ".png .jpg .jpeg .tif .tiff .webp", f"{_T}.images:read_image", magic="image",
       needs="ocr", paged=True),
    _f("heic", "a HEIC photo", ".heic .heif", f"{_T}.images:read_image", magic="image", needs="heic", paged=True),
    _f("doc", "a Word 97–2003 document", ".doc", f"{_T}.libreoffice:read_legacy", magic="ole",
       needs="libreoffice"),
    _f("ppt", "a PowerPoint 97–2003 deck", ".ppt", f"{_T}.libreoffice:read_legacy", magic="ole",
       needs="libreoffice", paged=True),
    _f("xls", "an Excel 97–2003 workbook", ".xls", f"{_T}.libreoffice:read_legacy", magic="ole",
       needs="libreoffice"),
)
BY_NAME: dict[str, Format] = {f.name: f for f in FORMATS}
BY_EXTENSION: dict[str, Format] = {ext: f for f in FORMATS for ext in f.extensions}
_OOXML = ("docx", "pptx", "xlsx")
_LEGACY = {"docx": "doc", "pptx": "ppt", "xlsx": "xls"}
#: The stream Office writes into the OLE container that wraps an encrypted OOXML file.
_ENCRYPTION_INFO = "EncryptionInfo".encode("utf-16-le")


def extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def format_for_path(path: str) -> Format | None:
    return BY_EXTENSION.get(extension(path))


# ── capabilities ───────────────────────────────────────────────────────────────

def soffice_path() -> str | None:
    """LibreOffice's binary: `SYNAPSE_SOFFICE`, else `soffice` or `libreoffice` on PATH."""
    explicit = os.environ.get("SYNAPSE_SOFFICE")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which("soffice") or shutil.which("libreoffice")


def has_capability(name: str | None, *, ocr: bool | None = None) -> bool:
    """Whether this process can read a format that needs `name`. `ocr`: whether an OCR provider is
    there — the parent knows (the seam); the parsing child is told, since no seam is registered in it."""
    if name is None:
        return True
    if ocr is None:
        from core.knowledge.seams import get_ocr_provider
        ocr = get_ocr_provider() is not None
    if name == "libreoffice":
        return soffice_path() is not None
    if name == "heic":
        return ocr and importlib.util.find_spec("pillow_heif") is not None
    if name == "ocr":
        return ocr
    return False


def reader_built(fmt: Format) -> bool:
    """Whether the reader's module exists (a format's reader lands in its own commit)."""
    return importlib.util.find_spec(fmt.reader.partition(":")[0]) is not None


def missing_capability_message(fmt: Format) -> str:
    if fmt.needs == "libreoffice":
        return (f"{fmt.label[0].upper()}{fmt.label[1:]} is read through LibreOffice, which is not installed "
                "here. Install it and the file is read automatically.")
    if fmt.needs == "ocr":
        return "An image is read by OCR, and no OCR is configured. Once one is, the file is read automatically."
    if fmt.needs == "heic":
        return ("A HEIC photo is read by OCR after conversion, which needs pillow-heif and an OCR provider "
                "(the knowledge extra).")
    return f"{fmt.label} is not read yet."


# ── sniffing ───────────────────────────────────────────────────────────────────

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")


def sniff(data: bytes) -> str | None:
    """What the bytes are: pdf, zip, ole, rtf, png, jpeg, tiff, webp, heic, gif, or None (text or unknown)."""
    head = data[:16]
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(_OLE):
        return "ole"
    if head.lstrip().startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "heic"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def zip_format(data: bytes) -> str | None:
    """Which zip-based format the bytes hold, by their members; None if none we know."""
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "mimetype" in names:
                mimetype = zf.read("mimetype")[:100].decode("ascii", errors="replace").strip()
                return {"application/vnd.oasis.opendocument.text": "odt",
                        "application/vnd.oasis.opendocument.presentation": "odp",
                        "application/vnd.oasis.opendocument.spreadsheet": "ods",
                        "application/epub+zip": "epub"}.get(mimetype)
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if "[Content_Types].xml" in names:
        if "word/document.xml" in names:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "xl/workbook.xml" in names:
            return "xlsx"
    return None


def resolve(path: str, data: bytes) -> Format:
    """The format to read `data` as. Raises `UnsupportedFormat` or `Encrypted`."""
    from core.knowledge.parse import UnsupportedFormat
    from core.knowledge.parse.limits import Encrypted

    ext = extension(path)
    fmt = BY_EXTENSION.get(ext)
    if fmt is None:
        raise UnsupportedFormat(f"{ext or 'A file without an extension'} is not a format Knowledge reads.")
    if fmt.magic is None:
        return fmt  # text: `decode` refuses binary content itself
    kind = sniff(data)
    actual = _actual(kind, data)
    if actual == fmt.name:
        return fmt
    if fmt.magic == "image" and kind in ("png", "jpeg", "tiff", "webp", "heic"):
        return BY_NAME["heic"] if kind == "heic" else BY_NAME["image"]
    if fmt.name in _OOXML and kind == "ole":
        if _ENCRYPTION_INFO in data:
            raise Encrypted(f"The file is {fmt.label} protected by a password.")
        return BY_NAME[_LEGACY[fmt.name]]  # a 97–2003 file given the new extension
    if fmt.magic == "ole" and kind == "ole":
        return fmt  # .doc / .ppt / .xls / .msg: LibreOffice and the MSG reader tell them apart
    if actual is not None:
        return BY_NAME[actual]  # mislabelled, but something we read (a .doc that is a .docx, or RTF)
    raise UnsupportedFormat(f"The file is named as {fmt.label}, but its content is not one.")


def _actual(kind: str | None, data: bytes) -> str | None:
    if kind == "zip":
        return zip_format(data)
    if kind in ("pdf", "rtf"):
        return kind
    if kind in ("png", "jpeg", "tiff", "webp"):
        return "image"
    if kind == "heic":
        return "heic"
    return None


# ── what a reader is given ─────────────────────────────────────────────────────

@dataclass
class ReadContext:
    """What a reader needs besides the bytes.

    `max_pages`: refuse a file with more (counted before parsing). `ocr`: render
    pages that have no text layer to PNG through `render`, for the OCR seam.
    `checkpoint`: a paged reader calls it with a function building what it has
    read so far, so a parse cut by a timeout still indexes those pages.
    `depth`: how deep inside email attachments this file sits.
    """

    path: str
    source: SourceInfo
    max_pages: int | None = None
    ocr: bool = False
    depth: int = 0
    docling: bool = False
    render: Callable[[int, bytes], None] | None = None
    checkpoint: Callable[[Callable[[], ParsedDocument]], None] | None = None
    notes: dict = field(default_factory=dict)

    def save_progress(self, build: Callable[[], ParsedDocument]) -> None:
        if self.checkpoint is not None:
            self.checkpoint(build)


def load_reader(fmt: Format) -> Callable[[bytes, ReadContext], ParsedDocument]:
    module_name, _, attr = fmt.reader.partition(":")
    return getattr(importlib.import_module(module_name), attr)
