"""What Knowledge knows about each embedding model: its width, its limits, how it tells a question from a passage,
and its price (B2a, D163).

A retrieval model is not just "a function from text to a vector". Each one has:

* **widths** — the native one, and for Matryoshka models the narrower ones it can answer
  (`text-embedding-3-*`, Gemini, Titan v2, Cohere v4);
* **limits** — how much text one input may hold, and how many inputs one call may carry;
* **a kind convention** — how it is told which side of a search a text is on:
  - `native`: the provider takes it as a parameter (Cohere's `input_type`, Gemini's `task_type`, our gateway's);
  - `prefix`: the text carries it (E5 `query: ` / `passage: `, nomic `search_query: ` / `search_document: `,
    BGE's query instruction, Qwen3's `Instruct: …`);
  - `none`: the model embeds both sides alike (OpenAI, Titan).

  Embedding a question with a passage's convention costs recall, silently, so a model whose convention is known
  is always given it;
* **a price** per million input tokens, the estimate a cost row carries when no operator rate is set
  (`model_pricing`) and no wallet charge comes back.

**Matched by the bare model name**, whatever its routing prefix: `ollama.nomic-embed-text`,
`oaic.nomic-ai/nomic-embed-text-v1.5` and `locv1.text-embedding-nomic-embed-text-v1.5` are all nomic. A model
none of these describes is **probed once** per process (one text embedded, its width kept) and treated as
symmetric, unpriced, with conservative limits.

Bump a profile's `version` when its convention or default width changes: a snapshot records the profile version
it was embedded with.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from core.knowledge.errors import EmbeddingError

KindConvention = Literal["native", "prefix", "none"]

#: Characters per token, for the input cap and for a token estimate when a provider reports none.
CHARS_PER_TOKEN = 4

#: Retrieval instructions published on the models' cards.
_BGE_QUERY = "Represent this sentence for searching relevant passages: "
_QWEN3_QUERY = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"


@dataclass(frozen=True)
class EmbeddingProfile:
    """One model family. `dims` is the width Synapse asks for when a knowledge base names none (None: learned by a
    probe). A Matryoshka model has `max_dims`, the widest it answers, and answers `widths` (empty: any width up
    to `max_dims`); a model with no `max_dims` answers `dims` only."""

    name: str
    dims: int | None
    max_dims: int | None = None
    widths: tuple[int, ...] = ()
    max_input_tokens: int = 512
    max_batch: int = 32
    kind: KindConvention = "none"
    query_prefix: str = ""
    document_prefix: str = ""
    usd_per_mtok: float | None = None
    version: str = "1"

    @property
    def profile_version(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def fixed(self) -> bool:
        """One width only: asking for any width means asking for nothing."""
        return self.max_dims is None

    def prepare(self, text: str, kind: str) -> str:
        """`text` as this model is given it: its kind's prefix, cut to the input cap."""
        if self.kind == "prefix":
            text = (self.query_prefix if kind == "query" else self.document_prefix) + text
        cap = self.max_input_tokens * CHARS_PER_TOKEN
        return text if len(text) <= cap else text[:cap]

    def input_type(self, kind: str) -> str | None:
        """What the provider is told about the kind: only a `native` model is told."""
        return kind if self.kind == "native" else None

    def request_dims(self, wanted: int | None) -> int | None:
        """The `dims` to send the provider for a knowledge base that asked for `wanted` (None: its default).

        None means "send nothing": the provider answers its native width. A width the model cannot answer is
        refused here, before any call is paid for.
        """
        width = wanted if wanted is not None else self.dims
        if width is None:
            return None
        if self.dims is not None and self.fixed:
            if width != self.dims:
                raise EmbeddingError(f"{self.name} answers {self.dims} dimensions only, not {width}.",
                                     retryable=False)
            return None
        if self.widths and width not in self.widths:
            allowed = ", ".join(str(w) for w in self.widths)
            raise EmbeddingError(f"{self.name} answers {allowed} dimensions, not {width}.", retryable=False)
        if self.max_dims is not None and width > self.max_dims:
            raise EmbeddingError(f"{self.name} answers at most {self.max_dims} dimensions, not {width}.",
                                 retryable=False)
        native = self.max_dims if self.max_dims is not None else self.dims
        return None if width == native else width


# ── the families ───────────────────────────────────────────────────────────────
# Prices: USD per 1M input tokens, checked 2026-10-01. OpenAI's three, Cohere v3 and Rerank on Bedrock: the
# providers' price pages. Titan v2: AWS's launch post ($0.00002 per 1k). Cohere v4: AWS's price list, read by KB0
# (D153). gemini-embedding-001: $0.15, quoted alike by Vertex resellers; Google's current Gemini API page lists
# only gemini-embedding-2 ($0.20). None: not priced.

_PROFILES: tuple[tuple[re.Pattern[str], EmbeddingProfile], ...] = tuple(
    (re.compile(pattern), profile) for pattern, profile in (
        # Amazon Titan (Bedrock, and our gateway's `titan-embed-v2`).
        (r"titan-embed(?:-text)?-v2", EmbeddingProfile(
            "titan-embed-text-v2", 1024, 1024, (256, 512, 1024), max_input_tokens=8192, max_batch=32,
            usd_per_mtok=0.02)),
        (r"titan-embed-text-v1|titan-embed-g1-text", EmbeddingProfile(
            "titan-embed-text-v1", 1536, max_input_tokens=8192, max_batch=32, usd_per_mtok=0.10)),
        # Cohere (Bedrock `cohere.embed-*`, our gateway's `cohere-embed-v4`).
        (r"cohere[.-]embed-v4", EmbeddingProfile(
            "cohere-embed-v4", 1536, 1536, (256, 512, 1024, 1536), max_input_tokens=128_000, max_batch=96,
            kind="native", usd_per_mtok=0.12)),
        (r"cohere[.-]embed-(?:english|multilingual)-v3", EmbeddingProfile(
            "cohere-embed-v3", 1024, max_input_tokens=512, max_batch=96, kind="native", usd_per_mtok=0.10)),
        # OpenAI. Matryoshka at any width up to the native one.
        (r"^text-embedding-3-small", EmbeddingProfile(
            "text-embedding-3-small", 1536, 1536, max_input_tokens=8191, max_batch=256, usd_per_mtok=0.02)),
        (r"^text-embedding-3-large", EmbeddingProfile(
            "text-embedding-3-large", 3072, 3072, max_input_tokens=8191, max_batch=256, usd_per_mtok=0.13)),
        (r"^text-embedding-ada-002", EmbeddingProfile(
            "text-embedding-ada-002", 1536, max_input_tokens=8191, max_batch=256, usd_per_mtok=0.10)),
        # Google. gemini-embedding-001 is 3,072 wide; Synapse asks for 768 unless told otherwise, as code
        # search always has (`services.code_indexer.EMBEDDING_MODEL_DIMS`).
        (r"^gemini-embedding-001", EmbeddingProfile(
            "gemini-embedding-001", 768, 3072, max_input_tokens=2048, max_batch=100, kind="native",
            usd_per_mtok=0.15)),
        (r"^gemini-embedding-2", EmbeddingProfile(
            "gemini-embedding-2", None, max_input_tokens=8192, max_batch=100, kind="native", usd_per_mtok=0.20)),
        (r"^(?:text-)?embedding-00\d", EmbeddingProfile(
            "gemini-text-embedding-004", 768, max_input_tokens=2048, max_batch=100, kind="native")),
        # Open models, wherever they run (Ollama, an OpenAI-compatible server). Widths vary by size: probed.
        (r"nomic-embed-text", EmbeddingProfile(
            "nomic-embed-text", None, max_input_tokens=8192, max_batch=64, kind="prefix",
            query_prefix="search_query: ", document_prefix="search_document: ")),
        (r"(?:^|[/._:-])(?:multilingual-)?e5-(?!mistral)", EmbeddingProfile(
            "e5", None, max_input_tokens=512, max_batch=64, kind="prefix", query_prefix="query: ",
            document_prefix="passage: ")),
        (r"bge-m3", EmbeddingProfile("bge-m3", None, max_input_tokens=8192, max_batch=32)),
        (r"bge-(?:small|base|large)(?:-en)?(?:-v1\.5)?(?:$|:|-en-v1\.5)", EmbeddingProfile(
            "bge-en-v1.5", None, max_input_tokens=512, max_batch=64, kind="prefix", query_prefix=_BGE_QUERY)),
        (r"mxbai-embed-large", EmbeddingProfile(
            "mxbai-embed-large", None, max_input_tokens=512, max_batch=64, kind="prefix", query_prefix=_BGE_QUERY)),
        (r"snowflake-arctic-embed(?:2|-[sml]-v2|.*v2\.0)", EmbeddingProfile(
            "snowflake-arctic-embed-v2", None, max_input_tokens=8192, max_batch=32, kind="prefix",
            query_prefix="query: ")),
        (r"snowflake-arctic-embed", EmbeddingProfile(
            "snowflake-arctic-embed", None, max_input_tokens=512, max_batch=64, kind="prefix",
            query_prefix=_BGE_QUERY)),
        (r"qwen3-embedding", EmbeddingProfile(
            "qwen3-embedding", None, max_input_tokens=8192, max_batch=16, kind="prefix", query_prefix=_QWEN3_QUERY)),
        (r"embeddinggemma", EmbeddingProfile(
            "embeddinggemma", None, max_input_tokens=2048, max_batch=32, kind="prefix",
            query_prefix="task: search result | query: ", document_prefix="title: none | text: ")),
        (r"all-minilm", EmbeddingProfile("all-minilm", None, max_input_tokens=256, max_batch=64)),
    )
)

#: A model none of the families describes: symmetric, unpriced, cautious limits; its width is probed.
UNKNOWN = EmbeddingProfile("unknown", None, max_input_tokens=2048, max_batch=32)


def bare_name(model: str) -> str:
    """The model's name without its routing prefix, lower-cased (`bedrock.`, `ollama.`, `oaic.`, `synapse.` …)."""
    from core.providers.registry import provider_for

    return provider_for(model).bare(model).strip().lower()


def profile_for(model: str) -> EmbeddingProfile | None:
    """The family `model` belongs to, or None."""
    name = bare_name(model)
    for pattern, profile in _PROFILES:
        if pattern.search(name):
            return profile
    return None


# ── probing ────────────────────────────────────────────────────────────────────

_probed: dict[tuple[str, str], int] = {}


def _probe_key(model: str, settings: Mapping[str, Any]) -> tuple[str, str]:
    """A model's width depends on the server that answers it (two Ollamas may hold different builds)."""
    from core.providers.registry import provider_for

    spec = provider_for(model)
    return model, str(settings.get(spec.address) or "") if spec.address else ""


async def resolve(model: str, settings: Mapping[str, Any]) -> EmbeddingProfile:
    """`model`'s profile, with its width filled in: a known width, or one probe per process.

    The probe embeds one short text as a document (the same call indexing makes). It raises what the call
    raised — the caller maps it — and refuses an answer with no usable vector.
    """
    profile = profile_for(model) or UNKNOWN
    if profile.dims is not None:
        return profile
    key = _probe_key(model, settings)
    width = _probed.get(key)
    if width is None:
        from core import llm_providers as llm

        answer = await llm.embed_batch_ex([profile.prepare("dimension probe", "document")], model, dict(settings))
        width = len(answer.vectors[0]) if answer.vectors else 0
        if width <= 0:
            raise EmbeddingError(f"{model} answered the width probe with no vector.", retryable=False)
        _probed[key] = width
    return replace(profile, dims=width, max_dims=None, widths=())  # one width: the one it answered


def forget_probes() -> None:
    """Tests, and a settings change that may point a model id at another server."""
    _probed.clear()


def estimate_tokens(texts: Sequence[str]) -> int:
    """A token count for a provider that reports none."""
    return sum(max(1, len(t) // CHARS_PER_TOKEN) for t in texts)
