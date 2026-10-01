# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""Knowledge's ids and the two folder names every KB reserves.

Every id is minted by the server, never taken from a client. Table keys carry
32 hex characters (128 bits): a rubric's 12 (`core/evals/store.py`) would give
roughly an 18% chance of one collision somewhere by ten million chunks.

A knowledge base id is the exception: it is a key in the tenant's
`knowledge_bases` collection, read by people in URLs and agent settings, and a
tenant has tens of them, not millions.

A chunk's id is *derived*, not random: the same document version, snapshot and
position always make the same id, so a retried ingestion overwrites what a
failed attempt half-wrote — in our tables and in an external vector store —
instead of leaving a second copy.
"""
from __future__ import annotations

import hashlib
import re
import uuid

KB_PREFIX = "kb_"
DOCUMENT_PREFIX = "kdoc_"
SNAPSHOT_PREFIX = "ksnap_"
QUERY_PREFIX = "kq_"
CHUNK_PREFIX = "kch_"
UPLOAD_PREFIX = "kup_"
CRAWL_PREFIX = "kcr_"
EVENT_PREFIX = "kev_"
WEB_SOURCE_PREFIX = "kws_"
#: CCR-6 (V1, D168): a question set (a `knowledge_eval_sets` collection key) and one run of it.
EVAL_SET_PREFIX = "kset_"
EVAL_RUN_PREFIX = "krun_"

#: Under a KB's folder: fetched web pages (`_web/<host>/…`) and records sent by
#: the API or the Ingest step (`_records/<record_id>.json`).
WEB_DIR = "_web"
RECORDS_DIR = "_records"

#: A record's id, chosen by the caller so a second send of the same record
#: replaces the first. It becomes a file name, so it is kept to a safe alphabet.
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_HEX32 = r"[0-9a-f]{32}"
_PATTERNS = {
    KB_PREFIX: re.compile(r"^kb_[0-9a-f]{12}$"),
    WEB_SOURCE_PREFIX: re.compile(r"^kws_[0-9a-f]{12}$"),
    **{p: re.compile(rf"^{p}{_HEX32}$") for p in (
        DOCUMENT_PREFIX, SNAPSHOT_PREFIX, QUERY_PREFIX, CHUNK_PREFIX,
        UPLOAD_PREFIX, CRAWL_PREFIX, EVENT_PREFIX, EVAL_SET_PREFIX, EVAL_RUN_PREFIX,
    )},
}


def new_kb_id() -> str:
    return KB_PREFIX + uuid.uuid4().hex[:12]


def new_web_source_id() -> str:
    return WEB_SOURCE_PREFIX + uuid.uuid4().hex[:12]


def new_id(prefix: str) -> str:
    """A 32-hex id for one of the table prefixes above."""
    if prefix not in _PATTERNS or prefix in (KB_PREFIX, WEB_SOURCE_PREFIX, CHUNK_PREFIX):
        raise ValueError(f"not a random-id prefix: {prefix!r}")
    return prefix + uuid.uuid4().hex


def chunk_id(doc_id: str, snapshot_id: str, doc_version: int, ordinal: int) -> str:
    """The chunk at `ordinal` of `doc_id`'s version `doc_version` in `snapshot_id`.

    Deterministic: see the module docstring. The unit separator keeps
    ("a", "bc") and ("ab", "c") apart.
    """
    raw = "\x1f".join((doc_id, snapshot_id, str(int(doc_version)), str(int(ordinal))))
    return CHUNK_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_id(value: object, prefix: str) -> bool:
    pattern = _PATTERNS.get(prefix)
    return isinstance(value, str) and pattern is not None and bool(pattern.match(value))


def is_record_id(value: object) -> bool:
    return isinstance(value, str) and bool(RECORD_ID_RE.match(value))
