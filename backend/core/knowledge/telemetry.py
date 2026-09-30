# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's span names, and the attributes they may carry.

The attributes are declared in `core.telemetry.attributes` (the allowlist: an
attribute not listed there is dropped before export); this module names the
spans and re-exports the keys for the lanes that open them. The rule is the
package's: ids, counts, codes, cost and timings — never a query, a chunk's
text, a file name, a URL or a MIME string (`format` is a fixed list).
"""
from __future__ import annotations

from core.telemetry.attributes import (  # noqa: F401 — re-exported for the lanes
    KNOWLEDGE_CANDIDATES, KNOWLEDGE_CHUNKS, KNOWLEDGE_COST_SOURCE, KNOWLEDGE_DEGRADED, KNOWLEDGE_DOC_ID,
    KNOWLEDGE_FILTERED, KNOWLEDGE_FORMAT, KNOWLEDGE_HITS, KNOWLEDGE_INDEX_KIND, KNOWLEDGE_KB_COUNT,
    KNOWLEDGE_KB_ID, KNOWLEDGE_LEG, KNOWLEDGE_PAGES, KNOWLEDGE_PARSER, KNOWLEDGE_QUERY_ID, KNOWLEDGE_REASON,
    KNOWLEDGE_RERANKED, KNOWLEDGE_SNAPSHOT_ID, KNOWLEDGE_STAGE, KNOWLEDGE_UNIT, KNOWLEDGE_UNITS,
)

SPAN_SEARCH = "synapse.knowledge.search"
SPAN_EMBED = "synapse.knowledge.embed"
SPAN_LEG = "synapse.knowledge.leg"
SPAN_RERANK = "synapse.knowledge.rerank"
SPAN_INGEST = "synapse.knowledge.ingest"
SPAN_PARSE = "synapse.knowledge.parse"
SPAN_OCR = "synapse.knowledge.ocr"
SPAN_CHUNK = "synapse.knowledge.chunk"
SPAN_ENRICH = "synapse.knowledge.enrich"
SPAN_INDEX = "synapse.knowledge.index"
SPAN_SNAPSHOT_BUILD = "synapse.knowledge.snapshot.build"

SPANS: tuple[str, ...] = (
    SPAN_SEARCH, SPAN_EMBED, SPAN_LEG, SPAN_RERANK, SPAN_INGEST, SPAN_PARSE, SPAN_OCR, SPAN_CHUNK,
    SPAN_ENRICH, SPAN_INDEX, SPAN_SNAPSHOT_BUILD,
)

#: `KNOWLEDGE_FORMAT`'s values. Anything else is "other".
FORMATS: tuple[str, ...] = (
    "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "csv", "tsv", "odt", "ods", "odp", "rtf", "txt",
    "md", "html", "eml", "msg", "epub", "json", "xml", "yaml", "code", "image", "record", "other",
)
