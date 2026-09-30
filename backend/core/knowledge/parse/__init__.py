"""Parsing: a Vault file's bytes into one `ParsedDocument`.

A1 reads the text formats — Markdown, plain text, JSON (records included),
CSV/TSV. A2 takes this package over and adds every other format (PDF, Office,
email, images) behind the same `parse_bytes`, in a subprocess with limits.
Until then another format is accepted at upload and ends `skipped` /
`unsupported_format`; `supports()` is what reconcile asks to re-queue it once
it is read.

Order inside every parser: decode → sanitize → cut blocks. Sanitizing first is
what keeps every block's offsets on the canonical text.
"""
from __future__ import annotations

import json
import posixpath
from typing import Any

from core.knowledge.ids import RECORDS_DIR
from core.knowledge.parse.sanitize import sanitize_text
from core.knowledge.parsed import (
    Block, ParsedDocument, ParserInfo, ParseStats, ParseWarning, SourceInfo,
)

TEXT_PARSER = ParserInfo(name="text", version="1")
RECORD_PARSER = ParserInfo(name="record", version="1")

#: What this package reads today.
PARSED_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".json", ".csv", ".tsv"})

#: What an upload accepts (16 §3.5): read now, or kept and read once a parser exists.
ACCEPTED_EXTENSIONS: frozenset[str] = PARSED_EXTENSIONS | frozenset({
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".ods", ".odp", ".rtf",
    ".html", ".htm", ".eml", ".msg", ".epub", ".xml", ".yaml", ".yml",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".webp",
})


class UnsupportedFormat(Exception):
    """This file is not read (yet): the document ends `skipped` / `unsupported_format`."""


def extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def supports(path: str) -> bool:
    return extension(path) in PARSED_EXTENSIONS


def is_record_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) >= 2 and parts[-2] == RECORDS_DIR and extension(path) == ".json"


def decode(data: bytes) -> tuple[str, list[ParseWarning]]:
    """Bytes as text. UTF-16 by its byte-order mark; UTF-8; then Windows-1252; replacement last."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), []
    if b"\x00" in data[:8192]:
        raise UnsupportedFormat("The file holds binary data, not text.")
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), []
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        bad = text.count("�")
        return text, [ParseWarning(code="garbled_text", count=bad,
                                   message=f"{bad} byte sequence(s) were not valid text and were replaced.")]


def parse_bytes(data: bytes, *, path: str, source: str, sha256: str, mime: str,
                url: str | None = None, fetched_at: str | None = None, etag: str | None = None,
                modified_at: str | None = None) -> ParsedDocument:
    """`data` (the file at Vault path `path`) as a `ParsedDocument`. Raises `UnsupportedFormat`."""
    ext = extension(path)
    if ext not in PARSED_EXTENSIONS:
        raise UnsupportedFormat(f"{ext or 'This file type'} is not read yet.")
    raw, warnings = decode(data)
    info = SourceInfo(path=path, sha256=sha256, size_bytes=len(data), mime=mime, source=source,  # type: ignore[arg-type]
                      url=url, fetched_at=fetched_at, etag=etag, modified_at=modified_at)
    if ext == ".json" and is_record_path(path):
        record = _record(raw)
        if record is not None:
            return _parse_record(record, info, warnings)
    return _parse_text(raw, ext, info, warnings, name=posixpath.splitext(posixpath.basename(path))[0])


def _finish(text: str, blocks: list[Block], info: SourceInfo, parser: ParserInfo, warnings: list[ParseWarning],
            *, truncated: bool = False, **fields: Any) -> ParsedDocument:
    if not blocks:
        warnings.append(ParseWarning(code="empty_document", message="The file has no text to index."))
    if truncated:
        warnings.append(ParseWarning(code="truncated_size", message=(
            "The file's text was cut at the per-document limit; the rest is not indexed.")))
    stats = ParseStats(chars=len(text), tables=sum(1 for b in blocks if b.kind == "table"),
                       truncated=truncated, truncated_reason="truncated_size" if truncated else None)
    return ParsedDocument(source=info, parser=parser, text=text, blocks=blocks, warnings=warnings, stats=stats,
                          **fields)


def _cut(text: str) -> tuple[str, bool]:
    from core.knowledge.parse.text import MAX_TEXT_CHARS

    return (text[:MAX_TEXT_CHARS], True) if len(text) > MAX_TEXT_CHARS else (text, False)


def _parse_text(raw: str, ext: str, info: SourceInfo, warnings: list[ParseWarning], *, name: str) -> ParsedDocument:
    from core.knowledge.parse import text as T

    if ext in (".csv", ".tsv"):
        clean, more = sanitize_text(raw)
        clean, truncated = _cut(clean)
        rendered, blocks, sheet_warnings = T.parse_delimited(clean, name=name, delimiter="\t" if ext == ".tsv" else ",")
        return _finish(rendered, blocks, info, TEXT_PARSER, warnings + more + sheet_warnings, truncated=truncated)
    if ext == ".json":
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if value is not None:
            rendered, truncated = T.render_json(value)
            clean, more = sanitize_text(rendered)
            return _finish(clean, T.parse_plain(clean), info, TEXT_PARSER, warnings + more, truncated=truncated)
    markdown = ext in (".md", ".markdown")
    clean, more = sanitize_text(raw, html_comments=markdown)
    clean, truncated = _cut(clean)
    if markdown:
        blocks, found = T.parse_markdown(clean)
        return _finish(clean, blocks, info, TEXT_PARSER, warnings + more, truncated=truncated,
                       title=found["title"], doc_date=found["doc_date"])
    return _finish(clean, T.parse_plain(clean), info, TEXT_PARSER, warnings + more, truncated=truncated)


# ── records ────────────────────────────────────────────────────────────────────

def _record(raw: str) -> dict | None:
    """The record a `_records/<id>.json` file holds, or None when it is some other JSON."""
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or ("text" in value) == ("json" in value):
        return None
    if "text" in value and not isinstance(value["text"], str):
        return None
    return value


def _parse_record(record: dict, info: SourceInfo, warnings: list[ParseWarning]) -> ParsedDocument:
    from core.knowledge.parse import text as T

    if "text" in record:
        clean, more = sanitize_text(record["text"])
        blocks = T.parse_plain(clean)
    else:
        rendered, _ = T.render_json(record["json"])
        clean, more = sanitize_text(rendered)
        blocks = T.parse_plain(clean)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    doc_date = record.get("doc_date")
    return _finish(clean, blocks, info, RECORD_PARSER, warnings + more,
                   title=record.get("title") if isinstance(record.get("title"), str) else None,
                   doc_date=doc_date if isinstance(doc_date, str) else None, metadata=metadata)
