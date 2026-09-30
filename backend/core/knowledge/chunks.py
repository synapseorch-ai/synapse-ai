# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""A chunk: what the chunker cuts a `ParsedDocument` into, and what a search returns.

* `kind` — "text", "table" (never split across chunks) or "sheet_card" (one
  per spreadsheet sheet: schema, headers, sample rows).
* `role` — "leaf" chunks are indexed and searched; "parent" chunks are the
  sections leaves expand into (`parent_id`), stored but never embedded.
* `content` is the chunk's own text; `context` is the enrichment prefix
  (title / type / date / heading path by default, a Contextual-Retrieval
  sentence when a KB opts in). The keyword and vector legs see
  `context + " " + content`; a citation quotes only `content`.
* `char_start:char_end` is the span in the parsed document's canonical text,
  so `parsed.text[char_start:char_end] == content` for a text chunk.
* `id` is `ids.chunk_id(doc_id, snapshot_id, doc_version, ordinal)`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.knowledge.parsed import BBox

ChunkKind = Literal["text", "table", "sheet_card"]
ChunkRole = Literal["leaf", "parent"]
CHUNK_KINDS: tuple[str, ...] = ("text", "table", "sheet_card")
CHUNK_ROLES: tuple[str, ...] = ("leaf", "parent")


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    doc_id: str
    kb_id: str
    snapshot_id: str
    doc_version: int = Field(ge=0)
    doc_sha256: str
    ordinal: int = Field(ge=0)
    kind: ChunkKind = "text"
    role: ChunkRole = "leaf"
    parent_id: str | None = None
    content: str
    context: str = ""
    heading_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    bbox: list[BBox] | None = None
    tokens: int = Field(default=0, ge=0)
    lang: str = "simple"
    block_ids: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    def search_text(self) -> str:
        """What the keyword and vector legs see."""
        return f"{self.context} {self.content}".strip() if self.context else self.content
