"""Text formats — Markdown, plain text, JSON, CSV/TSV — as `ParsedDocument` blocks.

Every block's `char_start:char_end` slices exactly its `text` out of the
document's canonical text (the contract's rule), so blocks are always cut from
the text with `_Blocks.add`, never assembled beside it. Text formats have no
pages: block ids are `b0000-<n>`, as in the committed record fixture.

JSON and CSV are *rendered* first — JSON as `path: value` lines, CSV as a sheet
card plus row groups — and the rendering is the canonical text, as a PDF's
extracted text is for a PDF.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from core.knowledge.parse.sheets import ROW_GROUP_MAX_ROWS, ROW_GROUP_SIZE, SAMPLE_ROWS, sheet_parts
from core.knowledge.parsed import Block, ParseWarning, TableData

#: Past these, the rest is cut and the document ends `partial` with a warning.
MAX_TEXT_CHARS = 5_000_000
MAX_BLOCKS = 20_000
#: The sheet card and its row-group sizes live in `sheets.py`, shared by every tabular format;
#: they are re-exported here for the callers that knew them from this module.
__all__ =["MAX_TEXT_CHARS", "MAX_BLOCKS", "ROW_GROUP_MAX_ROWS", "ROW_GROUP_SIZE", "SAMPLE_ROWS", "paragraphs",
           "parse_markdown", "parse_plain", "parse_code", "render_json", "parse_delimited"]


class _Blocks:
    def __init__(self, text: str) -> None:
        self.text = text
        self.blocks: list[Block] = []

    def add(self, kind: str, start: int, end: int, **fields: Any) -> Block | None:
        """A block over `text[start:end]`, trimmed of surrounding whitespace; None if nothing is left."""
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        if start >= end or len(self.blocks) >= MAX_BLOCKS:
            return None
        block = Block(id=f"b0000-{len(self.blocks):04d}", kind=kind, text=self.text[start:end],  # type: ignore[arg-type]
                      char_start=start, char_end=end, **fields)
        self.blocks.append(block)
        return block


def _lines(text: str) -> list[tuple[int, str]]:
    """Each line with the offset it starts at."""
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


# ── plain text ─────────────────────────────────────────────────────────────────

def paragraphs(text: str, blocks: _Blocks | None = None, *, offset_lines: list[tuple[int, str]] | None = None,
               heading_path: list[str] | None = None) -> _Blocks:
    """Paragraphs separated by blank lines."""
    blocks = blocks or _Blocks(text)
    start = None
    for pos, line in (offset_lines if offset_lines is not None else _lines(text)):
        if line.strip():
            if start is None:
                start = pos
            end = pos + len(line)
        elif start is not None:
            blocks.add("paragraph", start, end, heading_path=list(heading_path or []))
            start = None
    if start is not None:
        blocks.add("paragraph", start, end, heading_path=list(heading_path or []))
    return blocks


# ── Markdown ───────────────────────────────────────────────────────────────────

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])[ \t]+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_FRONT_KEY = re.compile(r"^(title|date)\s*:\s*[\"']?(.*?)[\"']?\s*$", re.IGNORECASE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def parse_markdown(text: str) -> tuple[list[Block], dict[str, str | None]]:
    """Blocks, and the title / date the document names (first H1, or front matter)."""
    blocks = _Blocks(text)
    lines = _lines(text)
    found: dict[str, str | None] = {"title": None, "doc_date": None}
    path: list[tuple[int, str]] = []
    i, n = 0, len(lines)

    def heading_path() -> list[str]:
        return [t for _, t in path]

    def starts_block(line: str, nxt: str | None) -> bool:
        return bool(_HEADING.match(line) or _FENCE.match(line) or _LIST.match(line)
                    or (line.lstrip().startswith("|") and nxt is not None and _TABLE_SEP.match(nxt)))

    if n and lines[0][1].strip() == "---":  # front matter: kept, never embedded
        for j in range(1, min(n, 200)):
            if lines[j][1].strip() in ("---", "..."):
                for _, line in lines[1:j]:
                    m = _FRONT_KEY.match(line)
                    if m and m.group(2):
                        key = "title" if m.group(1).lower() == "title" else "doc_date"
                        if key == "title" or _DATE.match(m.group(2)):
                            found[key] = m.group(2) if key == "title" else m.group(2)[:10]
                blocks.add("other", 0, lines[j][0] + len(lines[j][1]), furniture=True)
                i = j + 1
                break

    while i < n:
        pos, line = lines[i]
        nxt = lines[i + 1][1] if i + 1 < n else None
        if not line.strip():
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            while path and path[-1][0] >= level:
                path.pop()
            if title:
                path.append((level, title))
                blocks.add("heading", pos + m.start(2), pos + m.end(2), level=level, heading_path=heading_path())
                if level == 1 and found["title"] is None:
                    found["title"] = title
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < n and not lines[j][1].lstrip().startswith(fence[0] * len(fence)):
                j += 1
            if j > i + 1:
                last_pos, last = lines[j - 1]
                blocks.add("code", lines[i + 1][0], last_pos + len(last), heading_path=heading_path())
            i = j + 1
            continue
        if line.lstrip().startswith("|") and nxt is not None and _TABLE_SEP.match(nxt):
            j = i
            while j < n and lines[j][1].lstrip().startswith("|"):
                j += 1
            rows = [_cells(lines[k][1]) for k in range(i, j) if k != i + 1]
            width = max(len(r) for r in rows)
            end_pos, end_line = lines[j - 1]
            blocks.add("table", pos, end_pos + len(end_line), heading_path=heading_path(),
                       table=TableData(rows=[r + [""] * (width - len(r)) for r in rows], header_rows=1,
                                       n_rows=len(rows), n_cols=width))
            i = j
            continue
        if _LIST.match(line):
            j, items = i, []
            while j < n and lines[j][1].strip() and (_LIST.match(lines[j][1]) or lines[j][1][:1] in (" ", "\t")):
                item = _LIST.match(lines[j][1])
                if item:
                    items.append(item.group(1).strip())
                elif items:
                    items[-1] += " " + lines[j][1].strip()
                j += 1
            end_pos, end_line = lines[j - 1]
            blocks.add("list", pos, end_pos + len(end_line), items=items, heading_path=heading_path())
            i = j
            continue
        j = i
        while j < n and lines[j][1].strip() and (j == i or not starts_block(lines[j][1],
                                                                          lines[j + 1][1] if j + 1 < n else None)):
            j += 1
        end_pos, end_line = lines[j - 1]
        blocks.add("paragraph", pos, end_pos + len(end_line), heading_path=heading_path())
        i = j
    return blocks.blocks, found


def parse_plain(text: str) -> list[Block]:
    return paragraphs(text).blocks


def parse_code(text: str) -> list[Block]:
    """Source code: blank-line-separated runs, as `code` blocks."""
    return [b.model_copy(update={"kind": "code"}) for b in paragraphs(text).blocks]


# ── JSON ───────────────────────────────────────────────────────────────────────

def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten(value: Any, prefix: str, out: list[str]) -> None:
    if isinstance(value, dict):
        if not value:
            out.append(f"{prefix}: {{}}")
        for key, inner in value.items():
            _flatten(inner, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            out.append(f"{prefix}: {', '.join(_scalar(v) for v in value)}")
        else:
            for k, inner in enumerate(value):
                _flatten(inner, f"{prefix}[{k}]", out)
    else:
        out.append(f"{prefix}: {_scalar(value)}" if prefix else _scalar(value))


def render_json(value: Any) -> tuple[str, bool]:
    """`value` as `path: value` lines, one blank-line-separated section per top-level key or
    array item. Returns the text, and whether sections were cut at `MAX_TEXT_CHARS`."""
    if isinstance(value, dict):
        sections = [(str(k), v) for k, v in value.items()]
    elif isinstance(value, list):
        sections = [(f"[{k}]", v) for k, v in enumerate(value)]
    else:
        sections = [("", value)]
    parts, size = [], 0
    for name, inner in sections:
        lines: list[str] = []
        _flatten(inner, name, lines)
        body = "\n".join(lines)
        if size + len(body) > MAX_TEXT_CHARS:
            return "\n\n".join(parts), True
        parts.append(body)
        size += len(body) + 2
    return "\n\n".join(parts), False


# ── CSV / TSV ──────────────────────────────────────────────────────────────────

def parse_delimited(text: str, *, name: str, delimiter: str) -> tuple[str, list[Block], list[ParseWarning]]:
    """A sheet card, and row groups of `ROW_GROUP_SIZE` for a small sheet. Returns (text, blocks, warnings)."""
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    parts = sheet_parts(name, rows)
    if parts is None:
        return "", [], []
    pieces = [parts.card, *(body for body, _ in parts.groups)]
    full = "\n\n".join(pieces)
    blocks = _Blocks(full)
    pos = 0
    for k, piece in enumerate(pieces):
        if k == 0:
            blocks.add("sheet", pos, pos + len(piece), sheet=parts.data)
        else:
            blocks.add("table", pos, pos + len(piece), table=parts.groups[k - 1][1])
        pos += len(piece) + 2
    return full, blocks.blocks, parts.warnings
