# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""What a parser produces: one `ParsedDocument`, whatever the format.

Every parser (OSS's basic one, Docling, LibreOffice-then-Docling, the EML
walker, the web page extractor, a record) returns this tree, and the chunker
reads only this tree. Its JSON is also the **parse cache**: stored by sha256,
so changing the embedding model or the chunker never parses a file again.

**Offsets.** `text` is the document's canonical text: sanitized (hidden text,
HTML comments and zero-width characters removed — each removal is a warning),
blocks joined in reading order. Every block's `char_start:char_end` slices
exactly its own `text` out of it, counted in Unicode code points (the UI
converts to UTF-16 where it needs to). Citations and the quote check use the
same canonical text, so a citation's span always points at real characters.

**Nothing is silently dropped.** A page with no text layer is a page with
`quality.no_text_layer` and a `no_text_layer` warning until OCR reads it; a cut
is `stats.truncated` with a reason and a warning, and the document ends
`partial`, never `indexed`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PARSED_SCHEMA_VERSION = 1
MAX_ATTACHMENT_DEPTH = 3

WarningCode = Literal[
    "no_text_layer", "ocr_unavailable", "ocr_low_confidence", "ocr_failed", "truncated_pages",
    "truncated_size", "encrypted", "unsupported_format", "parser_timeout", "parser_memory",
    "parser_crashed", "hidden_text_removed", "zero_width_removed", "html_comments_removed",
    "attachment_skipped", "attachment_depth", "table_too_large", "sheet_truncated", "garbled_text",
    "legacy_converted", "empty_document", "enrich_fallback",
]
WARNING_CODES: tuple[str, ...] = WarningCode.__args__  # type: ignore[attr-defined]

BlockKind = Literal["heading", "paragraph", "list", "table", "code", "image", "sheet", "caption",
                    "footnote", "other"]
SourceKind = Literal["upload", "web", "record", "vault"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BBox(_Model):
    """A box on a page, in points, origin top-left."""

    page: int = Field(ge=1)
    x0: float
    y0: float
    x1: float
    y1: float


class PageQuality(_Model):
    no_text_layer: bool = False
    image_only: bool = False
    ocr_applied: bool = False
    ocr_engine: str | None = None
    ocr_confidence: float | None = None
    garbled_ratio: float | None = None
    rotation: int = 0


class Page(_Model):
    number: int = Field(ge=1)
    width: float | None = None
    height: float | None = None
    text_chars: int = 0
    quality: PageQuality = Field(default_factory=PageQuality)
    warnings: list[str] = Field(default_factory=list)


class TableData(_Model):
    rows: list[list[str]]
    header_rows: int = 0
    n_rows: int
    n_cols: int
    caption: str | None = None


class SheetData(_Model):
    """One spreadsheet sheet, as the "sheet card" embeds it (16 §3.5)."""

    name: str
    range: str | None = None
    n_rows: int
    n_cols: int
    headers: list[str] = Field(default_factory=list)
    sample_rows: list[list[str]] = Field(default_factory=list)
    dtypes: list[str] | None = None
    truncated: bool = False


class ImageData(_Model):
    alt: str | None = None
    ocr_text: str | None = None
    width: int | None = None
    height: int | None = None


class Block(_Model):
    """One unit of the document in reading order. `id` is `b<page:04d>-<n:04d>` or `s<sheet>-<n>`."""

    id: str
    kind: BlockKind
    text: str
    level: int | None = None
    page: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    heading_path: list[str] = Field(default_factory=list)
    bbox: list[BBox] | None = None
    furniture: bool = False  # running headers, footers, page numbers: kept, never embedded
    items: list[str] | None = None
    language: str | None = None
    table: TableData | None = None
    sheet: SheetData | None = None
    image: ImageData | None = None


class ParseWarning(_Model):
    code: WarningCode
    message: str
    page: int | None = None
    block_id: str | None = None
    count: int = 1


class SourceInfo(_Model):
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    mime: str
    source: SourceKind
    url: str | None = None
    fetched_at: str | None = None
    etag: str | None = None
    modified_at: str | None = None


class ParserInfo(_Model):
    name: str
    version: str
    options_hash: str = ""


class ParseStats(_Model):
    pages: int = 0
    pages_no_text_layer: int = 0
    pages_ocr: int = 0
    chars: int = 0
    tables: int = 0
    images: int = 0
    truncated: bool = False
    truncated_reason: str | None = None


class Attachment(_Model):
    name: str
    sha256: str
    mime: str
    size_bytes: int = Field(ge=0)
    parsed: ParsedDocument | None = None
    skipped_reason: str | None = None


class ParsedDocument(_Model):
    schema_version: Literal[1] = PARSED_SCHEMA_VERSION
    source: SourceInfo
    parser: ParserInfo
    title: str | None = None
    language: str | None = None
    doc_date: str | None = None
    text: str
    pages: list[Page] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)
    warnings: list[ParseWarning] = Field(default_factory=list)
    stats: ParseStats = Field(default_factory=ParseStats)

    @model_validator(mode="after")
    def _offsets_and_order(self) -> ParsedDocument:
        seen: set[str] = set()
        last = -1
        numbers = {p.number for p in self.pages}
        for block in self.blocks:
            if block.id in seen:
                raise ValueError(f"block id {block.id!r} appears twice")
            seen.add(block.id)
            if block.char_end < block.char_start:
                raise ValueError(f"block {block.id}: char_end before char_start")
            if block.char_start < last:
                raise ValueError(f"block {block.id}: blocks are not in reading order")
            last = block.char_start
            if self.text[block.char_start:block.char_end] != block.text:
                raise ValueError(f"block {block.id}: its offsets do not slice its text out of the document")
            for page in (block.page, block.page_end):
                if page is not None and numbers and page not in numbers:
                    raise ValueError(f"block {block.id}: page {page} is not one of the document's pages")
        if _depth(self) > MAX_ATTACHMENT_DEPTH:
            raise ValueError(f"attachments nest deeper than {MAX_ATTACHMENT_DEPTH}")
        return self


def _depth(doc: ParsedDocument) -> int:
    inner = [_depth(a.parsed) for a in doc.attachments if a.parsed is not None]
    return 1 + max(inner) if inner else 0


Attachment.model_rebuild()
ParsedDocument.model_rebuild()
