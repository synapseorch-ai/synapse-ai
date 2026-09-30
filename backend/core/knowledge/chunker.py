"""The chunker: a `ParsedDocument` cut into the passages Knowledge indexes and cites (B1a, D158).

It reads only the parsed tree (`core.knowledge.parsed`), never a file, so a
parser change and a chunker change are independent, and the parse cache means
re-chunking never parses again.

**One invariant, every mode:** a chunk's `content` is exactly
`text[char_start:char_end]` of the document it came from (an attachment's
chunks slice the attachment's own text). A citation's span therefore always
points at real characters, and the quote check compares against the same text.
Because content is one contiguous span, a chunk never crosses a furniture block
(a running header or footer) — it ends there.

**Modes** (`kb_settings.Chunking.mode`):

* `structure` (default) — sections by heading; blocks gather up to
  `target_tokens`; a block over `max_tokens` is split at sentences, then
  whitespace, then a hard cut; a table is its own chunk, never split under
  `max_tokens` (over it, split into row groups with the header carried in
  `context`, and a `table_too_large` warning); a sheet is a `sheet_card`.
* `fixed` — windows of `target_tokens` over the whole text.
* `page` — one chunk per page (split only past `max_tokens`).
* `none` — no chunking beyond the `max_tokens` cap.

`overlap_pct` repeats the end of a chunk at the start of the next when a size
cut, not a heading, separated them.

**Parent sections are virtual** (Naveen, D158): only leaf chunks are made.
A section is the run of a document's chunks that share a `heading_path`;
search expands a hit into it on read. A stored parent row would be found by
Postgres keyword search (its tsvector covers every row) and come back as a hit.

**Tokens are estimated**, not counted with a model tokenizer: one per CJK, kana
or hangul character, one per four characters of anything else. Deterministic,
dependency-free, and part of `CHUNKER_VERSION`.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Literal

from core.knowledge.chunks import Chunk
from core.knowledge.ids import chunk_id
from core.knowledge.kb_settings import Chunking
from core.knowledge.parsed import BBox, Block, ParsedDocument, ParseWarning
from core.knowledge.textsearch import normalize_language

#: Bump when a change would cut the same document differently. A snapshot
#: records `chunker_version(mode)`.
CHUNKER_VERSION = "1"

ATTACHMENT_KEY = "_attachment"
ATTACHMENT_SHA_KEY = "_attachment_sha256"

_WIDE = re.compile(
    "[ᄀ-ᇿ⺀-⿟　-ヿ㄀-ㇿ㐀-䶿一-鿿"
    "ꥠ-꥿가-퟿豈-﫿＀-￯\U00020000-\U0003134f]"
)
#: Where a piece may end, best first: a paragraph break, a sentence end, a line break.
_PARAGRAPH = re.compile(r"\n[ \t]*\n\s*")
_SENTENCE = re.compile(r"[.!?。！？][\"'”’)\]]*\s+")
_LINE = re.compile(r"\n\s*")
_SPACE = re.compile(r"\s+")

_BODY_KINDS = frozenset({"paragraph", "list", "code", "image", "caption", "footnote", "other"})


def chunker_version(mode: str) -> str:
    return f"{mode}-{CHUNKER_VERSION}"


def estimate_tokens(text: str) -> int:
    """One token per CJK/kana/hangul character, one per four characters of anything else."""
    if not text:
        return 0
    wide = len(_WIDE.findall(text))
    return wide + -(-(len(text) - wide) // 4)


@dataclass
class ChunkResult:
    chunks: list[Chunk]
    warnings: list[ParseWarning] = field(default_factory=list)


@dataclass
class _Piece:
    """A span of one document's text, before it becomes a `Chunk`."""

    start: int
    end: int
    kind: Literal["text", "table", "sheet_card"] = "text"
    context: str = ""


class _Text:
    """One document's canonical text, with what the splitter needs: token estimates over any
    span in O(1) and the boundaries a piece may end at."""

    def __init__(self, doc: ParsedDocument):
        self.doc = doc
        self.text = doc.text
        wide = [0] * (len(self.text) + 1)
        for i, ch in enumerate(self.text):
            wide[i + 1] = wide[i] + (1 if _WIDE.match(ch) else 0)
        self._wide = wide
        self._bounds: dict[str, list[int]] = {}

    def tokens(self, start: int, end: int) -> int:
        if end <= start:
            return 0
        wide = self._wide[end] - self._wide[start]
        return wide + -(-(end - start - wide) // 4)

    def bounds(self, kind: str) -> list[int]:
        """Sorted positions just after a boundary of `kind` (where the next piece may start)."""
        if kind not in self._bounds:
            pattern = {"paragraph": _PARAGRAPH, "sentence": _SENTENCE, "line": _LINE, "space": _SPACE}[kind]
            self._bounds[kind] = [m.end() for m in pattern.finditer(self.text)]
        return self._bounds[kind]

    def trim(self, start: int, end: int) -> tuple[int, int]:
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        return start, end

    def _last_within(self, start: int, end: int, budget: int) -> int | None:
        """The boundary in (start, end) to cut at so [start, b) stays within `budget` tokens.

        The strongest kind of boundary wins when it keeps at least half the budget
        (a paragraph break over a sentence end over a line break over a space);
        otherwise the furthest boundary of any kind.
        """
        furthest = None
        for kind in ("paragraph", "sentence", "line", "space"):
            positions = self.bounds(kind)
            lo = bisect.bisect_right(positions, start)
            hi = bisect.bisect_left(positions, end)
            best = None
            for pos in positions[lo:hi]:
                if self.tokens(start, pos) > budget:
                    break
                best = pos
            if best is not None:
                if self.tokens(start, best) * 2 >= budget:
                    return best
                furthest = best if furthest is None else max(furthest, best)
        return furthest

    def _hard_cut(self, start: int, end: int, budget: int) -> int:
        lo, hi = start + 1, end
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.tokens(start, mid) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return max(lo, start + 1)

    def overlap_start(self, start: int, end: int, overlap: int) -> int:
        """Where the next piece starts so it repeats at most `overlap` tokens of [start, end)."""
        if overlap <= 0:
            return end
        for kind in ("sentence", "line", "space"):
            positions = self.bounds(kind)
            lo = bisect.bisect_right(positions, start)
            hi = bisect.bisect_left(positions, end)
            for pos in positions[lo:hi]:
                if self.tokens(pos, end) <= overlap:
                    return pos
        return end

    def split(self, start: int, end: int, target: int, cap: int, overlap: int) -> list[tuple[int, int]]:
        """[start, end) cut into spans of about `target` tokens, none over `cap`.

        A remainder that fits under `cap` is not cut when cutting would leave a tail
        under a quarter of `target`.
        """
        spans: list[tuple[int, int]] = []
        start, end = self.trim(start, end)
        while start < end:
            remaining = self.tokens(start, end)
            if remaining <= target:
                spans.append((start, end))
                break
            cut = self._last_within(start, end, target) or self._last_within(start, end, cap)
            if cut is None or cut <= start:
                cut = self._hard_cut(start, end, cap)
            if remaining <= cap and self.tokens(cut, end) * 4 < target:
                spans.append((start, end))
                break
            piece = self.trim(start, cut)
            if piece[1] > piece[0]:
                spans.append(piece)
                nxt = self.overlap_start(piece[0], piece[1], overlap)
                start = nxt if piece[0] < nxt < cut else cut
            else:
                start = cut
            start, end = self.trim(start, end)
        return spans


class _Chunker:
    def __init__(self, settings: Chunking):
        self.settings = settings
        self.target = settings.target_tokens
        self.cap = settings.max_tokens
        self.overlap = int(round(settings.target_tokens * settings.overlap_pct))
        self.warnings: list[ParseWarning] = []

    # ── modes ──────────────────────────────────────────────────────────────────

    def pieces(self, doc: _Text) -> list[_Piece]:
        mode = self.settings.mode
        if mode == "fixed":
            return [_Piece(s, e) for s, e in doc.split(0, len(doc.text), self.target, self.cap, self.overlap)]
        if mode == "page":
            return self._runs(doc, by_page=True)
        if mode == "none":
            return self._runs(doc, by_page=False)
        return self._structure(doc)

    def _runs(self, doc: _Text, *, by_page: bool) -> list[_Piece]:
        """Consecutive non-furniture blocks (on one page, for `page`), split only past the cap."""
        out: list[_Piece] = []
        run: list[Block] = []

        def flush() -> None:
            if run:
                for s, e in doc.split(run[0].char_start, run[-1].char_end, self.cap, self.cap, 0):
                    out.append(_Piece(s, e))
                run.clear()

        for block in doc.doc.blocks:
            if block.furniture:
                flush()
                continue
            if by_page and run and block.page != run[-1].page:
                flush()
            run.append(block)
        flush()
        if not doc.doc.blocks and doc.text.strip():
            out = [_Piece(s, e) for s, e in doc.split(0, len(doc.text), self.cap, self.cap, 0)]
        return out

    def _structure(self, doc: _Text) -> list[_Piece]:
        out: list[_Piece] = []
        group: list[Block] = []  # the chunk being gathered: pending headings, then body blocks
        body = False             # whether `group` holds anything but headings
        carried: int | None = None  # where a chunk starts when it repeats its predecessor's end

        def start_of() -> int:
            return carried if carried is not None else group[0].char_start

        def flush(*, size_cut: bool = False) -> None:
            nonlocal body, carried
            nxt = None
            if group:
                s, e = doc.trim(start_of(), group[-1].char_end)
                if e > s:
                    out.append(_Piece(s, e))
                    nxt = doc.overlap_start(s, e, self.overlap) if size_cut else None
                    nxt = nxt if nxt is not None and nxt < e else None
            group.clear()
            body = False
            carried = nxt

        for block in doc.doc.blocks:
            if block.furniture:  # a chunk is one contiguous span: it ends at a running header or footer
                flush()
                carried = None
                continue
            if block.kind == "heading":
                if body:
                    flush()
                if not group:
                    carried = None  # a new section never repeats the last one's end
                group.append(block)
                continue
            if block.kind in ("table", "sheet"):
                if body:
                    flush()
                start = group[0].char_start if group else block.char_start  # pending headings go with it
                out.extend(self._table(doc, block, start))
                group.clear()
                body, carried = False, None
                continue
            if body and block.heading_path != group[-1].heading_path:
                flush()
                carried = None
            elif body and doc.tokens(start_of(), block.char_end) > self.target:
                flush(size_cut=True)
            if doc.tokens(block.char_start, block.char_end) > self.cap:
                start = group[0].char_start if group else block.char_start
                out.extend(_Piece(s, e) for s, e in doc.split(start, block.char_end, self.target, self.cap,
                                                              self.overlap))
                group.clear()
                body, carried = False, None
                continue
            if not group and carried is not None and carried >= block.char_start:
                carried = None
            group.append(block)
            body = True
        flush()
        if not doc.doc.blocks and doc.text.strip():
            out = [_Piece(s, e) for s, e in doc.split(0, len(doc.text), self.target, self.cap, self.overlap)]
        return out

    def _table(self, doc: _Text, block: Block, start: int) -> list[_Piece]:
        kind: Literal["table", "sheet_card"] = "sheet_card" if block.kind == "sheet" else "table"
        if doc.tokens(start, block.char_end) <= self.cap:
            s, e = doc.trim(start, block.char_end)
            return [_Piece(s, e, kind)] if e > s else []
        self.warnings.append(ParseWarning(
            code="table_too_large", block_id=block.id, page=block.page,
            message=f"A {'sheet' if kind == 'sheet_card' else 'table'} of about "
                    f"{doc.tokens(block.char_start, block.char_end)} tokens was split into row groups of at most "
                    f"{self.cap}; its header is repeated in each group's context.",
        ))
        lines = _line_spans(doc.text, block.char_start, block.char_end)
        header_rows = block.table.header_rows if block.table and block.table.header_rows else 1
        header = " / ".join(doc.text[s:e] for s, e in lines[:header_rows])
        context = f"Table columns: {header}" if header else ""
        pieces: list[_Piece] = []
        run_start = start
        run_end = None
        for s, e in lines:
            if run_end is not None and doc.tokens(run_start, e) > self.target:
                pieces.append(_Piece(*doc.trim(run_start, run_end), kind))
                run_start = s
            if doc.tokens(s, e) > self.cap:  # one row past the cap: cut it like text
                if run_end is not None and run_start < s:
                    pieces.append(_Piece(*doc.trim(run_start, run_end), kind))
                for a, b in doc.split(s, e, self.target, self.cap, 0):
                    pieces.append(_Piece(a, b, kind))
                run_start, run_end = e, None
                continue
            run_end = e
        if run_end is not None and run_end > run_start:
            pieces.append(_Piece(*doc.trim(run_start, run_end), kind))
        for i, piece in enumerate(pieces):
            if i:
                piece.context = context
        return [p for p in pieces if p.end > p.start]


def _line_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans = []
    pos = start
    for line in text[start:end].split("\n"):
        if line.strip():
            spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def _describe(doc: ParsedDocument, start: int, end: int) -> tuple[list[Block], int | None, int | None]:
    """The blocks a span touches, and its first and last page."""
    blocks = [b for b in doc.blocks if b.char_start < end and b.char_end > start and not b.furniture]
    if not blocks:
        blocks = [b for b in doc.blocks if b.char_start < end and b.char_end > start]
    pages = [p for b in blocks for p in (b.page, b.page_end) if p is not None]
    return blocks, (min(pages) if pages else None), (max(pages) if pages else None)


def _heading_path(blocks: list[Block]) -> list[str]:
    for block in blocks:
        if block.kind != "heading" and block.heading_path:
            return list(block.heading_path)
    return list(blocks[-1].heading_path) if blocks else []


def chunk_document(parsed: ParsedDocument, *, doc_id: str, kb_id: str, snapshot_id: str, doc_version: int,
                   settings: Chunking | None = None, lang: str | None = None) -> ChunkResult:
    """`parsed` cut into leaf chunks, in reading order, attachments after the body.

    Ids are `ids.chunk_id(doc_id, snapshot_id, doc_version, ordinal)`, so the same
    document version chunked again with the same settings gets the same ids.
    """
    settings = settings or Chunking()
    chunker = _Chunker(settings)
    language = normalize_language(lang)
    chunks: list[Chunk] = []

    def walk(doc: ParsedDocument, prefix: list[str], extra: dict) -> None:
        text = _Text(doc)
        for piece in chunker.pieces(text):
            blocks, page_start, page_end = _describe(doc, piece.start, piece.end)
            boxes: list[BBox] = []
            for block in blocks:
                for box in block.bbox or []:
                    if box not in boxes:
                        boxes.append(box)
            content = doc.text[piece.start:piece.end]
            ordinal = len(chunks)
            chunks.append(Chunk(
                id=chunk_id(doc_id, snapshot_id, doc_version, ordinal), doc_id=doc_id, kb_id=kb_id,
                snapshot_id=snapshot_id, doc_version=doc_version, doc_sha256=parsed.source.sha256,
                ordinal=ordinal, kind=piece.kind, role="leaf", content=content, context=piece.context,
                heading_path=prefix + _heading_path(blocks), page_start=page_start, page_end=page_end,
                char_start=piece.start, char_end=piece.end, bbox=boxes or None,
                tokens=estimate_tokens(content), lang=language, block_ids=[b.id for b in blocks],
                metadata=dict(extra),
            ))
        for attachment in doc.attachments:
            if attachment.parsed is None:
                continue
            name = f"{extra[ATTACHMENT_KEY]}/{attachment.name}" if ATTACHMENT_KEY in extra else attachment.name
            walk(attachment.parsed, prefix + [attachment.name],
                 {ATTACHMENT_KEY: name, ATTACHMENT_SHA_KEY: attachment.sha256})

    walk(parsed, [], {})
    warnings = list(chunker.warnings)
    if not chunks:
        warnings.append(ParseWarning(code="empty_document", message="The document holds no text to index."))
    return ChunkResult(chunks=chunks, warnings=warnings)


def section_key(chunk: Chunk) -> tuple:
    """Which virtual parent section a chunk belongs to: its document, its attachment, its heading path."""
    return (chunk.doc_id, chunk.metadata.get(ATTACHMENT_KEY), tuple(chunk.heading_path))
