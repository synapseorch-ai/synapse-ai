"""OpenDocument text, presentation and spreadsheet (LibreOffice's own formats), read from `content.xml`.

The same rules as the OOXML readers (`office.py`), in ODF's vocabulary:

* **Text:** `text:h` is a heading at its outline level, `text:list` a list,
  `table:table` a table; footnotes follow the body. Hidden text
  (`text:hidden-text`, `text:hidden-paragraph`, or a style with
  `text:display="none"`) is left out and counted.
* **Presentation:** a `draw:page` is a page; a title frame is its heading;
  speaker notes are indexed under it; a hidden slide is left out and counted.
* **Spreadsheet:** each `table:table` is a sheet card plus row groups; a
  hidden sheet is left out and counted. Repeated rows and columns
  (`number-*-repeated`) are expanded only as far as they hold anything, so a
  style applied to a million empty rows costs nothing.
"""
from __future__ import annotations

from xml.etree.ElementTree import Element

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import OverPageCap
from core.knowledge.parse.office import trim_columns
from core.knowledge.parse.sheets import ROW_GROUP_MAX_ROWS, sheet_parts
from core.knowledge.parse.tree import DocBuilder, Headings
from core.knowledge.parse.xmlread import attr, child, children, descendants, find, local, parse
from core.knowledge.parse.zipguard import SafeZip
from core.knowledge.parsed import ParsedDocument, ParserInfo, TableData

ODT_PARSER = ParserInfo(name="odt", version="1")
ODP_PARSER = ParserInfo(name="odp", version="1")
ODS_PARSER = ParserInfo(name="ods", version="1")

#: A repeated empty column or row is expanded at most this far (a whole-sheet style repeats 16k columns).
MAX_REPEAT = 1024


class _Doc:
    def __init__(self, data: bytes, kind: str):
        self.zip = SafeZip(data, kind=kind)
        self.root = parse(self.zip.read("content.xml"), part="content")
        self.hidden_styles = self._hidden_styles()
        self.meta = self._meta()

    def _hidden_styles(self) -> set[str]:
        """Automatic styles that hide what they style: text not displayed, a sheet or slide not shown."""
        out = set()
        for styles in (find(self.root, "automatic-styles"),):
            for style in children(styles, "style") if styles is not None else []:
                for props in style:
                    if (attr(props, "display") in ("none", "false")
                            or attr(props, "visibility") == "hidden"):
                        out.add(attr(style, "name") or "")
        return out

    def _meta(self) -> dict[str, str]:
        if not self.zip.has("meta.xml"):
            return {}
        meta = find(parse(self.zip.read("meta.xml"), part="metadata"), "meta")
        out = {}
        for el in meta if meta is not None else []:
            name, text = local(el.tag), (el.text or "").strip()
            if text and name in ("title", "creation-date", "language"):
                out[name] = text
        return out

    def hidden(self, el: Element) -> bool:
        return (attr(el, "style-name") or "") in self.hidden_styles

    def text(self, el: Element, builder: DocBuilder | None = None) -> str:
        """The visible text of a paragraph-like element, notes and hidden spans left out."""
        out: list[str] = []

        def walk(node: Element) -> None:
            if node.text:
                out.append(node.text)
            for c in node:
                name = local(c.tag)
                if name in ("note", "annotation"):
                    pass  # a footnote is read after the body; a comment is not the document's text
                elif name in ("hidden-text", "hidden-paragraph") or self.hidden(c):
                    if builder is not None and "".join(c.itertext()).strip():
                        builder.hidden()
                elif name == "s":
                    out.append(" " * int(attr(c, "c") or "1"))
                elif name == "tab":
                    out.append("\t")
                elif name == "line-break":
                    out.append("\n")
                else:
                    walk(c)
                if c.tail:
                    out.append(c.tail)

        walk(el)
        return "".join(out)

    def date(self) -> str | None:
        value = self.meta.get("creation-date")
        return value[:10] if value and len(value) >= 10 else None


def _table_rows(doc: _Doc, table: Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in descendants(table, "table-row"):
        cells: list[str] = []
        for cell in row:
            if local(cell.tag) not in ("table-cell", "covered-table-cell"):
                continue
            text = " ".join(t for t in (doc.text(p).strip() for p in children(cell, "p")) if t)
            repeat = int(attr(cell, "number-columns-repeated") or "1")
            cells.extend([text] * (min(repeat, MAX_REPEAT) if text else min(repeat, 1)))
        if not any(cells):
            continue
        repeat = int(attr(row, "number-rows-repeated") or "1")
        rows.extend([cells] * min(repeat, MAX_REPEAT))
    return trim_columns(rows)


def _add_table(builder: DocBuilder, rows: list[list[str]], **fields) -> None:
    if not rows:
        return
    width = max(len(r) for r in rows)
    square = [r + [""] * (width - len(r)) for r in rows]
    builder.add("table", "\n".join(" | ".join(r) for r in square),
                table=TableData(rows=square, header_rows=1, n_rows=len(square), n_cols=width), **fields)


# ── text ───────────────────────────────────────────────────────────────────────

def read_odt(data: bytes, ctx: ReadContext) -> ParsedDocument:
    doc = _Doc(data, "OpenDocument text")
    body = find(doc.root, "body", "text")
    builder = DocBuilder()
    headings = Headings()
    title = doc.meta.get("title")
    notes: list[str] = []

    def walk(container: Element) -> None:
        nonlocal title
        for el in container:
            name = local(el.tag)
            if doc.hidden(el) or name in ("hidden-paragraph",):
                if "".join(el.itertext()).strip():
                    builder.hidden()
                continue
            if name in ("h", "p", "list", "table"):  # leaves: a section's notes are collected inside it
                notes.extend(" ".join(doc.text(p).strip() for p in descendants(n, "p")).strip()
                             for n in descendants(el, "note-body"))
            if name == "h":
                text = doc.text(el, builder).strip()
                level = int(attr(el, "outline-level") or "1")
                block = builder.add("heading", text, level=level, heading_path=headings.push(level, text))
                if block is not None and title is None and level == 1:
                    title = block.text
            elif name == "p":
                builder.add("paragraph", doc.text(el, builder), heading_path=headings.path)
            elif name == "list":
                items = [t for t in (doc.text(p, builder).strip() for item in descendants(el, "list-item")
                                     for p in children(item, "p")) if t]
                if items:
                    builder.add("list", "\n".join(f"- {i}" for i in items), heading_path=headings.path, items=items)
            elif name == "table":
                _add_table(builder, _table_rows(doc, el), heading_path=headings.path)
            elif name in ("section", "index-body", "illustration-index", "table-of-content"):
                walk(el)

    if body is not None:
        walk(body)
    for note in notes:
        if note:
            builder.add("footnote", note)
    return builder.build(ctx.source, ODT_PARSER, title=title, doc_date=doc.date(), language=doc.meta.get("language"))


# ── presentation ───────────────────────────────────────────────────────────────

def read_odp(data: bytes, ctx: ReadContext) -> ParsedDocument:
    doc = _Doc(data, "OpenDocument presentation")
    body = find(doc.root, "body", "presentation")
    pages = children(body, "page") if body is not None else []
    if ctx.max_pages and len(pages) > ctx.max_pages:
        raise OverPageCap(len(pages), ctx.max_pages)
    builder = DocBuilder()
    title = doc.meta.get("title")
    for number, page in enumerate(pages, start=1):
        builder.page(number)
        if doc.hidden(page) or attr(page, "visibility") == "hidden":
            builder.hidden()
            continue
        path: list[str] = []
        for frame in page:
            if local(frame.tag) not in ("frame", "custom-shape", "g"):
                continue
            table = next(descendants(frame, "table"), None)
            if table is not None:
                _add_table(builder, _table_rows(doc, table), page=number, heading_path=path)
                continue
            paragraphs = [t for t in (doc.text(p, builder).strip() for p in descendants(frame, "p")) if t]
            if not paragraphs:
                continue
            if attr(frame, "class") == "title" and not path:
                path = [" ".join(paragraphs)]
                builder.add("heading", path[0], page=number, level=1, heading_path=path)
                title = title or path[0]
            elif len(paragraphs) > 1:
                builder.add("list", "\n".join(f"- {p}" for p in paragraphs), page=number, heading_path=path,
                            items=paragraphs)
            else:
                builder.add("paragraph", paragraphs[0], page=number, heading_path=path)
        notes = child(page, "notes")
        if notes is not None:
            text = "\n".join(t for t in (doc.text(p).strip() for p in descendants(notes, "p")) if t)
            if text:
                builder.add("paragraph", text, page=number,
                            heading_path=[*(path or [f"Slide {number}"]), "Speaker notes"])
    return builder.build(ctx.source, ODP_PARSER, title=title, doc_date=doc.date())


# ── spreadsheet ────────────────────────────────────────────────────────────────

def read_ods(data: bytes, ctx: ReadContext) -> ParsedDocument:
    doc = _Doc(data, "OpenDocument spreadsheet")
    body = find(doc.root, "body", "spreadsheet")
    builder = DocBuilder()
    for index, table in enumerate(children(body, "table") if body is not None else []):
        name = attr(table, "name") or f"Sheet{index + 1}"
        if doc.hidden(table):
            builder.hidden()
            continue
        rows = _table_rows(doc, table)
        kept, more = rows[:ROW_GROUP_MAX_ROWS + 1], max(0, len(rows) - ROW_GROUP_MAX_ROWS - 1)
        parts = sheet_parts(name, kept, more_rows=more)
        if parts is None:
            continue
        builder.add("sheet", parts.card, sheet_index=index, heading_path=[name], sheet=parts.data)
        for text, data_table in parts.groups:
            builder.add("table", text, sheet_index=index, heading_path=[name], table=data_table)
        builder.warnings.extend(parts.warnings)
    return builder.build(ctx.source, ODS_PARSER, title=doc.meta.get("title"), doc_date=doc.date())
