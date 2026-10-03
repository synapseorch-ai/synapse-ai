"""PDF through Docling: its layout model and TableFormer, when the `knowledge` extra is installed.

Docling reads PDFs only; Office files go to our own readers (D162 #1). It is
used when it imports **and** its models are on disk — `DOCLING_ARTIFACTS_PATH`,
else `<state dir>/models/docling` — so nothing downloads at run time (KB0: a
run-time fetch fails offline and, in cloud, would cross the egress proxy on
every cold start). Fetch them once with
`docling-tools models download layout tableformer -o <that path>`.

KB0's measurements decide the shape (16 §8 → "KB0"):

* **4 pages at a time** (`limits.docling_batch_pages`): a one-pass 48-page PDF
  peaks at 2.4 GB (KB0); measured here on the same PDF under a 2 GB cap,
  8-page batches peak at 1,994 MB in 130 s and 4-page batches at 1,789 MB in
  105 s, with the same output. Each batch's progress is saved, so a timeout
  keeps the pages read (measured: a 60 s cut kept 16 of 48 pages).
* **No Docling OCR** (`do_ocr=False`): OCR is ~9× slower and D153 #5 sends
  pages without a text layer to the OCR seam instead — the same quality gate
  as the basic reader decides which.
* **Layout and tables only:** no picture classification, picture
  description, code or formula enrichment, no page images.

Docling keeps no colour or size, so hidden text is found by the basic
reader's own character pass (`pdf.char_layer`, about 2% of Docling's time per
page): an item with no visible character in its box is left out, and hidden
runs inside a kept item are cut from its text. Running headers and footers
(Docling's furniture layer) become furniture blocks.
"""
from __future__ import annotations

import copy
import gc
import importlib.metadata
import importlib.util
import io
import os
from pathlib import Path
from typing import Any

from core.knowledge.parse import limits
from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.tree import DocBuilder, Headings, no_text_layer_warning
from core.knowledge.parsed import BBox, ParsedDocument, ParserInfo, TableData

#: The models the pipeline loads, and nothing else (E1b pre-fetches exactly these).
MODELS = ("layout", "tableformer")
_TEXT_KINDS = {"text": "paragraph", "paragraph": "paragraph", "caption": "caption", "footnote": "footnote",
               "reference": "paragraph", "handwritten_text": "paragraph", "formula": "paragraph",
               "checkbox_selected": "paragraph", "checkbox_unselected": "paragraph", "code": "code"}


def artifacts_path() -> Path | None:
    """Where Docling's models are, if they are there."""
    configured = os.environ.get("DOCLING_ARTIFACTS_PATH")
    if configured:
        path = Path(configured)
    else:
        from core.runtime_dirs import state_root

        path = state_root() / "models" / "docling"
    try:
        return path if path.is_dir() and any(path.iterdir()) else None
    except OSError:
        return None


def available() -> bool:
    """Docling imports here and its models are on disk. Cheap: nothing is imported."""
    return importlib.util.find_spec("docling") is not None and artifacts_path() is not None


def parser_info() -> ParserInfo:
    try:
        version = importlib.metadata.version("docling-slim")
    except importlib.metadata.PackageNotFoundError:
        try:
            version = importlib.metadata.version("docling")
        except importlib.metadata.PackageNotFoundError:
            version = "?"
    return ParserInfo(name="docling", version=version, options_hash="+".join(MODELS))


def _converter(artifacts: Path) -> Any:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(artifacts_path=str(artifacts), do_ocr=False, do_table_structure=True)
    for name in ("do_picture_classification", "do_picture_description", "do_code_enrichment",
                 "do_formula_enrichment", "generate_page_images", "generate_picture_images"):
        if hasattr(options, name):
            setattr(options, name, False)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def read_pdf_docling(data: bytes, ctx: ReadContext, document: Any) -> ParsedDocument:
    """`document` is the open pypdfium2 PDF (`pdf.read_pdf` counted its pages and keeps rendering)."""
    from core.knowledge.parse import pdf as basic

    artifacts = artifacts_path()  # the parent's resolved path reaches the child as DOCLING_ARTIFACTS_PATH
    if artifacts is None or importlib.util.find_spec("docling") is None:
        return basic._read_basic(data, ctx, document, len(document))

    import pdfplumber
    from docling_core.types.io import DocumentStream

    total = len(document)
    parser = parser_info()
    state = _State(ctx, parser, total)
    try:
        meta = document.get_metadata_dict()
    except Exception:  # noqa: BLE001 — a PDF without readable metadata still parses
        meta = {}
    state.meta_title, state.meta_date = basic._title(meta), basic._date(meta)
    converter = _converter(artifacts)
    batch = limits.docling_batch_pages()
    with pdfplumber.open(io.BytesIO(data)) as plumb:
        for start in range(1, total + 1, batch):
            end = min(total, start + batch - 1)
            layers = {}
            for number in range(start, end + 1):
                page = plumb.pages[number - 1]
                try:
                    layers[number] = (basic.char_layer(page), float(page.width), float(page.height))
                finally:
                    page.close()
            result = converter.convert(DocumentStream(name="document.pdf", stream=io.BytesIO(data)),
                                       raises_on_error=False, page_range=(start, end))
            state.add_batch(result.document, layers, start, end)
            for number in range(start, end + 1):
                if ctx.ocr and ctx.render is not None and number in state.no_text:
                    ctx.render(number, basic.render_png(document, number))
            del result
            gc.collect()
            ctx.report_pages(end, total)
            if end < total:
                ctx.save_progress(lambda end=end: state.snapshot(end))
    return state.finish()


class _State:
    """The document built so far, across batches: one heading path, list items gathered into lists."""

    def __init__(self, ctx: ReadContext, parser: ParserInfo, total: int):
        self.ctx, self.parser, self.total = ctx, parser, total
        self.builder = DocBuilder()
        self.headings = Headings()
        self.title: str | None = None
        self.no_text: list[int] = []
        self.meta_title: str | None = None  # the PDF's own Title, as the basic reader uses it
        self.meta_date: str | None = None
        self.list_items: list[tuple[str, int, BBox | None]] = []

    def flush_list(self) -> None:
        if self.list_items:
            items = [t for t, _, _ in self.list_items]
            page = self.list_items[0][1]
            boxes = [b for _, _, b in self.list_items if b is not None and b.page == page]
            self.builder.add("list", "\n".join(f"- {t}" for t in items), page=page, items=items,
                             heading_path=self.headings.path, bbox=boxes or None)
            self.list_items = []

    def add_batch(self, doc: Any, layers: dict, start: int, end: int) -> None:
        from docling_core.types.doc import ContentLayer

        for number in range(start, end + 1):
            layer, width, height = layers[number]
            self.builder.page(number, width=width, height=height)
            self.builder.hidden(len(layer.hidden))
            if not layer.visible and (layer.images or layer.curves >= 50):
                self.no_text.append(number)
                self.builder.page_quality(number, no_text_layer=True, image_only=layer.images)
        for item, _level in doc.iterate_items(included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE}):
            self.add_item(item, doc, layers)
        self.flush_list()

    def add_item(self, item: Any, doc: Any, layers: dict) -> None:
        label = str(getattr(item, "label", "") or "")
        prov = (getattr(item, "prov", None) or [None])[0]
        page = getattr(prov, "page_no", None)
        if page not in layers:
            return  # outside this batch (Docling numbers pages in the whole file)
        layer, _, height = layers[page]
        box = _bbox(prov, page, height)
        content_layer = getattr(item, "content_layer", "body")
        furniture = str(getattr(content_layer, "value", content_layer)).lower() == "furniture" or label in (
            "page_header", "page_footer")
        if label == "table":
            self.flush_list()
            rows = _table_rows(item, doc)
            if rows:
                width = max(len(r) for r in rows)
                self.builder.add("table", "\n".join(" | ".join(r) for r in rows), page=page,
                                 heading_path=self.headings.path, bbox=[box] if box else None,
                                 table=TableData(rows=rows, header_rows=1, n_rows=len(rows), n_cols=width))
            return
        text = str(getattr(item, "text", "") or "").strip()
        if not text or label in ("picture", "chart", "document_index", "form", "key_value_region"):
            return
        if box is not None and layer.hidden:
            area = (box.x0, box.y0, box.x1, box.y1)
            inside = [t for t, b in layer.hidden if _overlaps(area, b)]
            if inside and layer.visible_in(area) == 0:
                return  # nothing in it a reader can see: hidden text, counted already
            for hidden_text in inside:
                text = text.replace(hidden_text.strip(), "", 1).strip() if hidden_text.strip() else text
            if not text:
                return
        if furniture:
            self.flush_list()
            self.builder.add("other", text, page=page, furniture=True, bbox=[box] if box else None)
        elif label in ("title", "section_header"):
            self.flush_list()
            level = 1 if label == "title" else min(6, int(getattr(item, "level", 1) or 1) + 1)
            block = self.builder.add("heading", text, page=page, level=level,
                                     heading_path=self.headings.push(level, text), bbox=[box] if box else None)
            if block is not None and self.title is None and level == 1:
                self.title = block.text
        elif label == "list_item":
            self.list_items.append((text, page, box))
        else:
            self.flush_list()
            self.builder.add(_TEXT_KINDS.get(label, "paragraph"), text, page=page, heading_path=self.headings.path,
                             bbox=[box] if box else None)

    def _finish(self, builder: DocBuilder, read: int) -> ParsedDocument:
        if self.no_text:
            builder.warnings.append(no_text_layer_warning(self.no_text, ocr=False))
        if read < self.total:
            builder.cut("truncated_pages", f"Pages 1–{read} of {self.total} were read; the rest were not.")
        return builder.build(self.ctx.source, self.parser, title=self.meta_title or self.title, doc_date=self.meta_date)

    def snapshot(self, read: int) -> ParsedDocument:
        """What a timeout would keep: everything read so far, marked cut."""
        return _State._finish(self, copy.deepcopy(self.builder), read)

    def finish(self) -> ParsedDocument:
        self.flush_list()
        return self._finish(self.builder, self.total)


def _bbox(prov: Any, page: int, height: float) -> BBox | None:
    raw = getattr(prov, "bbox", None)
    if raw is None:
        return None
    if str(getattr(raw, "coord_origin", "")).upper().endswith("BOTTOMLEFT"):
        raw = raw.to_top_left_origin(page_height=height)
    x0, x1 = sorted((float(raw.l), float(raw.r)))
    y0, y1 = sorted((float(raw.t), float(raw.b)))
    return BBox(page=page, x0=round(x0, 2), y0=round(y0, 2), x1=round(x1, 2), y1=round(y1, 2))


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return a[0] - 1 <= cx <= a[2] + 1 and a[1] - 1 <= cy <= a[3] + 1


def _table_rows(item: Any, doc: Any) -> list[list[str]]:
    data = getattr(item, "data", None)
    grid = getattr(data, "grid", None) or []
    rows = [[" ".join(str(getattr(cell, "text", "") or "").split()) for cell in row] for row in grid]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]
