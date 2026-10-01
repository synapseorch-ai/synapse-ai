"""The parser's limits, and the refusals that end a document before indexing.

Constants, each overridable by an environment variable, rather than settings:
they size the process that parses, so a deployment sets them where it sets the
process's memory. Cloud's knowledge worker keeps its ARQ `job_timeout` above
`parse_timeout_s()` (16 §8 → "A2"); `knowledge_max_file_mb` / `_pages`
(D153 #11) stay the per-file caps a person sees.

KB0 measured Docling (16 §8 → "KB0"): one pass over a 48-page PDF peaks at
2.4 GB, 8-page batches stay under 2 GB, about 2.5 s a page on 2 vCPUs. A2
measured the slim extra the same way: 4-page batches, 1.8 GB, 2.2 s a page.
"""
from __future__ import annotations

import os

#: Text files this size or smaller parse in a thread; anything bigger, and every
#: other format, in a subprocess. Spawning one costs about 0.3 s.
IN_PROCESS_MAX_BYTES = 2 * 1024 * 1024

#: Pages rendered for OCR, dots per inch.
OCR_DPI = 200
#: The job bumps a parsing document's `started_at` this often, so reconcile
#: (`sync.STUCK_AFTER`, 15 min) never takes a long parse for a stuck one.
HEARTBEAT_S = 60.0

#: An OOXML / ODF / EPUB zip: at most this many members, expanding to at most
#: this much in total, and no member over this ratio once it passes 1 MiB.
ZIP_MAX_MEMBERS = 10_000
ZIP_MAX_EXPANDED = 512 * 1024 * 1024
ZIP_MAX_RATIO = 200
ZIP_RATIO_FLOOR = 1024 * 1024

#: An email: attachments read per message, and how deep (the contract's
#: `MAX_ATTACHMENT_DEPTH` is 3).
MAX_ATTACHMENTS = 50


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def parse_timeout_s() -> int:
    """How long one document may parse, OCR not included."""
    return _env_int("SYNAPSE_KNOWLEDGE_PARSE_TIMEOUT_S", 1800)


def parse_memory_mb(*, docling: bool = False) -> int:
    """The parsing process's resident memory cap."""
    if docling:
        return _env_int("SYNAPSE_KNOWLEDGE_DOCLING_MEMORY_MB", 2048)
    return _env_int("SYNAPSE_KNOWLEDGE_PARSE_MEMORY_MB", 1024)


def docling_batch_pages() -> int:
    """Docling converts this many pages at a time. Measured on NIST AI 100-1 (48 pages, 2 vCPUs, a 2 GB
    cap): 8 pages peaked at 1,994 MB in 130 s, 4 pages at 1,789 MB in 105 s with the same output."""
    return _env_int("SYNAPSE_KNOWLEDGE_DOCLING_BATCH_PAGES", 4)


def ocr_timeout_s() -> int:
    """How long OCR may take for one document, all batches together."""
    return _env_int("SYNAPSE_KNOWLEDGE_OCR_TIMEOUT_S", 900)


# ── refusals ───────────────────────────────────────────────────────────────────

class ParseRefused(Exception):
    """The document ends at parse: `status` / `reason` from `status.REASONS`, `message` for people."""

    status = "failed"
    reason = "parser_crashed"

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class Encrypted(ParseRefused):
    """A password protects the file."""

    reason = "encrypted"


class OverPageCap(ParseRefused):
    """More pages than `knowledge_max_file_pages`: counted before any is parsed."""

    status = "skipped"
    reason = "over_page_cap"

    def __init__(self, pages: int, cap: int):
        super().__init__(f"The file has {pages} pages; the limit is {cap}.")
        self.pages = pages


class ExpandsTooFar(ParseRefused):
    """A compressed file (an Office zip) that would expand past the limits: a zip bomb, or simply too big."""

    status = "skipped"
    reason = "over_size_cap"


class ParserFailed(ParseRefused):
    """The parser timed out (`parser_timeout`), ran out of memory (`parser_memory`) or crashed
    (`parser_crashed`) before it read a single page."""
