# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""A knowledge base: one Vault folder made searchable, and how it is searched.

The record lives in the engine's `collections` under "knowledge_bases", like
skills and rubrics, keyed by `id`. It holds **only what a person chose** —
never runtime state (active snapshot, counts, pauses), which lives in the
knowledge tables. That matters because `collections.save` replaces the whole
list: `revision` is a compare-and-set, and a save from a stale revision is
refused (`StaleRevision`, a 409) rather than silently undoing someone's edit.

**Folder rules** (D153 #1): a KB is one folder tree; no two KBs overlap
(compared by path component); the Vault root itself is not a KB; the engine's
own folders (`tool_outputs`, `playwright`, `compaction_archives`) are refused
anywhere in the chain. Fetched web pages land in `<folder>/_web/<host>/…` and
records in `<folder>/_records/<id>.json`.

**What rebuilds the index.** A change to chunking, embedding, enrichment or
language changes `index_signature`, and a new snapshot is built beside the
serving one (blue/green); retrieval-only settings (top_k, rerank, fusion
weights) apply to the next search.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import unicodedata
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.knowledge.errors import KnowledgePathError
from core.knowledge.textsearch import TS_LANGUAGES

COLLECTION = "knowledge_bases"

#: The engine's own Vault folders. Equal to `core.routes.vault._ENGINE_OWNED`
#: (a test holds them equal; this module must not import a route module).
REFUSED_ROOTS: frozenset[str] = frozenset({"tool_outputs", "playwright", "compaction_archives"})

#: Settings whose change needs a new snapshot. Everything else applies at query time.
INDEX_AFFECTING: tuple[str, ...] = ("chunking", "embedding", "enrichment", "language")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chunking(_Model):
    mode: Literal["structure", "fixed", "page", "none"] = "structure"
    target_tokens: int = Field(default=450, ge=64, le=2000)
    max_tokens: int = Field(default=800, ge=64, le=4000)
    overlap_pct: float = Field(default=0.0, ge=0.0, le=0.25)

    @model_validator(mode="after")
    def _max_at_least_target(self) -> Chunking:
        if self.max_tokens < self.target_tokens:
            raise ValueError("max_tokens must be at least target_tokens")
        return self


class Embedding(_Model):
    """The embedding model. "" means the deployment's default (cloud: Titan v2 1024-d)."""

    model: str = ""
    dims: int | None = Field(default=None, ge=1, le=8192)
    profile: str | None = None


class Rerank(_Model):
    """None means the deployment's default: on in cloud, off in OSS unless configured."""

    enabled: bool | None = None
    model: str | None = None
    top_n: int = Field(default=8, ge=1, le=50)
    candidates: int = Field(default=50, ge=1, le=100)


class Fusion(_Model):
    method: Literal["rrf"] = "rrf"
    rrf_k: int = Field(default=60, ge=1, le=1000)
    weights: dict[str, float] = Field(default_factory=lambda: {"lexical": 1.0, "vector": 1.0})
    overfetch: int = Field(default=50, ge=10, le=200)

    @field_validator("weights")
    @classmethod
    def _legs(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) - {"lexical", "vector"}:
            raise ValueError("weights name only 'lexical' and 'vector'")
        if any(w < 0 for w in value.values()) or not any(w > 0 for w in value.values()):
            raise ValueError("weights are non-negative and at least one is positive")
        return value


class Retrieval(_Model):
    top_k: int = Field(default=8, ge=1, le=50)
    score_threshold: float | None = None
    parent_expansion: Literal["off", "section", "neighbors"] = "off"
    neighbor_window: int = Field(default=1, ge=0, le=3)
    max_context_tokens: int = Field(default=6000, ge=500, le=100_000)


class Enrichment(_Model):
    """"prefix" is deterministic and free; "contextual" is Contextual Retrieval, an opt-in
    behind a cost preview (D153 #3), on any chat model including the org's own."""

    mode: Literal["prefix", "contextual"] = "prefix"
    contextual_model: str | None = None
    extract_metadata: bool = False


class WebSource(_Model):
    id: str
    url: str = Field(max_length=2048)
    kind: Literal["page", "sitemap", "site"] = "page"
    include: list[str] = Field(default_factory=list, max_length=50)
    exclude: list[str] = Field(default_factory=list, max_length=50)


class Web(_Model):
    """Per-KB crawl settings, each bounded by the deployment's caps (`config.py`)."""

    max_depth: int = Field(default=3, ge=0, le=5)
    max_pages: int = Field(default=500, ge=1, le=100_000)
    rps_per_host: float = Field(default=1.0, gt=0, le=10)
    recrawl_days: int = Field(default=7, ge=0, le=365)  # 0 = never
    sources: list[WebSource] = Field(default_factory=list, max_length=50)


class KnowledgeBase(_Model):
    id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    folder: str
    status: Literal["active", "disabled"] = "active"
    language: str = "simple"
    visitor_access: bool = False  # D153 #12: and the tool must also be ticked on the widget
    budget_monthly_usd: float | None = Field(default=None, ge=0)
    chunking: Chunking = Field(default_factory=Chunking)
    embedding: Embedding = Field(default_factory=Embedding)
    rerank: Rerank = Field(default_factory=Rerank)
    fusion: Fusion = Field(default_factory=Fusion)
    retrieval: Retrieval = Field(default_factory=Retrieval)
    enrichment: Enrichment = Field(default_factory=Enrichment)
    web: Web = Field(default_factory=Web)
    revision: int = Field(default=1, ge=1)
    created_at: str = ""
    updated_at: str = ""

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        if value not in TS_LANGUAGES:
            raise ValueError(f"language is one of {', '.join(TS_LANGUAGES)}")
        return value

    @field_validator("folder")
    @classmethod
    def _folder(cls, value: str) -> str:
        return normalize_folder(value)


def normalize_folder(folder: str) -> str:
    """A KB folder as stored: NFC, POSIX, no leading/trailing slash, no `.` or `..`.

    Raises `KnowledgePathError` for the Vault root or an engine-owned folder.
    """
    raw = unicodedata.normalize("NFC", (folder or "").replace("\\", "/")).strip()
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise KnowledgePathError("A knowledge base is a folder inside the Vault, not the Vault itself.")
    if any(p == ".." for p in parts):
        raise KnowledgePathError("A knowledge base folder cannot contain '..'.")
    if any(p in REFUSED_ROOTS for p in parts):
        raise KnowledgePathError("Synapse's own folders (tool_outputs, playwright, compaction_archives) "
                                 "cannot be a knowledge base.")
    clean = "/".join(parts)
    if len(clean.encode("utf-8")) > 900:
        raise KnowledgePathError("A knowledge base folder path is at most 900 bytes.")
    return clean


def _components(path: str) -> tuple[str, ...]:
    return tuple(p for p in posixpath.normpath(path).split("/") if p not in ("", "."))


def folders_overlap(a: str, b: str) -> bool:
    """One folder is the other or inside it, by path component."""
    ca, cb = _components(a), _components(b)
    shorter = min(len(ca), len(cb))
    return ca[:shorter] == cb[:shorter]


def validate_folder(folder: str, others: Iterable[KnowledgeBase], *, self_id: str | None = None) -> str:
    """`folder` normalized, or `KnowledgePathError` if it overlaps another KB's."""
    clean = normalize_folder(folder)
    for other in others:
        if other.id != self_id and folders_overlap(clean, other.folder):
            raise KnowledgePathError(f"This folder overlaps the knowledge base {other.name!r} "
                                     f"({other.folder}). One file belongs to at most one knowledge base.")
    return clean


def kb_for_path(path: str, kbs: Iterable[KnowledgeBase]) -> KnowledgeBase | None:
    """The knowledge base whose folder holds the Vault path `path`, if any."""
    parts = _components(unicodedata.normalize("NFC", path.replace("\\", "/")))
    for kb in kbs:
        folder = _components(kb.folder)
        if kb.status == "active" and parts[:len(folder)] == folder and len(parts) > len(folder):
            return kb
    return None


def index_signature(kb: KnowledgeBase) -> str:
    """A hash of the settings that shape the index. Changes → a new snapshot."""
    shaped = {name: getattr(kb, name) for name in INDEX_AFFECTING}
    payload = {k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in shaped.items()}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
