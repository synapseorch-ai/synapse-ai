"""PDF: Docling when it is installed with its models (`docling_adapter.py`), else this basic reader.

**The basic reader** (pdfplumber over pdfminer; pypdfium2 counts, opens and
renders) reads a page at a time, then lays the document out:

* **Hidden text is left out**, counted in one `hidden_text_removed` warning:
  characters under 1 pt, characters off the page, and white characters with
  nothing filled beneath them (white-on-white is how a prompt hides in a PDF).
  White text on a dark box or an image is visible and kept.
* **Tables** that are ruled (`find_tables`) become table blocks, their text
  kept out of the paragraphs around them.
* **Headings** by font size against the body text's size, larger first;
  a short bold line alone is the lowest heading level.
* **Running headers, footers and page numbers** — a line in the top or bottom
  band that repeats on half the pages — are `furniture`: kept, never embedded.
* **Two columns** are read left column, then right, between full-width lines.
* **The quality gate:** a page with no visible characters but an image (or
  much drawn vector text) has no text layer; so does one whose text is mostly
  `(cid:…)` glyphs with no Unicode mapping. Either is reported, and rendered
  for OCR when a provider is set (`ctx.ocr`).

A page that cannot be read is reported (`truncated_pages`, with its page and
error) and the rest is read. A file past `max_pages` is refused before any page
is parsed; a password-protected one is `encrypted`.
"""
from __future__ import annotations

import io
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from core.knowledge.parse import limits
from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import Encrypted, OverPageCap, ParseRefused
from core.knowledge.parse.tree import DocBuilder, Headings, no_text_layer_warning
from core.knowledge.parsed import BBox, ParsedDocument, ParserInfo, TableData

PARSER = ParserInfo(name="pdf", version="1")

#: A character smaller than this, in points, is not text a reader can see.
MIN_VISIBLE_PT = 1.0
#: Over this share of `(cid:n)` / replacement / private-use characters, a page's text layer is junk.
GARBLED_RATIO = 0.3
#: The top and bottom bands where running headers and footers live, as a share of the page height.
FURNITURE_BAND = 0.08
#: A wider band for a line that is only a page number: some layouts set the folio well above the margin
#: (NIST AI 100-1's sits at 89% of the page). Wider for repeated lines would take "Section 3" at the top
#: of every page for a running header.
PAGE_NUMBER_BAND = 0.12
#: Vector paths on a page with no characters that suggest text drawn as outlines.
OUTLINED_TEXT_CURVES = 50

_PAGE_NUMBER = re.compile(r"^\W*(?i:page\s*)?\d{1,4}(\s*(?i:of|/)\s*\d{1,4})?\W*$")
#: A front-matter page number: a valid lowercase Roman numeral (i, iv, xii), alone on its line.
_ROMAN_PAGE = re.compile(r"^\W*(?=[ivxlc])(x{0,3}|xl|l?x{0,3}|xc|c{0,3})(ix|iv|v?i{0,3})\W*$")
_BULLET = re.compile(r"^\s*([•◦▪▫●○■□‣⁃∙·\-–—*]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])\s+")
_GARBLE = re.compile(r"\(cid:\d+\)|[�-]")


@dataclass
class _Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool


@dataclass
class _Table:
    rows: list[list[str]]
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class _Page:
    number: int
    width: float
    height: float
    lines: list[_Line] = field(default_factory=list)
    tables: list[_Table] = field(default_factory=list)
    hidden: int = 0
    no_text_layer: bool = False
    image_only: bool = False
    garbled_ratio: float | None = None
    error: str | None = None


def open_pdf(data: bytes) -> Any:
    """The PDF as a pypdfium2 document. Raises `Encrypted`, or `ParseRefused` for a damaged file."""
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise Encrypted("The PDF is protected by a password.") from exc
        raise ParseRefused(f"The PDF is damaged and cannot be opened: {exc}.") from exc


def render_png(document: Any, number: int, *, dpi: int = limits.OCR_DPI) -> bytes:
    """Page `number` (1-based) as a grayscale PNG, for OCR."""
    page = document[number - 1]
    try:
        image = page.render(scale=dpi / 72, grayscale=True).to_pil()
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=False)
        return buf.getvalue()
    finally:
        page.close()


def read_pdf(data: bytes, ctx: ReadContext) -> ParsedDocument:
    document = open_pdf(data)
    try:
        total = len(document)
        if ctx.max_pages and total > ctx.max_pages:
            raise OverPageCap(total, ctx.max_pages)
        if ctx.docling:
            from core.knowledge.parse.docling_adapter import read_pdf_docling

            return read_pdf_docling(data, ctx, document)
        return _read_basic(data, ctx, document, total)
    finally:
        document.close()


def _read_basic(data: bytes, ctx: ReadContext, document: Any, total: int) -> ParsedDocument:
    import pdfplumber

    pages: list[_Page] = []
    with pdfplumber.open(io.BytesIO(data)) as plumb:
        meta = dict(plumb.metadata or {})
        for number in range(1, total + 1):
            page = plumb.pages[number - 1]
            try:
                pages.append(_read_page(page, number))
            except Exception as exc:  # noqa: BLE001 — one broken page never loses the others
                pages.append(_Page(number=number, width=float(page.width or 0), height=float(page.height or 0),
                                   error=f"{type(exc).__name__}: {exc}"[:300]))
            finally:
                page.close()
            if ctx.ocr and ctx.render is not None and pages[-1].no_text_layer:
                ctx.render(number, render_png(document, number))
            ctx.report_pages(number, total)
            if number < total:
                ctx.save_progress(lambda: assemble(pages, ctx, meta, total=total))
    return assemble(pages, ctx, meta, total=total)


# ── one page ───────────────────────────────────────────────────────────────────

def _colour_is_white(colour: Any) -> bool:
    if colour is None:
        return False
    values = colour if isinstance(colour, (tuple, list)) else (colour,)
    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):
        return False  # a pattern or a named colour space: assume visible
    if len(values) == 1:
        return values[0] >= 0.97
    if len(values) == 3:
        return min(values) >= 0.97
    if len(values) == 4:  # CMYK: white is no ink at all
        return max(values) <= 0.03
    return False


def _box(obj: dict) -> tuple[float, float, float, float]:
    return float(obj["x0"]), float(obj["top"]), float(obj["x1"]), float(obj["bottom"])


def _inside(obj: dict, box: tuple[float, float, float, float], *, pad: float = 1.0) -> bool:
    cx, cy = (float(obj["x0"]) + float(obj["x1"])) / 2, (float(obj["top"]) + float(obj["bottom"])) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def _hidden_test(page: Any):
    """A predicate for characters a reader of the page never sees."""
    x0, top, x1, bottom = (float(v) for v in page.bbox)
    filled = [_box(o) for o in (*page.rects, *page.curves) if o.get("fill")
              and not _colour_is_white(o.get("non_stroking_color"))]
    filled += [_box(o) for o in page.images]

    def hidden(ch: dict) -> bool:
        if float(ch.get("size") or 0) < MIN_VISIBLE_PT:
            return True
        if float(ch["x1"]) < x0 or float(ch["x0"]) > x1 or float(ch["bottom"]) < top or float(ch["top"]) > bottom:
            return True
        if _colour_is_white(ch.get("non_stroking_color")):
            return not any(_inside(ch, box) for box in filled)
        return False

    return hidden


@dataclass
class CharLayer:
    """A page's characters as a reader sees them: the boxes of the visible ones, the hidden runs
    (text and box), and whether the page has images or much vector drawing. Docling's path uses it
    too: it is the only view of colour and size, which Docling does not keep."""

    visible: list[tuple[float, float, float, float]]
    hidden: list[tuple[str, tuple[float, float, float, float]]]
    images: bool
    curves: int

    def visible_in(self, box: tuple[float, float, float, float]) -> int:
        x0, top, x1, bottom = box
        return sum(1 for b in self.visible
                   if x0 - 1 <= (b[0] + b[2]) / 2 <= x1 + 1 and top - 1 <= (b[1] + b[3]) / 2 <= bottom + 1)


def char_layer(page: Any) -> CharLayer:
    hidden_test = _hidden_test(page)
    visible, runs = [], []
    run_text, run_box, last_top = "", None, None
    for ch in page.chars:
        box = _box(ch)
        if hidden_test(ch):
            if run_box is not None and last_top is not None and abs(box[1] - last_top) <= 1:
                run_text += ch.get("text", "")
                run_box = (min(run_box[0], box[0]), min(run_box[1], box[1]), max(run_box[2], box[2]),
                           max(run_box[3], box[3]))
            else:
                if run_box is not None:
                    runs.append((run_text, run_box))
                run_text, run_box = ch.get("text", ""), box
            last_top = box[1]
        else:
            if run_box is not None:
                runs.append((run_text, run_box))
                run_text, run_box = "", None
            if str(ch.get("text", "")).strip():
                visible.append(box)
    if run_box is not None:
        runs.append((run_text, run_box))
    return CharLayer(visible=visible, hidden=[r for r in runs if r[0].strip()], images=bool(page.images),
                     curves=len(page.curves))


def _read_page(page: Any, number: int) -> _Page:
    out = _Page(number=number, width=float(page.width), height=float(page.height))
    hidden = _hidden_test(page)
    chars = page.chars
    flags = [hidden(ch) for ch in chars]
    out.hidden = sum(1 for k, f in enumerate(flags) if f and (  # hidden runs, one per line they sit on
        k == 0 or not flags[k - 1] or abs(float(chars[k]["top"]) - float(chars[k - 1]["top"])) > 1))
    visible = page.filter(lambda o: o.get("object_type") != "char" or not hidden(o))

    boxes = []
    for table in visible.find_tables():
        rows = [[(cell or "").strip() for cell in row] for row in table.extract()]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue  # one ruled row or column is a box around text, not a table
        x0, top, x1, bottom = (float(v) for v in table.bbox)
        out.tables.append(_Table(rows=rows, x0=x0, x1=x1, top=top, bottom=bottom))
        boxes.append((x0, top, x1, bottom))
    prose = visible.filter(lambda o: o.get("object_type") != "char" or not any(_inside(o, b) for b in boxes))

    out.lines = _lines(prose.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False,
                                           use_text_flow=False, extra_attrs=["size", "fontname"]))
    chars = sum(len(line.text) for line in out.lines) + sum(len(c) for t in out.tables for r in t.rows for c in r)

    if chars:
        out.garbled_ratio = garbled_ratio("".join(line.text for line in out.lines))
        if out.garbled_ratio > GARBLED_RATIO:  # glyphs with no Unicode: as good as no text layer
            out.lines, out.tables, out.no_text_layer = [], [], True
    elif page.images or len(page.curves) >= OUTLINED_TEXT_CURVES:
        out.no_text_layer = True
        out.image_only = bool(page.images)
    return out


def _lines(words: list[dict]) -> list[_Line]:
    """Words into lines: one baseline, split where a gap is wider than two em (a column gutter, an
    unruled table's columns). pdfplumber's own lines join everything on a baseline across columns."""
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (float(w["bottom"]), float(w["x0"]))):
        size = float(word.get("size") or 10)
        if rows and abs(float(rows[-1][-1]["bottom"]) - float(word["bottom"])) <= size * 0.35:
            rows[-1].append(word)
        else:
            rows.append([word])
    out: list[_Line] = []
    for row in rows:
        row.sort(key=lambda w: float(w["x0"]))
        segment = [row[0]]
        for word in row[1:]:
            size = float(word.get("size") or 10)
            if float(word["x0"]) - float(segment[-1]["x1"]) > 2.0 * size:
                out.append(_line(segment))
                segment = []
            segment.append(word)
        out.append(_line(segment))
    return sorted(out, key=lambda ln: (round(ln.top), ln.x0))


def _line(words: list[dict]) -> _Line:
    weights = [len(w["text"]) for w in words]
    sizes = [float(w.get("size") or 0) for w in words]
    bold = sum(n for n, w in zip(weights, words, strict=True) if "bold" in str(w.get("fontname", "")).lower())
    return _Line(text=" ".join(w["text"] for w in words), x0=float(words[0]["x0"]), x1=float(words[-1]["x1"]),
                 top=min(float(w["top"]) for w in words), bottom=max(float(w["bottom"]) for w in words),
                 size=round(statistics.median(sizes) * 2) / 2, bold=bold >= 0.6 * sum(weights))


def garbled_ratio(text: str) -> float:
    """The share of `text` that is glyphs with no Unicode mapping: `(cid:n)`, U+FFFD, private use."""
    garbled = len("".join(_GARBLE.findall(text)))
    return round(garbled / max(1, len(text)), 3)


# ── the document ───────────────────────────────────────────────────────────────

def assemble(pages: list[_Page], ctx: ReadContext, meta: dict, *, total: int) -> ParsedDocument:
    """Every page read so far, laid out as one document. Fewer than `total` pages: a checkpoint,
    marked cut (`truncated_pages`) — what a timeout keeps."""
    builder = DocBuilder()
    sizes = Counter()
    for page in pages:
        for line in page.lines:
            sizes[line.size] += len(line.text)
    body = sizes.most_common(1)[0][0] if sizes else 0.0
    levels = {s: k + 1 for k, s in enumerate(sorted((s for s in sizes if body and s >= body * 1.15),
                                                    reverse=True)[:5])}
    furniture = _furniture(pages)
    headings = Headings()
    title = None

    for page in pages:
        builder.page(page.number, width=page.width, height=page.height)
        if page.error:
            builder.warn("truncated_pages", f"Page {page.number} could not be read: {page.error}", page=page.number)
            continue
        if page.no_text_layer or page.garbled_ratio is not None:
            builder.page_quality(page.number, no_text_layer=page.no_text_layer, image_only=page.image_only,
                                 garbled_ratio=page.garbled_ratio)
        if page.garbled_ratio is not None and page.garbled_ratio > GARBLED_RATIO:
            builder.warn("garbled_text", f"Page {page.number}'s text layer has no readable character mapping; "
                                         "it is treated as an image.", page=page.number)
        builder.hidden(page.hidden)
        for item in _ordered(page, body):
            if isinstance(item, _Table):
                builder.add("table", "\n".join(" | ".join(r) for r in item.rows), page=page.number,
                            heading_path=headings.path, bbox=[_bbox(page, item)],
                            table=TableData(rows=_square(item.rows), header_rows=1, n_rows=len(item.rows),
                                            n_cols=max(len(r) for r in item.rows)))
                continue
            kind, lines = item
            text = _join(lines) if kind != "list" else "\n".join(line.text for line in lines)
            box = [_bbox(page, _span(lines))]
            if kind == "furniture" or (kind != "heading" and _key(text) in furniture):
                builder.add("other", text, page=page.number, furniture=True, bbox=box)
            elif kind == "heading":
                level = levels.get(lines[0].size, len(levels) + 1)
                block = builder.add("heading", text, page=page.number, level=level,
                                    heading_path=headings.push(level, text), bbox=box)
                if block is not None and title is None and page.number == pages[0].number and level == 1:
                    title = block.text
            elif kind == "list":
                builder.add("list", text, page=page.number, heading_path=headings.path, bbox=box,
                            items=[_BULLET.sub("", line.text, count=1) for line in lines])
            else:
                builder.add("paragraph", text, page=page.number, heading_path=headings.path, bbox=box)

    unread = [p.number for p in pages if p.no_text_layer]
    if unread:
        builder.warnings.append(no_text_layer_warning(unread, ocr=False))
    if len(pages) < total:
        builder.cut("truncated_pages", f"Pages 1–{len(pages)} of {total} were read; the rest were not.")
    return builder.build(ctx.source, PARSER, title=_title(meta) or title, doc_date=_date(meta))


def _ordered(page: _Page, body: float) -> list[Any]:
    """The page's tables and line groups in reading order: (kind, lines) or a `_Table`.
    A table sits before the first group that starts below its top."""
    tables = sorted(page.tables, key=lambda t: t.top)
    out: list[Any] = []
    for group in _groups(_reading_order(page), page, body):
        while tables and tables[0].top <= group[1][0].top:
            out.append(tables.pop(0))
        out.append(group)
    return out + tables


def _reading_order(page: _Page) -> list[_Line]:
    """Lines top to bottom; on a two-column page, the left column then the right between full-width lines."""
    lines = sorted(page.lines, key=lambda ln: (round(ln.top), ln.x0))
    gutter = _gutter(lines, page.width)
    if gutter is None:
        return lines
    out, left, right = [], [], []
    for line in lines:
        if line.x0 < gutter < line.x1:  # spans both columns: a title, a full-width figure caption
            out.extend(left + right)
            left, right = [], []
            out.append(line)
        elif line.x1 <= gutter:
            left.append(line)
        else:
            right.append(line)
    return out + left + right


def _gutter(lines: list[_Line], width: float) -> float | None:
    """The x of a gap between two columns, if the page has them."""
    if len(lines) < 8 or width <= 0:
        return None
    narrow = [ln for ln in lines if ln.x1 - ln.x0 < width * 0.55]
    left = [ln for ln in narrow if ln.x1 <= width * 0.6 and ln.x0 < width * 0.4]
    right = [ln for ln in narrow if ln.x0 >= width * 0.4]
    if len(left) < 4 or len(right) < 4 or len(left) + len(right) < 0.6 * len(lines):
        return None
    edge_left, edge_right = max(ln.x1 for ln in left), min(ln.x0 for ln in right)
    if edge_right - edge_left < 6:
        return None
    gutter = (edge_left + edge_right) / 2
    crossing = sum(1 for ln in lines if ln.x0 < gutter < ln.x1)
    return gutter if crossing <= 0.25 * len(lines) else None


def _groups(lines: list[_Line], page: _Page, body: float):
    """Consecutive lines as headings, list items and paragraphs. `body` is the document's body text size."""
    out: list[tuple[str, list[_Line]]] = []

    def kind_of(line: _Line) -> str:
        band = page.height * PAGE_NUMBER_BAND
        if (line.top < band or line.bottom > page.height - band) and (
                _PAGE_NUMBER.match(line.text) or _ROMAN_PAGE.match(line.text)):
            return "furniture"
        if body and line.size >= body * 1.15 and len(line.text) <= 200:
            return "heading"
        if line.bold and len(line.text) <= 80 and not line.text.rstrip().endswith((".", ",", ";", ":")):
            return "heading"
        if _BULLET.match(line.text):
            return "list"
        return "paragraph"

    for line in lines:
        kind = kind_of(line)
        if out:
            prev_kind, prev = out[-1]
            last = prev[-1]
            gap = line.top - last.bottom
            close = -2 <= gap <= max(last.size, line.size) * 0.9
            if prev_kind == kind == "heading" and close and line.size == last.size:
                prev.append(line)
                continue
            if prev_kind == "list" and kind == "paragraph" and close and line.x0 > prev[0].x0 + 2:
                prev.append(line)  # a wrapped list item: indented under its bullet
                continue
            if prev_kind == kind == "list" and close:
                prev.append(line)
                continue
            if prev_kind == kind == "paragraph" and close and abs(line.size - last.size) < 1:
                prev.append(line)
                continue
        out.append((kind, [line]))
    return out


def _join(lines: list[_Line]) -> str:
    text = ""
    for line in lines:
        if not text:
            text = line.text
        elif text.endswith("-") and line.text[:1].islower():
            text = text[:-1] + line.text  # inter- / national
        else:
            text += " " + line.text
    return text


def _span(lines: list[_Line]) -> _Line:
    return _Line(text="", x0=min(ln.x0 for ln in lines), x1=max(ln.x1 for ln in lines),
                 top=min(ln.top for ln in lines), bottom=max(ln.bottom for ln in lines), size=0, bold=False)


def _bbox(page: _Page, item: Any) -> BBox:
    return BBox(page=page.number, x0=round(item.x0, 2), y0=round(item.top, 2), x1=round(item.x1, 2),
                y1=round(item.bottom, 2))


def _square(rows: list[list[str]]) -> list[list[str]]:
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def _key(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())


def _furniture(pages: list[_Page]) -> set[str]:
    """Lines in the top or bottom band that repeat on at least half the pages (and on two or more).

    A line repeats when its text is the same, or when only its number changes and that number keeps
    step with the page ("Q3 report — 7" on page 7, "— 8" on page 8: a folio inside a header). A
    number that does not keep step ("Section 2", "Section 5" at the top of pages 2 and 3) is content.
    """
    seen: dict[str, list[tuple[int, str]]] = {}
    readable = [p for p in pages if p.lines]
    for page in readable:
        band = page.height * FURNITURE_BAND
        for line in page.lines:
            if line.top < band or line.bottom > page.height - band:
                key = _key(line.text)
                if key and (page.number, line.text) not in seen.get(key, []):
                    seen.setdefault(key, []).append((page.number, line.text.strip()))
    need = max(2, (len(readable) + 1) // 2)
    out = set()
    for key, hits in seen.items():
        if len({n for n, _ in hits}) < need:
            continue
        texts = {t for _, t in hits}
        if len(texts) == 1:
            out.add(key)
            continue
        numbers = [re.findall(r"\d+", t) for _, t in hits]
        if all(len(ns) == 1 for ns in numbers) and len({int(ns[0]) - n for ns, (n, _) in zip(numbers, hits,
                                                                                            strict=True)}) == 1:
            out.add(key)
    return out


def _title(meta: dict) -> str | None:
    value = meta.get("Title")
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    value = re.sub(r"^Microsoft (Word|PowerPoint|Excel) - ", "", value.strip())
    value = re.sub(r"\.(docx?|pptx?|xlsx?)$", "", value, flags=re.IGNORECASE)
    return value if value and value.lower() not in ("untitled", "title") else None


def _date(meta: dict) -> str | None:
    value = meta.get("CreationDate") or meta.get("ModDate")
    if isinstance(value, bytes):
        value = value.decode("latin-1", errors="replace")
    match = re.match(r"^(?:D:)?(\d{4})(\d{2})(\d{2})", str(value or ""))
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None
