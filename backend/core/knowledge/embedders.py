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
our gateway's charges) and whatever the failing slice itself was charged (F329). The error carries the vectors those
slices answered (`answered_vectors`), which indexing keeps across a pause so they are not bought twice (F345).

**A rate limit is waited out** (PG1) for a document's texts while the indexing pipeline asks for it
(`pacing.paced`): the slice waits, is halved and is sent again, and the call fails only after ten minutes with nothing
answered. Each answered slice is counted on the document's progress. **An outage** (a 5xx, a timeout) is tried
again there too, twice, after 2 s and 8 s (F418); then the document fails `embed_failed` with the reason, retryable.

`llm_providers` is imported inside the functions: cloud's API process imports the seams, and it has no provider
SDKs to load.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.knowledge import pacing, profiles, progress
from core.knowledge.errors import EmbedderUnavailable, EmbeddingError, KnowledgeError, WalletExhausted
from core.knowledge.types import Attribution, CostSource, EmbeddingSpec, EmbedKind, EmbedResult, Usage
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

#: The pipeline slices its chunks by this; each model's own batch size is applied inside.
MAX_BATCH = 512
#: Tries per slice when indexing meets an outage — a 5xx or a timeout, not a rate limit, which is waited out —
#: and the waits between them (F418), the shape of a rebuild's `snapshots.DOC_ATTEMPTS` / `BACKOFF_S`. A document
#: used to fail for good on one transient 503 and wait for a person's Retry.
SLICE_ATTEMPTS = 3
SLICE_BACKOFF_S: tuple[float, ...] = (2.0, 8.0)

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
    """The rate an estimate uses: for a model our gateway serves, the deployment's (`costs.platform_rate`: what
    the gateway will charge, the plan's markup included — F334); else the operator's (`model_pricing`, by the id or
    its bare name), else the profile's, else None (unpriced)."""
    from core.providers.registry import provider_for

    if cost_source(provider_for(model).id) == "platform":
        from core.knowledge.costs import platform_rate

        rate = await platform_rate(model)
        if rate is not None:
            return rate.input_per_mtok
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
        # A document's slices, while the indexing pipeline asks (PG1): a rate limit is waited out and the slice
        # sent again, halved each time so a provider that answers part of a slice before its limit (our gateway
        # settles what it embedded; Titan answers text by text) has less to answer again; each answered slice
        # is counted on the document's progress. A search's question is never kept waiting.
        waiter = pacing.Waiter(model) if kind == "document" and pacing.is_paced() else None
        tracker = progress.current() if waiter is not None else None
        i, size = 0, step
        tries = 1
        while i < len(prepared):
            batch = prepared[i:i + size]
            try:
                answer = await llm.embed_batch_ex(batch, model, call_settings, input_type=input_type, dims=dims)
            except llm.EmbeddingFailed as exc:
                if waiter is not None and pacing.rate_limited(exc):
                    # What this slice was charged before its limit is recorded now, alone: what the slices
                    # before it cost is in the call's answer, or recorded once if the call fails in the end.
                    await _record_failed_charge(exc, model=model, provider=provider.id, attribution=attribution,
                                                profile=profile)
                    try:
                        waited = await waiter.wait()
                    except pacing.Parked as parked:
                        # The slot is given back (KL1): the slices answered were paid for, so their cost is
                        # recorded and their vectors kept by indexing (F345), as on any other stop.
                        if vectors:
                            await asyncio.shield(_record_failed_charge(parked, model=model, provider=provider.id,
                                                                       attribution=attribution, tokens=tokens,
                                                                       charged=charged, profile=profile))
                        parked.answered_vectors = list(vectors)  # type: ignore[attr-defined]
                        raise
                    if waited:
                        size = max(1, len(batch) // 2)
                        continue
                    exc.charged_microusd = None  # recorded above
                elif waiter is not None and exc.retryable and tries < SLICE_ATTEMPTS:
                    # An outage, while indexing asks: the slice is sent again after a short wait (F418). What
                    # it was charged is recorded alone, as a rate limit's is.
                    await _record_failed_charge(exc, model=model, provider=provider.id, attribution=attribution,
                                                profile=profile)
                    _log.info(f"knowledge: {model} failed a slice ({exc}); trying it again ({tries + 1} of "
                              f"{SLICE_ATTEMPTS})")
                    await asyncio.sleep(SLICE_BACKOFF_S[min(tries - 1, len(SLICE_BACKOFF_S) - 1)])
                    tries += 1
                    continue
                elif tries > 1:
                    exc.charged_microusd = None  # each try's charge recorded as it failed
                await _record_failed_charge(exc, model=model, provider=provider.id, attribution=attribution,
                                            tokens=tokens, charged=charged, profile=profile)
                error = _knowledge_error(exc, model)
                if tries > 1 and isinstance(error, EmbeddingError):
                    error = EmbeddingError(f"{error} (after {tries} tries)", retryable=error.retryable)
                # The texts answered before the failure, in order: indexing keeps them across a pause (F345).
                error.answered_vectors = list(vectors)  # type: ignore[attr-defined]
                raise error from exc
            except BaseException as exc:
                # Cancelled (a job's time limit) or broken mid-call: the slices answered were paid for all the same
                # — their cost recorded, shielded from the cancel (F329), their vectors kept by indexing (F345).
                if vectors:
                    await asyncio.shield(_record_failed_charge(exc, model=model, provider=provider.id,
                                                               attribution=attribution, tokens=tokens,
                                                               charged=charged, profile=profile))
                exc.answered_vectors = list(vectors)  # type: ignore[attr-defined]
                raise
            vectors.extend(answer.vectors)
            tries = 1
            tokens += answer.input_tokens or profiles.estimate_tokens(batch)
            if answer.charged_microusd is not None:
                charged = (charged or 0) + answer.charged_microusd
            request_id = request_id or answer.request_id
            i += len(batch)
            if waiter is not None:
                waiter.answered()
                size = step
            if tracker is not None:
                tracker.advance(by=len(batch))

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
