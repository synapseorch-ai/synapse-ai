"""A small synthetic corpus whose rankings are known.

Two knowledge bases, two snapshots of the first, sixteen chunks each (48 in
all), for whichever tenant `entries()` is built in. Vectors are 8-dimensional
and built from a few directions, so exact cosine order is easy to reason about
and ties are deliberate. Metadata has the traps a filter must handle: missing
keys, a number stored as a string, lists, booleans, accents, mixed case.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from core.knowledge.ids import chunk_id
from core.knowledge.types import IndexEntry, Scope

KB_A = "kb_00000000000a"
KB_B = "kb_00000000000b"
SNAP_A1 = "ksnap_" + "a1" * 16
SNAP_A2 = "ksnap_" + "a2" * 16
SNAP_B1 = "ksnap_" + "b1" * 16
SCOPE_A1 = Scope(KB_A, SNAP_A1)
SCOPE_A2 = Scope(KB_A, SNAP_A2)
SCOPE_B1 = Scope(KB_B, SNAP_B1)
DIMS = 8


def doc_id(n: int) -> str:
    return "kdoc_" + f"{n:032x}"


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def direction(i: int, j: int | None = None, w: float = 0.0) -> list[float]:
    """Axis `i`, tilted toward axis `j` by weight `w`, normalized."""
    v = [0.0] * DIMS
    v[i] = 1.0
    if j is not None:
        v[j] = w
    return _unit(v)


#: (ordinal, doc number, path, text, vector, metadata)
_ROWS: list[tuple[int, int, str, str, list[float] | None, dict[str, Any]]] = [
    (0, 1, "contracts/acme.pdf", "Master services agreement with Acme for data processing.",
     direction(0), {"doc_type": "contract", "year": 2024, "tags": ["legal", "urgent"], "region": "EU",
                    "confidential": True, "amount": 125000.5}),
    (1, 1, "contracts/acme.pdf", "Termination clause: either party may terminate with ninety days notice.",
     direction(0, 1, 0.2), {"doc_type": "contract", "year": 2024, "tags": ["legal"], "region": "EU",
                            "confidential": True}),
    (2, 1, "contracts/acme.pdf", "Renewal happens automatically each year unless cancelled.",
     direction(0, 2, 0.5), {"doc_type": "contract", "year": 2024, "region": "EU"}),
    (3, 2, "contracts/zurich.docx", "Vertrag mit der Zürich Versicherung über Haftpflicht.",
     direction(1), {"doc_type": "contract", "year": 2023, "tags": ["legal", "de"], "region": "CH",
                    "language": "de"}),
    (4, 2, "contracts/zurich.docx", "Kündigung: dreißig Tage zum Monatsende.",
     direction(1, 0, 0.3), {"doc_type": "contract", "year": 2023, "region": "CH", "language": "de"}),
    (5, 3, "contracts2/beta.pdf", "Invoice schedule for the Beta renewal, payable quarterly.",
     direction(2), {"doc_type": "invoice", "year": "2024", "tags": ["finance"], "region": "US"}),
    (6, 3, "contracts2/beta.pdf", "Late payment accrues interest at one percent per month.",
     direction(2, 3, 0.4), {"doc_type": "invoice", "year": 2025, "region": "US", "amount": 900}),
    (7, 4, "reports/q3.md", "Quarterly report: revenue grew twelve percent; churn fell.",
     direction(3), {"doc_type": "report", "year": 2025, "tags": ["finance", "board"], "confidential": False}),
    (8, 4, "reports/q3.md", "Risks: supplier concentration and currency exposure.",
     direction(3, 4, 0.6), {"doc_type": "report", "year": 2025, "tags": []}),
    (9, 5, "reports/Q4 notes.txt", "Notes on the Q4 plan. INVOICE totals were reconciled.",
     direction(4), {"doc_type": "Report", "year": 2025.0, "tags": ["Finance"]}),
    (10, 6, "_records/rec-1.json", "Ticket 4411: customer cannot reset password.",
     direction(5), {"doc_type": "ticket", "priority": 1, "tags": ["support"]}),
    (11, 6, "_records/rec-1.json", "Ticket 4411 resolution: reset link expired; resent.",
     direction(5), {"doc_type": "ticket", "priority": 1}),
    (12, 7, "_web/example.com/pricing.md", "Pricing: the team plan costs twenty dollars a seat.",
     direction(6), {"doc_type": "web", "tags": ["pricing"], "region": "US"}),
    (13, 7, "_web/example.com/pricing.md", "Enterprise pricing is available on request.",
     direction(6, 7, 0.1), {"doc_type": "web"}),
    (14, 8, "misc/empty-meta.txt", "A note with no metadata at all about data processing.",
     direction(7), {}),
    (15, 8, "misc/empty-meta.txt", "A parent section, stored and never embedded.",
     None, {}),
]

_BUILTINS = {
    1: ("upload", "application/pdf", "Acme MSA", "2024-03-01"),
    2: ("upload", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Zürich Vertrag",
        "2023-11-15"),
    3: ("upload", "application/pdf", "Beta invoice", "2024-06-30"),
    4: ("vault", "text/markdown", "Q3 report", "2025-10-01"),
    5: ("vault", "text/plain", None, None),
    6: ("record", "application/json", "Ticket 4411", "2025-01-20"),
    7: ("web", "text/markdown", "Pricing", None),
    8: ("vault", "text/plain", None, None),
}
_INDEXED_AT = {1: "2026-09-01T10:00:00+00:00", 2: "2026-09-02T10:00:00+00:00", 3: "2026-09-03T10:00:00+00:00",
               4: "2026-09-04T10:00:00+00:00", 5: "2026-09-05T10:00:00+00:00", 6: "2026-09-06T10:00:00+00:00",
               7: "2026-09-07T10:00:00+00:00", 8: "2026-09-08T10:00:00+00:00"}


def fields_for(doc: int, path: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    source, mime, title, doc_date = _BUILTINS[doc]
    return {"path": path, "source": source, "mime": mime, "title": title, "doc_date": doc_date,
            "indexed_at": _INDEXED_AT[doc], **metadata}


def entries(scope: Scope = SCOPE_A1, *, doc_offset: int = 0) -> list[IndexEntry]:
    """The sixteen chunks in `scope`. `doc_offset` makes distinct document ids for another KB."""
    out = []
    for ordinal, doc, path, text, vector, metadata in _ROWS:
        d = doc_id(doc + doc_offset)
        out.append(IndexEntry(
            chunk_id=chunk_id(d, scope.snapshot_id, 1, ordinal), doc_id=d, kb_id=scope.kb_id,
            snapshot_id=scope.snapshot_id, text=text, lang="simple", vector=vector,
            fields=fields_for(doc, path, metadata),
        ))
    return out


def all_entries() -> list[IndexEntry]:
    """KB A's two snapshots and KB B's one: 48 entries."""
    return entries(SCOPE_A1) + entries(SCOPE_A2) + entries(SCOPE_B1, doc_offset=100)


def rows() -> list[dict[str, Any]]:
    """Each chunk's filterable fields with its ordinal as `_id`, for filter tests."""
    return [{"_id": ordinal, **fields_for(doc, path, metadata)} for ordinal, doc, path, _, _, metadata in _ROWS]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def records_for(entries: list[IndexEntry], tenant: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`knowledge_documents` and `knowledge_chunks` row values for `entries`, in `tenant`.

    For an index that writes onto rows of its own tables (the SQL and Postgres
    indexes): a conformance harness inserts these before `upsert`. Built-in
    fields become columns; everything else is `meta`. Chunks without a vector
    are parents.
    """
    from datetime import datetime

    from core.knowledge.filters import BUILTINS

    documents: dict[str, dict[str, Any]] = {}
    chunks: list[dict[str, Any]] = []
    ordinals: dict[tuple[str, str], int] = {}
    for e in entries:
        f = dict(e.fields)
        meta = {k: v for k, v in f.items() if k not in BUILTINS}
        indexed_at = datetime.fromisoformat(f["indexed_at"]) if f.get("indexed_at") else None
        documents.setdefault(e.doc_id, {
            "id": e.doc_id, "tenant_id": tenant, "kb_id": e.kb_id, "path": f["path"], "source": f["source"],
            "status": "indexed", "version": 1, "mime": f.get("mime"), "title": f.get("title"),
            "doc_date": f.get("doc_date"), "meta": meta, "indexed_at": indexed_at,
        })
        key = (e.doc_id, e.snapshot_id)
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        chunks.append({
            "chunk_id": e.chunk_id, "tenant_id": tenant, "kb_id": e.kb_id, "doc_id": e.doc_id,
            "snapshot_id": e.snapshot_id, "doc_version": 1, "ordinal": ordinal, "kind": "text",
            "role": "leaf" if e.vector is not None else "parent", "content": e.text, "context": "",
            "char_start": 0, "char_end": len(e.text), "lang": e.lang, "path": f["path"], "source": f["source"],
            "mime": f.get("mime"), "title": f.get("title"), "doc_date": f.get("doc_date"),
            "indexed_at": indexed_at, "meta": meta,
        })
    return list(documents.values()), chunks
