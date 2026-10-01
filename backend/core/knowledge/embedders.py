"""`ProviderEmbedder` — Knowledge's default embedder: any embedding model the provider table can call (B2a, D163).

`seams.DEFAULT_EMBEDDER` names it, so every deployment that does not plug its own in embeds through it: OSS with
whatever model the workspace chose (`knowledge_embedding_model`, else `embedding_model`), a knowledge base with its
own (`kb.embedding.model`), a `synapse.*` model through our gateway.

For each call it:

1. finds the model's profile (`profiles.resolve`: a known family, or one probe) and checks the width asked for;
2. prepares each text the way the model expects to tell a question from a passage — a prefix, or the provider's
   own `input_type` / `task_type` — and cuts it to the model's input cap;
3. sends the texts in the model's batch size through `llm_providers.embed_batch_ex`, which raises rather than
   answer zeros (the seam's guard checks the vectors again);
4. answers one `Usage` for the whole call: what the provider reported (or an estimate), priced from the
   operator's rate (`model_pricing`) or the profile's, and the wallet's charge when our gateway settled one.

**Errors.** A missing key or server address is `EmbedderUnavailable`: indexing skips the embed stage and the
chunks stay searchable by keyword until a backfill embeds them (`backfill.py`). Our gateway's 402 is
`WalletExhausted` (the document pauses). Everything else is `EmbeddingError`, retryable when the provider's
failure is (a rate limit, an outage). When a call fails, what it already cost is recorded in the cost ledger before
the error is raised, because no `Usage` will carry it: the slices answered before the failing one (their tokens and
our gateway's charges) and whatever the failing slice itself was charged (F329).

`llm_providers` is imported inside the functions: cloud's API process imports the seams, and it has no provider
SDKs to load.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.knowledge import profiles
from core.knowledge.errors import EmbedderUnavailable, EmbeddingError, KnowledgeError, WalletExhausted
from core.knowledge.types import Attribution, CostSource, EmbeddingSpec, EmbedKind, EmbedResult, Usage
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

#: The pipeline slices its chunks by this; each model's own batch size is applied inside.
MAX_BATCH = 512

#: Providers whose model runs on the customer's own machine: $0.
LOCAL_PROVIDERS = frozenset({"ollama", "local_compatible"})
PLATFORM_PROVIDERS = frozenset({"model_gateway"})


def cost_source(provider_id: str) -> CostSource:
    """Who pays: our gateway's wallet, a model on the customer's machine, or the customer's own key."""
    if provider_id in PLATFORM_PROVIDERS:
        return "platform"
    if provider_id in LOCAL_PROVIDERS:
        return "local"
    return "byok"


async def usd_per_mtok(model: str, profile: profiles.EmbeddingProfile) -> float | None:
    """The rate an estimate uses: the operator's (`model_pricing`, by the id or its bare name), else the
    profile's, else None (unpriced)."""
    try:
        from core import usage_tracker

        table = await usage_tracker._ensure_pricing()
        for name in (model, profiles.bare_name(model)):
            entry = usage_tracker._resolve_pricing_entry(name, table)
            if entry and entry.get("input_per_1m") is not None:
                return float(entry["input_per_1m"])
    except Exception as exc:  # noqa: BLE001 — a price never fails an embedding
        _log.debug(f"knowledge: no operator rate for {model}: {exc}")
    return profile.usd_per_mtok


def _knowledge_error(exc: Exception, model: str) -> KnowledgeError:
    """An `EmbeddingFailed` as the Knowledge error the pipeline maps to a status."""
    if getattr(exc, "missing", None):
        return EmbedderUnavailable(f"{model}: {exc}")
    if getattr(exc, "status", None) == 402 and getattr(exc, "provider", None) == "model_gateway":
        return WalletExhausted(str(exc), needed_microusd=getattr(exc, "needed_microusd", None),
                               available_microusd=getattr(exc, "available_microusd", None))
    return EmbeddingError(f"{model}: {exc}", retryable=bool(getattr(exc, "retryable", False)))


async def _record_failed_charge(exc: Exception, *, model: str, provider: str, attribution: Attribution,
                                tokens: int = 0, charged: int | None = None,
                                profile: profiles.EmbeddingProfile | None = None) -> None:
    """Record what a failed call cost: `tokens` and `charged` from the slices answered before the failing one, plus
    whatever the failing slice was charged. A settled charge is the cost; without one, the tokens at the model's
    rate (a key of the customer's own was billed for them). Nothing answered and nothing charged: no row."""
    failed = getattr(exc, "charged_microusd", None)
    settled = (charged or 0) + (int(failed) if failed else 0) if (charged is not None or failed) else None
    if not settled and tokens <= 0:
        return
    from core.knowledge.ledger import CostRow, record_costs

    source = cost_source(provider)
    if settled:
        est, rate = settled / 1_000_000, 0.0
    else:
        rate = 0.0 if source == "local" else await usd_per_mtok(model, profile or profiles.UNKNOWN)
        est = round(tokens * (rate or 0.0) / 1_000_000, 10)
    usage = Usage(stage="embed", cost_source=source, model=model, provider=provider, input_tokens=tokens,
                  units=float(tokens), unit="tokens", est_usd=est, charged_microusd=settled,
                  priced=bool(settled) or rate is not None)
    await record_costs([CostRow(usage, attribution, details={"failed": str(exc)[:500]})])


async def snapshot_spec(kb: KnowledgeBase, snapshot_id: str, settings: Mapping[str, Any]) -> EmbeddingSpec:
    """What a snapshot's vectors are embedded with: **the model it recorded**, once it has one (Naveen, D163 #3),
    else the knowledge base's own model, else the deployment's (`knowledge_embedding_model`, then
    `embedding_model`). A changed default therefore never puts two models' vectors in one snapshot, where vectors
    of the same width would compare as if they meant the same thing; moving a knowledge base to another model is
    a new snapshot (blue/green, B2b). An empty `model` means none is configured."""
    from core.knowledge.config import knowledge_settings
    from core.knowledge.snapshots import snapshot_info

    info = (await snapshot_info([snapshot_id])).get(snapshot_id) or {}
    if info.get("embed_model"):
        return EmbeddingSpec(model=info["embed_model"], dims=info.get("embed_dims"), profile=kb.embedding.profile)
    return EmbeddingSpec(model=kb.embedding.model or knowledge_settings(settings).embedding_model,
                         dims=kb.embedding.dims, profile=kb.embedding.profile)


def profile_version(model: str) -> str | None:
    """The version a snapshot records for `model`'s profile, or None for a model no profile describes."""
    found = profiles.profile_for(model)
    return found.profile_version if found else None


class ProviderEmbedder:
    """The default `KnowledgeEmbedder` (see the module docstring)."""

    max_batch = MAX_BATCH

    async def embed(self, texts: Sequence[str], *, kind: EmbedKind, spec: EmbeddingSpec,
                    settings: Mapping[str, Any], attribution: Attribution) -> EmbedResult:
        from core import llm_providers as llm
        from core.providers.registry import provider_for

        model = (spec.model or "").strip()
        if not model:
            raise EmbedderUnavailable("No embedding model is configured (Settings → Models, or the knowledge "
                                      "base's own).")
        provider = provider_for(model)
        if provider.embed_ex is None:
            raise EmbedderUnavailable(f"{provider.label} does not embed ({model}).")
        call_settings = dict(settings)
        t0 = time.monotonic()
        try:
            profile = await profiles.resolve(model, call_settings)
        except llm.EmbeddingFailed as exc:
            await _record_failed_charge(exc, model=model, provider=provider.id, attribution=attribution)
            raise _knowledge_error(exc, model) from exc
        dims = profile.request_dims(spec.dims)
        prepared = [profile.prepare(t, kind) for t in texts]
        # Our gateway takes the kind for every model and passes it on only where the model reads it; it also sets
        # how long the call may take, so a question is never given a document batch's wait (F329).
        input_type = profile.input_type(kind) or (kind if provider.id == "model_gateway" else None)

        vectors: list[list[float]] = []
        tokens = 0
        charged: int | None = None
        request_id: str | None = None
        step = max(1, profile.max_batch)
        for i in range(0, len(prepared), step):
            batch = prepared[i:i + step]
            try:
                answer = await llm.embed_batch_ex(batch, model, call_settings, input_type=input_type, dims=dims)
            except llm.EmbeddingFailed as exc:
                await _record_failed_charge(exc, model=model, provider=provider.id, attribution=attribution,
                                            tokens=tokens, charged=charged, profile=profile)
                raise _knowledge_error(exc, model) from exc
            vectors.extend(answer.vectors)
            tokens += answer.input_tokens or profiles.estimate_tokens(batch)
            if answer.charged_microusd is not None:
                charged = (charged or 0) + answer.charged_microusd
            request_id = request_id or answer.request_id

        source = cost_source(provider.id)
        if source == "local":
            rate: float | None = 0.0
        else:
            rate = await usd_per_mtok(model, profile)
        usage = Usage(
            stage="embed", cost_source=source, model=model, provider=provider.id, input_tokens=tokens,
            units=float(tokens), unit="tokens", est_usd=round(tokens * (rate or 0.0) / 1_000_000, 10),
            charged_microusd=charged, priced=rate is not None, latency_ms=int((time.monotonic() - t0) * 1000),
            request_id=request_id,
        )
        return EmbedResult(vectors=vectors, model=model, dims=len(vectors[0]), usage=usage)
