"""Building a `ParsedDocument` block by block, for every structured reader.

The text formats (`text.py`) cut blocks out of a text that already exists. A
PDF, an Office file, an email or a web page has no such text: its readers
produce blocks, and `DocBuilder` makes the canonical text out of them — every
block sanitized first, then joined in reading order by a blank line — so each
block's `char_start:char_end` slices exactly its own text (the contract's rule).

Block ids are `b<page:04d>-<n:04d>`, numbered per page (page 0 for a format
without pages), or `s<sheet>-<n:04d>` for a spreadsheet's sheets. Numbering per
page is what lets OCR add a page's text later without renumbering any other
page's blocks (`merge_ocr`).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from core.knowledge.parse.sanitize import sanitize_text
from core.knowledge.parsed import (
    Block, Page, PageQuality, ParsedDocument, ParserInfo, ParseStats, ParseWarning, SourceInfo,
)

#: Past these the rest is cut: the document ends `partial` with a `truncated_size` warning.
MAX_TEXT_CHARS = 5_000_000
MAX_BLOCKS = 50_000
SEPARATOR = "\n\n"

_SANITIZE_CODES = ("zero_width_removed", "html_comments_removed")


class Headings:
    """The heading path in force: `push(level, title)` closes every heading at that level or deeper."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> list[str]:
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, title))
        return self.path

    @property
    def path(self) -> list[str]:
        return [title for _, title in self._stack]

    def reset(self) -> None:
        self._stack = []


class DocBuilder:
    """Collects blocks in reading order; `build()` returns the validated `ParsedDocument`."""

    def __init__(self, *, max_chars: int | None = None) -> None:
        self.max_chars = MAX_TEXT_CHARS if max_chars is None else max_chars
        self._parts: list[str] = []
        self._length = 0
        self.blocks: list[Block] = []
        self.pages: dict[int, Page] = {}
        self.warnings: list[ParseWarning] = []
        self.truncated = False
        self.truncated_reason: str | None = None
        self.hidden_removed = 0
        self._removed: Counter[str] = Counter()
        self._numbers: Counter[str] = Counter()

    # ── pages ──────────────────────────────────────────────────────────────────

    def page(self, number: int, *, width: float | None = None, height: float | None = None,
             quality: PageQuality | None = None) -> Page:
        page = Page(number=number, width=width, height=height, quality=quality or PageQuality())
        self.pages[number] = page
        return page

    def page_quality(self, number: int, **fields: Any) -> None:
        page = self.pages[number]
        self.pages[number] = page.model_copy(update={"quality": page.quality.model_copy(update=fields)})

    # ── blocks ─────────────────────────────────────────────────────────────────

    def add(self, kind: str, text: str, *, page: int | None = None, sheet_index: int | None = None,
            html_comments: bool = False, **fields: Any) -> Block | None:
        """Sanitize `text` and append it as one block; None when nothing visible is left or a cap is hit."""
        clean, found = sanitize_text(text, html_comments=html_comments)
        for warning in found:
            self._removed[warning.code] += warning.count
        clean = clean.strip()
        if not clean:
            return None
        if len(self.blocks) >= MAX_BLOCKS or self._length + len(SEPARATOR) + len(clean) > self.max_chars:
            self.cut("truncated_size")
            return None
        start = self._length + (len(SEPARATOR) if self._parts else 0)
        key = f"s{sheet_index}" if sheet_index is not None else f"b{page or 0:04d}"
        block = Block(id=f"{key}-{self._numbers[key]:04d}", kind=kind, text=clean,  # type: ignore[arg-type]
                      page=page, char_start=start, char_end=start + len(clean), **fields)
        self._numbers[key] += 1
        if self._parts:
            self._parts.append(SEPARATOR)
        self._parts.append(clean)
        self._length = block.char_end
        self.blocks.append(block)
        return block

    def cut(self, reason: str) -> None:
        """The rest of the document is not read: `reason` is `truncated_size` or `truncated_pages`."""
        if not self.truncated:
            self.truncated, self.truncated_reason = True, reason

    def warn(self, code: str, message: str, *, page: int | None = None, count: int = 1) -> None:
        self.warnings.append(ParseWarning(code=code, message=message, page=page, count=count))  # type: ignore[arg-type]

    def hidden(self, count: int = 1) -> None:
        """Content a reader of the file never sees (hidden text, a hidden sheet or slide) was left out."""
        self.hidden_removed += count

    # ── the result ─────────────────────────────────────────────────────────────

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def build(self, source: SourceInfo, parser: ParserInfo, **fields: Any) -> ParsedDocument:
        warnings = list(self.warnings)
        for code in _SANITIZE_CODES:
            if self._removed[code]:
                warnings.append(ParseWarning(code=code, count=self._removed[code], message=(  # type: ignore[arg-type]
                    f"{self._removed[code]} HTML comment(s) removed: a reader never sees them."
                    if code == "html_comments_removed" else
                    f"{self._removed[code]} invisible character(s) removed (zero-width, bidi controls or "
                    "control codes).")))
        if self.hidden_removed:
            warnings.append(ParseWarning(code="hidden_text_removed", count=self.hidden_removed, message=(
                f"{self.hidden_removed} piece(s) of hidden content left out: text, sheets or slides a reader "
                "of the file never sees.")))
        if self.truncated:
            warnings.append(ParseWarning(code=self.truncated_reason or "truncated_size", message=(  # type: ignore[arg-type]
                "The document was cut: the rest is not indexed." if self.truncated_reason != "truncated_pages"
                else "Only some pages were read; the rest are not indexed.")))
        if not self.blocks and not any(w.code == "empty_document" for w in warnings):
            warnings.append(ParseWarning(code="empty_document", message="The file has no text to index."))
        pages = [self._page_chars(p) for _, p in sorted(self.pages.items())]
        stats = ParseStats(
            pages=len(pages),
            pages_no_text_layer=sum(1 for p in pages if p.quality.no_text_layer),
            pages_ocr=sum(1 for p in pages if p.quality.ocr_applied),
            chars=self._length, tables=sum(1 for b in self.blocks if b.kind in ("table", "sheet")),
            images=sum(1 for b in self.blocks if b.kind == "image"),
            truncated=self.truncated, truncated_reason=self.truncated_reason if self.truncated else None)
        return ParsedDocument(source=source, parser=parser, text=self.text, pages=pages, blocks=self.blocks,
                              warnings=warnings, stats=stats, **fields)

    def _page_chars(self, page: Page) -> Page:
        chars = sum(len(b.text) for b in self.blocks if b.page == page.number and not b.furniture)
        return page.model_copy(update={"text_chars": chars})


def source_info(*, path: str, sha256: str, size: int, mime: str, source: str, url: str | None = None,
                fetched_at: str | None = None, etag: str | None = None, modified_at: str | None = None) -> SourceInfo:
    return SourceInfo(path=path, sha256=sha256, size_bytes=size, mime=mime, source=source,  # type: ignore[arg-type]
                      url=url, fetched_at=fetched_at, etag=etag, modified_at=modified_at)


# ── OCR text into a parsed document ────────────────────────────────────────────

def merge_ocr(doc: ParsedDocument, texts: dict[int, tuple[str, float | None]], *, engine: str,
              low_confidence: float = 0.5) -> ParsedDocument:
    """`doc` with OCR text added to the pages in `texts` ({page: (text, confidence)}).

    The document is rebuilt in page order: every existing block keeps its
    text and its id (ids are numbered per page, and OCR only adds to pages
    that had no text), and each read page gets its paragraphs after the blocks
    it already had. Every offset is recomputed, so the result passes the
    contract's validator.
    """
    builder = DocBuilder(max_chars=max(MAX_TEXT_CHARS, len(doc.text) + sum(len(t) for t, _ in texts.values()) + 1024))
    for page in doc.pages:
        builder.pages[page.number] = page
    by_page: dict[int | None, list[Block]] = {}
    for block in doc.blocks:
        by_page.setdefault(block.page, []).append(block)
    numbers = sorted({p.number for p in doc.pages} | set(texts))
    for number in [None, *numbers]:
        for block in by_page.get(number, []):
            _readd(builder, block)
        if number is None or number not in texts:
            continue
        text, confidence = texts[number]
        added = 0
        for paragraph in _paragraphs(text):
            if builder.add("paragraph", paragraph, page=number) is not None:
                added += 1
        if number not in builder.pages:
            builder.page(number)
        builder.page_quality(number, ocr_applied=added > 0, ocr_engine=engine, ocr_confidence=confidence)
        if confidence is not None and confidence < low_confidence:
            builder.warn("ocr_low_confidence", f"Page {number} was read by OCR with low confidence "
                                               f"({confidence:.0%}); its text may be wrong.", page=number)
    builder.warnings = [w for w in doc.warnings if w.code not in (*_SANITIZE_CODES, "hidden_text_removed",
                                                                 "empty_document", "truncated_size",
                                                                 "truncated_pages")] + builder.warnings
    builder.hidden_removed = sum(w.count for w in doc.warnings if w.code == "hidden_text_removed")
    for warning in doc.warnings:
        if warning.code in _SANITIZE_CODES:
            builder._removed[warning.code] += warning.count
    if doc.stats.truncated:
        builder.cut(doc.stats.truncated_reason or "truncated_size")
    fields = {k: getattr(doc, k) for k in ("title", "language", "doc_date", "metadata", "attachments")}
    return builder.build(doc.source, doc.parser, **fields)


def _readd(builder: DocBuilder, block: Block) -> None:
    """Append an already-sanitized block as it is (same id), at the end of the builder's text."""
    start = builder._length + (len(SEPARATOR) if builder._parts else 0)
    moved = block.model_copy(update={"char_start": start, "char_end": start + len(block.text)})
    key = block.id.rsplit("-", 1)[0]
    builder._numbers[key] = max(builder._numbers[key], int(block.id.rsplit("-", 1)[1]) + 1)
    if builder._parts:
        builder._parts.append(SEPARATOR)
    builder._parts.append(block.text)
    builder._length = moved.char_end
    builder.blocks.append(moved)


def _paragraphs(text: str) -> list[str]:
    """OCR text as paragraphs: split on blank lines, single newlines kept inside a paragraph."""
    out, current = [], []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip():
            current.append(line.strip())
        elif current:
            out.append("\n".join(current))
            current = []
    if current:
        out.append("\n".join(current))
    return out
