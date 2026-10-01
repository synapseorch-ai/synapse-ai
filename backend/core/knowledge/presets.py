"""Pipeline presets: named starting points for how a knowledge base is chunked and searched (B2b, D167).

A preset is a set of fields, not whole settings blocks, so applying one keeps what it does not name (a
rerank model someone chose, the embedding model, the budget). Nothing stores which preset a base used: the
settings screen applies one to its draft, and says which one a base matches (`matches`).

* **Cheap** — fewer, larger chunks; no rerank (on cloud, a rerank is $0.002 a search); fewer candidates and
  passages; a smaller context.
* **Balanced** — the defaults.
* **High recall** — smaller chunks with 10% overlap; more candidates for fusion and the reranker; more
  passages, each with its neighbours; a larger context. More tokens to embed: the cost preview says how many.

A preset never picks an embedding model (that depends on what the deployment has) and never turns rerank
*on*: an install with no reranker would read `rerank_unconfigured` on every search. "High recall" leaves it
at the deployment's default.

`lib/knowledge.ts` mirrors `PRESETS` for the settings screen; `test_knowledge_presets.py` holds the two equal.
Pure: cloud's API process may import it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    #: block → field → value: only these fields are set.
    fields: dict[str, dict[str, Any]]


PRESETS: tuple[Preset, ...] = (
    Preset(
        id="cheap", name="Cheap",
        description="Larger chunks, no rerank, fewer passages. The least to embed and to search.",
        fields={
            "chunking": {"mode": "structure", "target_tokens": 600, "max_tokens": 1000, "overlap_pct": 0.0},
            "rerank": {"enabled": False, "top_n": 8, "candidates": 50},
            "fusion": {"overfetch": 20},
            "retrieval": {"top_k": 5, "parent_expansion": "off", "neighbor_window": 1, "max_context_tokens": 4000},
            "enrichment": {"mode": "prefix"},
        }),
    Preset(
        id="balanced", name="Balanced",
        description="The defaults: section-sized chunks, rerank where one is configured, eight passages.",
        fields={
            "chunking": {"mode": "structure", "target_tokens": 450, "max_tokens": 800, "overlap_pct": 0.0},
            "rerank": {"enabled": None, "top_n": 8, "candidates": 50},
            "fusion": {"overfetch": 50},
            "retrieval": {"top_k": 8, "parent_expansion": "off", "neighbor_window": 1, "max_context_tokens": 6000},
            "enrichment": {"mode": "prefix"},
        }),
    Preset(
        id="high_recall", name="High recall",
        description="Smaller overlapping chunks, more candidates and passages, each with its neighbours. "
                    "More to embed and to read.",
        fields={
            "chunking": {"mode": "structure", "target_tokens": 300, "max_tokens": 600, "overlap_pct": 0.1},
            "rerank": {"enabled": None, "top_n": 12, "candidates": 100},
            "fusion": {"overfetch": 100},
            "retrieval": {"top_k": 12, "parent_expansion": "neighbors", "neighbor_window": 1,
                          "max_context_tokens": 10000},
            "enrichment": {"mode": "prefix"},
        }),
)

BY_ID: dict[str, Preset] = {p.id: p for p in PRESETS}


def get(preset_id: str) -> Preset:
    """The preset, or `ValueError` naming the ones there are."""
    try:
        return BY_ID[preset_id]
    except KeyError:
        raise ValueError(f"No preset {preset_id!r}; the presets are {', '.join(BY_ID)}.") from None


def apply(kb: KnowledgeBase, preset_id: str) -> KnowledgeBase:
    """`kb` with the preset's fields set and everything else kept."""
    from core.knowledge.kb_settings import KnowledgeBase

    data = kb.model_dump(mode="json")
    for block, fields in get(preset_id).fields.items():
        data[block] = {**data[block], **copy.deepcopy(fields)}
    return KnowledgeBase.model_validate(data)


def matches(kb: KnowledgeBase) -> str | None:
    """The preset whose every field `kb` has, or None (custom settings)."""
    data = kb.model_dump(mode="json")
    for preset in PRESETS:
        if all(data[block].get(name) == value for block, fields in preset.fields.items()
               for name, value in fields.items()):
            return preset.id
    return None


def as_json() -> list[dict[str, Any]]:
    """The presets as the settings screen reads them."""
    return [{"id": p.id, "name": p.name, "description": p.description, "fields": copy.deepcopy(p.fields)}
            for p in PRESETS]
