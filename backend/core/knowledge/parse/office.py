"""Word, PowerPoint and Excel (OOXML), read by our own readers — Docling or not (D162 #1).

An Office file already stores its structure: heading styles, list numbering,
tables, slide titles, sheets. Reading that XML directly gives the same
structure Docling's rule-based Office backends give, in OSS and cloud alike,
and lets the reader see what a person never does:

* **Word:** hidden runs (`w:vanish`), white text with no shading or
  highlight, and text under 1 pt are left out and counted. Deleted tracked
  changes and field codes are not text; inserted changes are. Text boxes are
  read once (not again from their compatibility fallback). Footnotes and
  endnotes follow the body.
* **PowerPoint:** a slide is a page; its title is a heading; body
  placeholders are lists; tables are tables; speaker notes are indexed under
  the slide (the author sees them). Hidden slides and hidden shapes are left
  out and counted (D162 #3). White text is kept: a dark slide is common.
* **Excel:** each sheet is a sheet card plus row groups (`sheets.py`), read
  with openpyxl in read-only mode from cached values. Hidden sheets are left
  out and counted (D162 #3).

Every zip passes `SafeZip` first; every XML part goes through defusedxml.
"""
from __future__ import annotations

import io
import posixpath
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from xml.etree.ElementTree import Element

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import OverPageCap
from core.knowledge.parse.sheets import ROW_GROUP_MAX_ROWS, sheet_parts
from core.knowledge.parse.tree import DocBuilder, Headings
from core.knowledge.parse.xmlread import (
    attr, child, children, descendants, find, is_true, local, parse, relationship_id,
)
from core.knowledge.parse.zipguard import SafeZip
from core.knowledge.parsed import ParsedDocument, ParserInfo, TableData

DOCX_PARSER = ParserInfo(name="docx", version="1")
PPTX_PARSER = ParserInfo(name="pptx", version="1")
XLSX_PARSER = ParserInfo(name="xlsx", version="1")

#: Run text in these is not what a reader sees: deleted text, field codes, a fallback copy.
_NOT_TEXT = frozenset({"delText", "instrText", "del", "moveFrom", "Fallback", "txbxContent", "footnoteReference",
                       "endnoteReference", "commentReference", "rPr", "pPr"})


# ── metadata ───────────────────────────────────────────────────────────────────

def core_properties(z: SafeZip) -> dict[str, str]:
    """`docProps/core.xml`: title, created date, language."""
    if not z.has("docProps/core.xml"):
        return {}
    root = parse(z.read("docProps/core.xml"), part="properties")
    out = {}
    for el in root:
        name, text = local(el.tag), (el.text or "").strip()
        if text and name in ("title", "created", "language"):
            out[name] = text
    return out


def _date(value: str | None) -> str | None:
    return value[:10] if value and len(value) >= 10 and value[4] == "-" else None


def _rels(z: SafeZip, part: str) -> dict[str, str]:
    """A part's relationships: id → target path inside the zip."""
    folder, name = posixpath.split(part)
    rels_path = posixpath.join(folder, "_rels", name + ".rels")
    if not z.has(rels_path):
        return {}
    out = {}
    for rel in parse(z.read(rels_path), part="relationships"):
        target = attr(rel, "Target") or ""
        if attr(rel, "TargetMode") == "External":
            continue
        out[attr(rel, "Id") or ""] = posixpath.normpath(posixpath.join(folder, target)).lstrip("/") \
            if not target.startswith("/") else target.lstrip("/")
    return out


# ── Word ───────────────────────────────────────────────────────────────────────

@dataclass
class _RunStyle:
    vanish: bool | None = None
    color: str | None = None
    size: int | None = None  # half-points
    shaded: bool | None = None

    def over(self, base: _RunStyle) -> _RunStyle:
        return _RunStyle(*(mine if mine is not None else theirs for mine, theirs in zip(
            (self.vanish, self.color, self.size, self.shaded), (base.vanish, base.color, base.size, base.shaded),
            strict=True)))

    def hidden(self) -> bool:
        if self.vanish:
            return True
        if self.size is not None and self.size < 2:
            return True
        return (self.color or "").upper() in ("FFFFFF", "FFFFFE") and not self.shaded


def _run_style(rpr: Element | None) -> _RunStyle:
    style = _RunStyle()
    if rpr is None:
        return style
    for el in rpr:
        name = local(el.tag)
        if name in ("vanish", "specVanish"):
            style.vanish = is_true(attr(el, "val"))
        elif name == "color":
            style.color = attr(el, "val")
        elif name == "sz":
            try:
                style.size = int(attr(el, "val") or "")
            except ValueError:
                pass
        elif name in ("shd", "highlight"):
            fill = (attr(el, "fill") or attr(el, "val") or "").lower()
            style.shaded = fill not in ("", "auto", "none", "ffffff", "clear")
    return style


@dataclass
class _Styles:
    headings: dict[str, int]          # paragraph style id → heading level
    runs: dict[str, _RunStyle]        # style id → its run properties (after basedOn)
    titles: set[str]


def _word_styles(z: SafeZip) -> _Styles:
    out = _Styles(headings={}, runs={}, titles=set())
    if not z.has("word/styles.xml"):
        return out
    raw: dict[str, tuple[str | None, _RunStyle]] = {}
    for style in descendants(parse(z.read("word/styles.xml"), part="styles"), "style"):
        sid = attr(style, "styleId") or ""
        name = (attr(child(style, "name"), "val") if child(style, "name") is not None else "") or ""
        based = child(style, "basedOn")
        raw[sid] = (attr(based, "val") if based is not None else None, _run_style(child(style, "rPr")))
        lowered = name.lower().replace(" ", "")
        outline = find(style, "pPr", "outlineLvl")
        if lowered == "title":
            out.titles.add(sid)
            out.headings[sid] = 1
        elif lowered.startswith("heading") and lowered[7:].isdigit():
            out.headings[sid] = max(1, min(9, int(lowered[7:])))
        elif outline is not None and (attr(outline, "val") or "").isdigit() and int(attr(outline, "val")) < 9:
            out.headings[sid] = int(attr(outline, "val")) + 1

    def resolved(sid: str, seen: frozenset = frozenset()) -> _RunStyle:
        based, own = raw.get(sid, (None, _RunStyle()))
        if based and based in raw and based not in seen:
            return own.over(resolved(based, seen | {sid}))
        return own

    out.runs = {sid: resolved(sid) for sid in raw}
    return out


class _WordReader:
    def __init__(self, styles: _Styles, builder: DocBuilder):
        self.styles, self.builder = styles, builder
        self.boxes: list[Element] = []  # text-box paragraphs, read after the paragraph that holds them

    def runs_text(self, el: Element, base: _RunStyle) -> str:
        """The visible text of a paragraph (or any run container), in order."""
        out: list[str] = []
        hidden_run = [False]

        def walk(node: Element, inherited: _RunStyle) -> None:
            for c in node:
                name = local(c.tag)
                if name in _NOT_TEXT:
                    if name == "txbxContent":
                        self.boxes.extend(descendants(c, "p"))
                    continue
                if name == "r":
                    style = _run_style(child(c, "rPr"))
                    rstyle = find(c, "rPr", "rStyle")
                    if rstyle is not None:
                        style = style.over(self.styles.runs.get(attr(rstyle, "val") or "", _RunStyle()))
                    style = style.over(inherited)
                    if style.hidden():
                        if any(local(t.tag) == "t" and (t.text or "").strip() for t in c):
                            if not hidden_run[0]:
                                self.builder.hidden()
                            hidden_run[0] = True
                        continue  # a hidden run hides its text boxes too
                    hidden_run[0] = False
                    walk(c, style)
                elif name == "t":
                    out.append(c.text or "")
                elif name == "tab":
                    out.append("\t")
                elif name in ("br", "cr"):
                    out.append("\n")
                elif name == "noBreakHyphen":
                    out.append("-")
                else:  # hyperlink, ins, smartTag, fldSimple, sdt, sdtContent, AlternateContent, Choice, drawing …
                    walk(c, inherited)

        walk(el, base)
        return "".join(out)

    def paragraph_style(self, p: Element) -> tuple[str, _RunStyle]:
        pstyle = find(p, "pPr", "pStyle")
        sid = (attr(pstyle, "val") if pstyle is not None else None) or ""
        return sid, self.styles.runs.get(sid, _RunStyle())

    def table_rows(self, tbl: Element) -> list[list[str]]:
        rows = []
        for tr in children(tbl, "tr"):
            cells = []
            for tc in children(tr, "tc"):
                parts = [self.runs_text(p, self.paragraph_style(p)[1]).strip() for p in descendants(tc, "p")]
                cells.append(" ".join(x for x in parts if x))
            if any(cells):
                rows.append(cells)
        return rows


def _block_items(body: Element):
    """The body's paragraphs and tables in order, through content controls and custom XML."""
    for c in body:
        name = local(c.tag)
        if name in ("p", "tbl"):
            yield name, c
        elif name in ("sdt", "customXml", "smartTag"):
            inner = child(c, "sdtContent") if name == "sdt" else c
            if inner is not None:
                yield from _block_items(inner)


def read_docx(data: bytes, ctx: ReadContext) -> ParsedDocument:
    z = SafeZip(data, kind="Word document")
    props = core_properties(z)
    styles = _word_styles(z)
    builder = DocBuilder()
    reader = _WordReader(styles, builder)
    headings = Headings()
    title = props.get("title")
    root = parse(z.read("word/document.xml"), part="document")
    body = find(root, "body")
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            builder.add("list", "\n".join(f"- {i}" for i in list_items), heading_path=headings.path,
                        items=list(list_items))
            list_items.clear()

    def paragraph(p: Element) -> None:
        sid, base = reader.paragraph_style(p)
        text = reader.runs_text(p, base).strip()
        boxes, reader.boxes = reader.boxes, []
        if text:
            level = styles.headings.get(sid)
            outline = find(p, "pPr", "outlineLvl")
            if level is None and outline is not None and (attr(outline, "val") or "").isdigit():
                level = int(attr(outline, "val")) + 1 if int(attr(outline, "val")) < 9 else None
            if level is not None:
                flush_list()
                nonlocal title
                block = builder.add("heading", text, level=level, heading_path=headings.push(level, text))
                if block is not None and title is None and (sid in styles.titles or level == 1):
                    title = block.text
            elif find(p, "pPr", "numPr") is not None:
                list_items.append(text)
            else:
                flush_list()
                builder.add("paragraph", text, heading_path=headings.path)
        for box in boxes:
            paragraph(box)

    for kind, el in _block_items(body) if body is not None else []:
        if kind == "p":
            paragraph(el)
        else:
            flush_list()
            rows = reader.table_rows(el)
            if rows:
                width = max(len(r) for r in rows)
                square = [r + [""] * (width - len(r)) for r in rows]
                builder.add("table", "\n".join(" | ".join(r) for r in square), heading_path=headings.path,
                            table=TableData(rows=square, header_rows=1, n_rows=len(square), n_cols=width))
    flush_list()
    for part, element in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
        if not z.has(part):
            continue
        for note in descendants(parse(z.read(part), part="notes"), element):
            if (attr(note, "type") or "normal") != "normal":
                continue  # separators
            text = " ".join(t for t in (reader.runs_text(p, reader.paragraph_style(p)[1]).strip()
                                        for p in descendants(note, "p")) if t)
            if text:
                builder.add("footnote", text)
    return builder.build(ctx.source, DOCX_PARSER, title=title, doc_date=_date(props.get("created")),
                         language=props.get("language"))


# ── PowerPoint ─────────────────────────────────────────────────────────────────

def _a_text(el: Element) -> list[str]:
    """DrawingML paragraphs (`a:p`) under `el`, each as its text."""
    out = []
    for p in descendants(el, "p"):
        if local(p.tag) != "p" or not any(local(c.tag) in ("r", "fld", "br") for c in p):
            continue
        parts = []
        for c in p:
            name = local(c.tag)
            if name in ("r", "fld"):
                rpr = child(c, "rPr")
                size = attr(rpr, "sz") if rpr is not None else None
                if size and size.isdigit() and int(size) < 100:  # under 1 pt
                    continue
                t = child(c, "t")
                parts.append(t.text or "" if t is not None else "")
            elif name == "br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            out.append(text)
    return out


def _shapes(tree: Element, builder: DocBuilder):
    """A slide's shapes in order, through groups; hidden ones counted and skipped."""
    for c in tree:
        name = local(c.tag)
        if name in ("sp", "graphicFrame", "grpSp", "pic", "cxnSp"):
            props = next(descendants(c, "cNvPr"), None)
            if props is not None and is_true(attr(props, "hidden"), default=False):
                if any(True for _ in descendants(c, "t")):
                    builder.hidden()
                continue
        if name == "grpSp":
            yield from _shapes(c, builder)
        elif name in ("sp", "graphicFrame"):
            yield c


def read_pptx(data: bytes, ctx: ReadContext) -> ParsedDocument:
    z = SafeZip(data, kind="PowerPoint deck")
    props = core_properties(z)
    pres = parse(z.read("ppt/presentation.xml"), part="presentation")
    rels = _rels(z, "ppt/presentation.xml")
    ids = find(pres, "sldIdLst")
    slides = [rels.get(relationship_id(s) or "") for s in (children(ids, "sldId") if ids is not None else [])]
    slides = [s for s in slides if s and z.has(s)]
    if ctx.max_pages and len(slides) > ctx.max_pages:
        raise OverPageCap(len(slides), ctx.max_pages)
    builder = DocBuilder()
    title = props.get("title")
    for number, part in enumerate(slides, start=1):
        builder.page(number)
        root = parse(z.read(part), part=f"slide {number}")
        if not is_true(attr(root, "show"), default=True):
            builder.hidden()
            continue
        tree = next(descendants(root, "spTree"), None)
        slide_title: str | None = None
        path: list[str] = []
        for shape in _shapes(tree, builder) if tree is not None else []:
            ph = next(descendants(shape, "ph"), None)
            kind = attr(ph, "type") if ph is not None else None
            table = next(descendants(shape, "tbl"), None)
            if table is not None:
                rows = [[" ".join(_a_text(tc)) for tc in descendants(tr, "tc")] for tr in descendants(table, "tr")]
                rows = [r for r in rows if any(r)]
                if rows:
                    width = max(len(r) for r in rows)
                    square = [r + [""] * (width - len(r)) for r in rows]
                    builder.add("table", "\n".join(" | ".join(r) for r in square), page=number, heading_path=path,
                                table=TableData(rows=square, header_rows=1, n_rows=len(square), n_cols=width))
                continue
            paragraphs = _a_text(shape)
            if not paragraphs:
                continue
            if kind in ("title", "ctrTitle") and slide_title is None:
                slide_title = " ".join(paragraphs)
                path = [slide_title]
                builder.add("heading", slide_title, page=number, level=1, heading_path=path)
                title = title or slide_title
            elif kind in (None, "body", "obj") and len(paragraphs) > 1:
                builder.add("list", "\n".join(f"- {p}" for p in paragraphs), page=number, heading_path=path,
                            items=paragraphs)
            else:
                builder.add("paragraph", "\n".join(paragraphs), page=number, heading_path=path)
        notes = [t for rel in _rels(z, part).values() if "notesSlide" in rel and z.has(rel)
                 for t in _notes_text(parse(z.read(rel), part=f"notes of slide {number}"))]
        if notes:
            builder.add("paragraph", "\n".join(notes), page=number,
                        heading_path=[*(path or [f"Slide {number}"]), "Speaker notes"])
    return builder.build(ctx.source, PPTX_PARSER, title=title, doc_date=_date(props.get("created")),
                         language=props.get("language"))


def _notes_text(root: Element) -> list[str]:
    out = []
    for shape in descendants(root, "sp"):
        ph = next(descendants(shape, "ph"), None)
        if ph is not None and attr(ph, "type") == "body":
            out.extend(_a_text(shape))
    return out


# ── Excel ──────────────────────────────────────────────────────────────────────

def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    return str(value).strip()


def trim_columns(rows: list[list[str]]) -> list[list[str]]:
    """Rows without the empty columns at the right (a stray format can reach column XFD)."""
    width = max((max((k + 1 for k, c in enumerate(r) if c), default=0) for r in rows), default=0)
    return [r[:width] for r in rows]


def read_xlsx(data: bytes, ctx: ReadContext) -> ParsedDocument:
    import openpyxl

    SafeZip(data, kind="Excel workbook")  # the bomb check, before openpyxl decompresses anything
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    builder = DocBuilder()
    try:
        for index, sheet in enumerate(workbook.worksheets):
            if sheet.sheet_state in ("hidden", "veryHidden"):
                builder.hidden()
                continue
            rows: list[list[str]] = []
            more = 0
            for values in sheet.iter_rows(values_only=True):
                row = [cell_text(v) for v in values]
                if not any(row):
                    continue
                if len(rows) <= ROW_GROUP_MAX_ROWS:
                    rows.append(row)
                else:
                    more += 1  # counted, not kept: a big sheet is its card
            parts = sheet_parts(sheet.title, trim_columns(rows), more_rows=more)
            if parts is None:
                continue
            builder.add("sheet", parts.card, sheet_index=index, heading_path=[sheet.title], sheet=parts.data)
            for body, table in parts.groups:
                builder.add("table", body, sheet_index=index, heading_path=[sheet.title], table=table)
            builder.warnings.extend(parts.warnings)
        props = workbook.properties
        created = props.created.date().isoformat() if getattr(props, "created", None) else None
        title = props.title or None
    finally:
        workbook.close()
    return builder.build(ctx.source, XLSX_PARSER, title=title, doc_date=created)
