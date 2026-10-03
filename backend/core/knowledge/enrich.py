"""Contextual Retrieval: a model writes each passage's context before it is indexed (CR1, D180; F328).

A knowledge base that opts in (`Enrichment.mode = "contextual"`) has every chunk searched with a sentence or two a
chat model wrote about where the passage sits in its document — Anthropic's Contextual Retrieval — between the free
title/date/section prefix and the passage (`pipeline.compose_context`). The keyword leg and the vector leg both
read it (`Chunk.search_text()`); a citation still quotes only the passage. Opting in changes the index signature,
so it is a blue/green rebuild behind the cost preview.

* **The model** is the base's `contextual_model`, else the deployment's `knowledge_contextual_model` (cloud: its
  platform default), else the workspace's chat model (`resolve_model`). A snapshot records the model it was built
  with when it is made (`snapshots._snapshot`), and every document indexed into it uses that one (`built_with`), so
  a snapshot never holds two models' contexts — as `embedders.snapshot_spec` keeps one embedding model.
* **What the model sees:** the document (or the attachment the passage comes from) whole, up to
  `WHOLE_DOC_TOKENS`. A longer one is cut into windows of `WINDOW_TOKENS` at fixed places (moved forward to a line
  break), and the model sees the document's opening (`HEAD_TOKENS`) and the window holding the passage. Every
  passage in a window is asked with the same system prompt — the document — so a provider caches it: the engine
  marks it on Anthropic and Bedrock, OpenAI and DeepSeek cache a repeated prefix themselves. The first call of a
  window runs alone so the cache is written before the others read it; the rest run `CONCURRENCY` at a time.
  Reasoning is off: a context is a sentence, not a problem.
* **Money:** before each window, the base's budget is checked with the window's estimate (`check_budget`); each
  window's calls are recorded as one `knowledge_costs` row (stage `enrich`) as soon as they finish, so the next
  window's check sees them. Their `usage_logs` rows are the chat path's own (`ledger.usage_logged_by`). A platform
  call's row carries what our gateway charged (`LLMResult.charged_microusd`). Prices come from
  `costs.chat_usd` (`_price`: a platform model at the deployment's rate, F334); estimates count a cache read only
  where a cache point holds (`estimate_usd`).
* **Failures, by cause** (Naveen's call 1 in D180):
  - an answer that is empty, blocked, or cut off at the token limit is asked once more; a second such answer leaves
    that passage with the prefix alone, counted and warned (`enrich_fallback`), and never cached, so the next build
    asks again;
  - a provider still failing after its own retries fails the document: `EnrichmentError` (`enrich_failed`),
    retryable for an outage or a rate limit, not for a refused key or an exhausted quota;
  - an empty wallet (a 402 from our gateway) is `WalletExhausted` and a spent budget `BudgetExceeded`: both pause.

  What was spent before a failure is recorded, and the contexts written so far are kept (`enrich_cache`).
* **A provider's rate limit is paced, not failed** (F356): once one outlasts the provider's own retries, the document's
  calls go one at a time and each limit is waited out (`_Pacer`); the document fails only after ten minutes with
  nothing answered. Any provider: a `rate_limited` failure, or one that says it was throttled.
* **A long document keeps its lease:** the job or the rebuild step that indexes it sets a heartbeat (`beating`),
  called while the calls run, so reconcile never takes a long enrichment for a stuck one.

`extract_metadata` is not built (listed for after release); the stage's report says so when it is asked for.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from core.knowledge.chunker import ATTACHMENT_KEY, estimate_tokens
from core.knowledge.errors import EnrichmentError, KnowledgeError, WalletExhausted
from core.knowledge.parsed import ParseWarning

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import StageEstimate
    from core.knowledge.chunks import Chunk
    from core.knowledge.kb_settings import Enrichment, KnowledgeBase
    from core.knowledge.parsed import ParsedDocument
    from core.knowledge.pipeline_contract import IndexContext
    from core.knowledge.types import Attribution, Usage

#: A snapshot built with Contextual Retrieval records this as its `enrichment_version`.
CONTEXTUAL_VERSION = "contextual-1"
#: Bump when the prompt changes: the context cache keys on it.
PROMPT_VERSION = "1"

#: A document up to this many tokens is shown whole; a longer one as its opening and the window of the passage.
WHOLE_DOC_TOKENS = 16_000
HEAD_TOKENS = 1_500
WINDOW_TOKENS = 12_000
#: A window's edge moves forward to the next line break within this many characters.
SNAP_CHARS = 2_000
#: The words around the document and around the passage, in tokens (for estimates).
DOCUMENT_WRAPPER_TOKENS = 40
INSTRUCTION_TOKENS = 80
#: What a context is expected to cost to write (estimates), and the most one may be.
CONTEXT_OUTPUT_TOKENS = 75
MAX_OUTPUT_TOKENS = 150
MAX_CONTEXT_CHARS = 600
#: Calls at once for one document, after a window's first.
CONCURRENCY = max(1, int(os.environ.get("SYNAPSE_KNOWLEDGE_CONTEXT_CONCURRENCY", "4") or 4))
#: A provider's rate limit that outlasts its own retries (F356; Naveen: pace, don't fail): the document's calls go one at
#: a time, each limit waits `RATE_WAIT_S` (doubling to `RATE_WAIT_MAX_S`) and the passage is asked again; the document
#: fails only after `NO_PROGRESS_S` with nothing answered.
RATE_WAIT_S = 15.0
RATE_WAIT_MAX_S = 60.0
NO_PROGRESS_S = 600.0
#: A rate limit said in words, for a provider whose failure carries no status (Bedrock's `ThrottlingException`).
_THROTTLED = re.compile(r"throttl|too ?many ?requests|rate.?limit|servicequotaexceeded", re.IGNORECASE)

#: Where a cache point holds. Explicit: the engine marks the system prompt (`core.cache.prompt_cache`), honoured
#: above the model's minimum — Haiku 4.5's 4,096 tokens is the highest. Automatic: the provider caches a repeated
#: prefix of 1,024 tokens or more by itself. Anywhere else an estimate prices every call in full: an upper bound.
EXPLICIT_CACHE_MIN_TOKENS = 4_096
AUTOMATIC_CACHE_MIN_TOKENS = 1_024
_AUTOMATIC_CACHE = frozenset({"openai", "deepseek"})
_BEDROCK_CACHED = re.compile(r"anthropic|claude|nova", re.IGNORECASE)

AGENT_ID = "knowledge_enrich"
_TOOL_NAME = "knowledge:enrich"
WINDOWS = f"{WHOLE_DOC_TOKENS}/{HEAD_TOKENS}/{WINDOW_TOKENS}/{SNAP_CHARS}"

_SYSTEM = "Here is {what}{title}:\n<document>\n{text}\n</document>"
_WHOLE = "a document"
_EXCERPT = ("part of a long document: its opening, then the stretch of it the passage comes from "
            "(\"[…]\" marks what is left out)")
_ASK = ("Here is the passage we want to situate within the whole document:\n<passage>\n{passage}\n</passage>\n"
        "Please give a short succinct context to situate this passage within the overall document for the purposes "
        "of improving search retrieval of the passage. Answer only with the succinct context and nothing else.")
_GAP = "\n\n[…]\n\n"


# ── the model ──────────────────────────────────────────────────────────────────

def resolve_model(enrichment: Enrichment, settings: Mapping[str, Any] | None = None) -> str:
    """The model that writes contexts: the base's own, else the deployment's default, else the workspace's chat model."""
    from core.knowledge.config import knowledge_settings

    if enrichment.contextual_model:
        return enrichment.contextual_model.strip()
    if settings is None:
        from core.config import load_settings
        settings = load_settings()
    return (knowledge_settings(settings).contextual_model or str(settings.get("model") or "")).strip()


async def built_with(kb: KnowledgeBase, snapshot_id: str, settings: Mapping[str, Any]) -> tuple[str, str]:
    """(mode, model) the snapshot was made with: its recorded `enrichment_mode` and model. A snapshot made before
    Contextual Retrieval was built recorded "prefix" whatever its settings said, and stays so; an unknown snapshot
    follows the base."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeSnapshotDB as S
    from core.tenancy import get_tenant

    async with session() as s:
        row = (await s.execute(select(S.enrichment_mode, S.stats).where(
            S.tenant_id == get_tenant(), S.id == snapshot_id))).first()
    if row is None:
        mode = kb.enrichment.mode
        return mode, resolve_model(kb.enrichment, settings) if mode == "contextual" else ""
    mode = str(row[0] or "prefix")
    if mode != "contextual":
        return mode, ""
    stats = row[1] if isinstance(row[1], dict) else {}
    recorded = (stats.get("enrichment") or {}).get("model") if isinstance(stats.get("enrichment"), dict) else None
    return mode, str(recorded or resolve_model(kb.enrichment, settings))


def snapshot_record(kb: KnowledgeBase, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What a new snapshot of `kb` records about its enrichment (`snapshots._snapshot`): its version and, for
    Contextual Retrieval, the model it is built with."""
    if kb.enrichment.mode != "contextual":
        return {}
    return {"enrichment": {"model": resolve_model(kb.enrichment, settings), "version": CONTEXTUAL_VERSION,
                           "prompt": PROMPT_VERSION}}


def check_settings(enrichment: Enrichment | None, settings: Mapping[str, Any] | None = None) -> None:
    """Raise `ValueError` (a 422) for Contextual Retrieval with no model that can write contexts — before anything
    is saved or indexed. A key not saved yet is not checked here: it may be saved later, and indexing says so."""
    from core.knowledge import profiles
    from core.providers.registry import provider_for

    if enrichment is None or enrichment.mode != "contextual":
        return
    model = resolve_model(enrichment, settings)
    if not model:
        raise ValueError("Contextual Retrieval needs a chat model to write the contexts: choose one for this "
                         "knowledge base, or set the workspace's Contextual Retrieval model.")
    spec = provider_for(model)
    if spec.mode not in ("cloud", "bedrock", "local"):
        raise ValueError(f"{model!r} cannot write contexts: a {spec.label} model runs a session per call. "
                         "Choose an API model.")
    if profiles.profile_for(model) is not None:
        raise ValueError(f"{model!r} is an embedding model; Contextual Retrieval needs a chat model.")


# ── windows ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Window:
    """What the model sees as the document for the passages it holds."""

    id: str
    text: str
    tokens: int
    excerpt: bool


def _texts(doc: ParsedDocument) -> dict[str | None, str]:
    """Each text a chunk can come from: the document's (key None) and each attachment's, by the chunker's name."""
    found: dict[str | None, str] = {None: doc.text}

    def walk(d: ParsedDocument, name: str | None) -> None:
        for attachment in d.attachments:
            if attachment.parsed is None:
                continue
            inner = f"{name}/{attachment.name}" if name else attachment.name
            found.setdefault(inner, attachment.parsed.text)
            walk(attachment.parsed, inner)

    walk(doc, None)
    return found


def spans(text: str) -> list[tuple[int, int]]:
    """The windows a text is cut into, as (start, end) characters: one for a text up to `WHOLE_DOC_TOKENS`."""
    tokens = estimate_tokens(text)
    if tokens <= WHOLE_DOC_TOKENS:
        return [(0, len(text))]
    per_token = len(text) / tokens
    size = max(1, int(WINDOW_TOKENS * per_token))
    found: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            brk = text.find("\n", end, end + SNAP_CHARS)
            if brk != -1:
                end = brk + 1
        found.append((start, end))
        start = end
    return found


def _window(text: str, source: str | None, span: tuple[int, int], *, whole: bool) -> Window:
    start, end = span
    if whole:
        shown = text
    else:
        head = text[:max(1, int(HEAD_TOKENS * len(text) / max(1, estimate_tokens(text))))]
        shown = text[:end] if start <= len(head) else head + _GAP + text[start:end]
    return Window(id=f"{source or ''}#{start}-{end}", text=shown, tokens=estimate_tokens(shown), excerpt=not whole)


def plan(doc: ParsedDocument, chunks: Sequence[Chunk]) -> list[tuple[Window, list[Chunk]]]:
    """The windows `chunks` are asked in, each with its passages, in the order of their first passage."""
    texts = _texts(doc)
    cut: dict[str | None, list[tuple[int, int]]] = {}
    grouped: dict[str, tuple[Window, list[Chunk]]] = {}
    for chunk in chunks:
        source = chunk.metadata.get(ATTACHMENT_KEY)
        if source not in texts:
            source = None
        text = texts[source]
        if source not in cut:
            cut[source] = spans(text)
        windows = cut[source]
        at = next((i for i, (a, b) in enumerate(windows) if a <= chunk.char_start < b), len(windows) - 1)
        window = _window(text, source, windows[at], whole=len(windows) == 1)
        grouped.setdefault(window.id, (window, []))[1].append(chunk)
    return list(grouped.values())


def system_prompt(window: Window, title: str | None) -> str:
    return _SYSTEM.format(what=_EXCERPT if window.excerpt else _WHOLE,
                          title=f" titled {title!r}" if title else "", text=window.text)


def user_prompt(chunk: Chunk) -> str:
    return _ASK.format(passage=chunk.content)


_LABEL = re.compile(r"^(?:context|succinct context|answer)\s*:\s*", re.IGNORECASE)


def clean(answer: str) -> str:
    """A context as it is searched: one line, no label, no wrapping quotes or markup."""
    text = re.sub(r"[*_`#]+", "", answer or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = _LABEL.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


# ── prices ─────────────────────────────────────────────────────────────────────

async def _price(model: str, input_tokens: int, output_tokens: int, *, cache_read: int = 0,
                 cache_write: int = 0) -> float | None:
    """USD for these tokens on `model`, or None when it is unpriced. `input_tokens` are the uncached ones.
    Through `costs.chat_usd` (F334): a platform model at what the deployment charges, any other by the rate card."""
    from core.knowledge.costs import chat_usd

    return await chat_usd(model, input_tokens, output_tokens, cache_read=cache_read, cache_write=cache_write)


def _caching(model: str) -> tuple[str | None, int]:
    """("explicit" | "automatic" | None, the prefix a cache needs) for `model`."""
    from core.providers.registry import provider_for

    spec = provider_for(model)
    if spec.id == "anthropic" or (spec.id == "bedrock" and _BEDROCK_CACHED.search(model)):
        return "explicit", EXPLICIT_CACHE_MIN_TOKENS
    if spec.id in _AUTOMATIC_CACHE:
        return "automatic", AUTOMATIC_CACHE_MIN_TOKENS
    return None, 0


def window_tokens(model: str, document_tokens: int, passages: Sequence[int], *,
                  output_tokens: int = CONTEXT_OUTPUT_TOKENS) -> tuple[int, int, int, int]:
    """(uncached input, output, cache reads, cache writes) for asking `passages` against one window of
    `document_tokens`: a cache write and reads where a cache point holds, every call in full elsewhere."""
    n = len(passages)
    if not n:
        return 0, 0, 0, 0
    shown = document_tokens + DOCUMENT_WRAPPER_TOKENS
    misses = sum(p + INSTRUCTION_TOKENS for p in passages)
    kind, floor = _caching(model)
    if kind == "explicit" and shown >= floor:
        return misses, n * output_tokens, shown * (n - 1), shown
    if kind == "automatic" and shown >= floor:
        return misses + shown, n * output_tokens, shown * (n - 1), 0
    return misses + shown * n, n * output_tokens, 0, 0


async def estimate_usd(model: str, docs: Sequence[tuple[int, int]]) -> float | None:
    """What writing contexts for `docs` — (chunks, the document's tokens) each — costs on `model`, or None when it
    is unpriced. Windows as `plan` cuts them; each passage taken as the document's average."""
    total_in = total_out = total_read = total_write = 0
    for n, doc_tokens in docs:
        if n <= 0:
            continue
        each = max(1, round(doc_tokens / n))
        if doc_tokens <= WHOLE_DOC_TOKENS:
            groups = [(doc_tokens, n)]
        else:
            k = max(1, math.ceil(doc_tokens / WINDOW_TOKENS))
            groups = [(HEAD_TOKENS + WINDOW_TOKENS, n // k + (1 if i < n % k else 0)) for i in range(k)]
        for shown, count in groups:
            i, o, r, w = window_tokens(model, shown, [each] * count)
            total_in, total_out, total_read, total_write = total_in + i, total_out + o, total_read + r, total_write + w
    return await _price(model, total_in, total_out, cache_read=total_read, cache_write=total_write)


async def preview_stage(enrichment: Enrichment, per_doc: Sequence[tuple[int, int, int]],
                        settings: Mapping[str, Any]) -> StageEstimate | None:
    """The cost preview's `enrich` line for a draft asking for Contextual Retrieval: one call per chunk, priced as
    `estimate_usd` prices it. `per_doc` is the preview's (chunks, content tokens, prefix tokens) per document."""
    from core.knowledge.api_models import StageEstimate
    from core.knowledge.embedders import cost_source
    from core.providers.registry import provider_for

    if enrichment.mode != "contextual" or not per_doc:
        return None
    model = resolve_model(enrichment, settings)
    if not model:
        return None
    usd = await estimate_usd(model, [(n, content) for n, content, _ in per_doc])
    return StageEstimate(stage="enrich", model=model, units=float(sum(n for n, _, _ in per_doc)), unit="requests",
                         est_usd=round(usd or 0.0, 8), cost_source=cost_source(provider_for(model).id))


# ── the heartbeat ──────────────────────────────────────────────────────────────

_HEARTBEAT: contextvars.ContextVar[Callable[[], Awaitable[Any]] | None] = contextvars.ContextVar(
    "knowledge_enrich_heartbeat", default=None)


@contextlib.contextmanager
def beating(heartbeat: Callable[[], Awaitable[Any]] | None) -> Iterator[None]:
    """While the block runs, a document's contexts being written keep `heartbeat` beating (the job's lease on the
    document, or the rebuild step's on its snapshot)."""
    token = _HEARTBEAT.set(heartbeat)
    try:
        yield
    finally:
        _HEARTBEAT.reset(token)


# ── writing the contexts ───────────────────────────────────────────────────────

@dataclass
class Enriched:
    """What `contextualize` gives the pipeline: each chunk's context sentence, what it cost, what to report."""

    sentences: dict[str, str] = field(default_factory=dict)
    usage: list[Usage] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"


@dataclass
class _Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    charged: int = 0
    charged_calls: int = 0
    latency_ms: int = 0

    def add(self, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
            charged: int | None, latency_ms: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read += cache_read
        self.cache_write += cache_write
        if charged is not None:
            self.charged += charged
            self.charged_calls += 1
        self.latency_ms += latency_ms


def _hard(exc: Exception, model: str, *, platform: bool) -> KnowledgeError:
    """A provider failure as the Knowledge error it leaves the document in."""
    from core.llm_providers import MODEL_ERROR, PROVIDER_UNAVAILABLE, RATE_LIMITED

    status, code = getattr(exc, "status", None), getattr(exc, "code", None)
    if status == 402 and platform:
        return WalletExhausted(f"{model}: {exc}")
    retryable = code in (RATE_LIMITED, PROVIDER_UNAVAILABLE, MODEL_ERROR)
    return EnrichmentError(f"{model} could not write the passages' context: {exc}", retryable=retryable)


def _ready(model: str, settings: Mapping[str, Any]) -> None:
    """`EnrichmentError` (not retryable) when the model's provider needs a key or an address not saved."""
    from core.providers.registry import provider_for

    spec = provider_for(model)
    if spec.readiness == "key" and spec.keys and not settings.get(spec.keys[0]):
        raise EnrichmentError(f"{model}: the {spec.label} key Contextual Retrieval needs is not saved.",
                              retryable=False)
    if spec.readiness == "address" and spec.address and not settings.get(spec.address):
        raise EnrichmentError(f"{model}: the {spec.label} address Contextual Retrieval needs is not saved.",
                              retryable=False)


async def _ask(model: str, system: str, prompt: str, settings: Mapping[str, Any], attribution: Attribution) -> Any:
    from core.llm_providers import detect_mode_from_model, generate_response_ex

    return await generate_response_ex(
        prompt_msg=prompt, sys_prompt=system, mode=detect_mode_from_model(model), current_model=model,
        current_settings=dict(settings), session_id=attribution.session_id, agent_id=AGENT_ID,
        source=attribution.source or "knowledge", run_id=attribution.run_id, tool_name=_TOOL_NAME,
        reasoning_effort="off", max_output_tokens=MAX_OUTPUT_TOKENS)


def _rate_limited(exc: BaseException) -> bool:
    """A provider saying "too many, slow down" — never an exhausted credit or a refused key."""
    from core.llm_providers import MODEL_ERROR, PROVIDER_UNAVAILABLE, RATE_LIMITED

    code = getattr(exc, "code", None)
    if code == RATE_LIMITED:
        return True
    return code in (MODEL_ERROR, PROVIDER_UNAVAILABLE) and bool(_THROTTLED.search(str(exc)))


class _Pacer:
    """One document's calls: `CONCURRENCY` at a time until a rate limit outlasts the provider's own retries, then one
    at a time, each limit waited out, until nothing has been answered for `NO_PROGRESS_S` (F356)."""

    def __init__(self) -> None:
        self.gate = asyncio.Semaphore(CONCURRENCY)
        self.serial = asyncio.Lock()
        self.limited = False
        self.waits = 0
        self.waited_s = 0.0
        self._wait = RATE_WAIT_S
        self._answered = time.monotonic()

    async def call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """`fn()`, one at a time once the document has been rate limited."""
        if self.limited:
            async with self.serial:
                return await fn()
        return await fn()

    def answered(self) -> None:
        self._answered = time.monotonic()
        self._wait = RATE_WAIT_S

    async def rate_limited(self) -> bool:
        """Wait out a rate limit; False (give up) once nothing has been answered for `NO_PROGRESS_S`."""
        if time.monotonic() - self._answered >= NO_PROGRESS_S:
            return False
        self.limited = True
        wait, self._wait = self._wait, min(self._wait * 2, RATE_WAIT_MAX_S)
        self.waits += 1
        self.waited_s += wait
        await asyncio.sleep(wait)
        return True


async def _context(model: str, system: str, chunk: Chunk, settings: Mapping[str, Any], attribution: Attribution,
                   tally: _Tally, pacer: _Pacer, *, platform: bool) -> str | None:
    """One passage's context, or None when two answers were unusable. A rate limit is waited out and the same
    question asked again (it spends neither try). Raises the document's error."""
    from core.llm_providers import EmptyAnswer, LLMError

    prompt = user_prompt(chunk)
    tries = 0
    while tries < 2:
        started = time.monotonic()
        try:
            result = await pacer.call(lambda: _ask(model, system, prompt, settings, attribution))
        except EmptyAnswer as empty:  # billed, and nothing usable: empty, blocked, all reasoning
            pacer.answered()
            spent = tuple(getattr(empty, "usage", None) or (0, 0, 0, 0))
            tally.add(int(spent[0]), int(spent[1]), int(spent[2]), int(spent[3]), None,
                      int((time.monotonic() - started) * 1000))
            tries += 1
            continue
        except LLMError as exc:
            if _rate_limited(exc) and await pacer.rate_limited():
                continue
            raise _hard(exc, model, platform=platform) from exc
        pacer.answered()
        tally.add(result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens,
                  getattr(result, "charged_microusd", None), int((time.monotonic() - started) * 1000))
        tries += 1
        if result.stop_reason == "max_tokens":
            continue
        text = clean(result.text)
        if text:
            return text
    return None


async def _record(tally: _Tally, *, model: str, attribution: Attribution, window: Window,
                  passages: int) -> Usage | None:
    """The window's calls as one cost row, written now (`record_costs` never raises)."""
    from core.knowledge.embedders import cost_source
    from core.knowledge.ledger import CostRow, record_costs
    from core.knowledge.types import Usage
    from core.providers.registry import provider_for

    if not tally.calls:
        return None
    spec = provider_for(model)
    usd = await _price(model, tally.input_tokens, tally.output_tokens, cache_read=tally.cache_read,
                       cache_write=tally.cache_write)
    usage = Usage(stage="enrich", cost_source=cost_source(spec.id), model=model, provider=spec.id,
                  input_tokens=tally.input_tokens + tally.cache_read + tally.cache_write,
                  output_tokens=tally.output_tokens, units=float(tally.calls), unit="requests",
                  est_usd=round(usd or 0.0, 10),
                  # What our gateway settled; None when no call reported a charge (BYOK, local, OSS).
                  charged_microusd=tally.charged if tally.charged_calls else None,
                  priced=usd is not None, latency_ms=tally.latency_ms)
    await record_costs([CostRow(usage, attribution, details={
        "cache_read_tokens": tally.cache_read, "cache_write_tokens": tally.cache_write,
        "uncached_input_tokens": tally.input_tokens, "window": window.id, "window_tokens": window.tokens,
        "passages": passages, "calls_without_charge": tally.calls - tally.charged_calls})])
    return usage


async def _run_window(model: str, window: Window, todo: list[tuple[Chunk, str]], system: str,
                      settings: Mapping[str, Any], attribution: Attribution, tally: _Tally,
                      written: dict[str, str], pacer: _Pacer, *, platform: bool) -> int:
    """Ask every passage of `todo` against `window`: the first alone, the rest `CONCURRENCY` at a time. Fills
    `written` (cache key → context); answers how many fell back. On the first hard failure the rest are cancelled
    and it is raised."""
    fallback = 0

    async def one(chunk: Chunk, key: str) -> None:
        nonlocal fallback
        async with pacer.gate:
            text = await _context(model, system, chunk, settings, attribution, tally, pacer, platform=platform)
        if text is None:
            fallback += 1
        else:
            written[key] = text

    await one(*todo[0])  # alone: the provider's cache is written before the others read it
    tasks = [asyncio.ensure_future(one(c, k)) for c, k in todo[1:]]
    if not tasks:
        return fallback
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    failures = [e for e in (t.exception() for t in done if not t.cancelled()) if e is not None]  # each one read
    if failures:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise min(failures, key=_telling)
    return fallback


def _telling(exc: BaseException) -> int:
    """Which of several calls' failures the document reports: an empty wallet (it pauses), then a failure no retry
    mends, then one a retry may."""
    if isinstance(exc, WalletExhausted):
        return 0
    if isinstance(exc, KnowledgeError) and not exc.retryable:
        return 1
    return 2


async def contextualize(ctx: IndexContext, chunks: Sequence[Chunk], *, model: str, title: str | None) -> Enriched:
    """Write a context for each of `chunks` (see the module docstring). Raises `EnrichmentError`,
    `WalletExhausted` or `BudgetExceeded` — having recorded what was spent and kept what was written."""
    from core.knowledge import enrich_cache
    from core.knowledge.embedders import cost_source
    from core.knowledge.ledger import check_budget
    from core.knowledge.parse.service import _beating
    from core.providers.registry import provider_for

    out = Enriched(detail={"mode": "contextual", "version": CONTEXTUAL_VERSION, "model": model,
                           "passages": len(chunks)})
    if ctx.kb.enrichment.extract_metadata:
        out.detail["extract_metadata"] = "not built yet"
    if not chunks:
        return out
    if not model:
        raise EnrichmentError("Contextual Retrieval has no model to write the contexts: choose one in the "
                              "knowledge base's settings.", retryable=False)
    settings = dict(ctx.settings)
    _ready(model, settings)
    platform = cost_source(provider_for(model).id) == "platform"
    attribution = replace(ctx.attribution, snapshot_id=ctx.snapshot_id)
    sig = enrich_cache.signature(model, prompt_version=PROMPT_VERSION, windows=WINDOWS)
    cached = await enrich_cache.load(ctx.sha256, sig)
    written: dict[str, str] = dict(cached)
    windows = plan(ctx.parsed, chunks)
    hits = calls = fallback = 0
    keys: dict[str, str] = {}
    pacer = _Pacer()

    async with _beating(_HEARTBEAT.get()):
        for window, members in windows:
            todo: list[tuple[Chunk, str]] = []
            for chunk in members:
                key = enrich_cache.entry_key(window.id, chunk.content)
                keys[chunk.id] = key
                if key in cached:
                    hits += 1
                else:
                    todo.append((chunk, key))
            if not todo:
                continue
            i, o, r, w = window_tokens(model, window.tokens, [c.tokens for c, _ in todo])
            estimate = await _price(model, i, o, cache_read=r, cache_write=w)
            await check_budget(ctx.kb, estimate or 0.0)
            tally = _Tally()
            try:
                fallback += await _run_window(model, window, todo, system_prompt(window, title), settings,
                                              attribution, tally, written, pacer, platform=platform)
            finally:
                calls += tally.calls
                usage = await _record(tally, model=model, attribution=attribution, window=window,
                                      passages=len(todo))
                if usage is not None:
                    out.usage.append(usage)
                await enrich_cache.save(ctx.sha256, sig, written, model=model)

    out.sentences = {cid: written[key] for cid, key in keys.items() if key in written}
    out.detail.update(windows=len(windows), whole=all(not w.excerpt for w, _ in windows), calls=calls,
                      from_cache=hits, written=len(out.sentences) - hits, fallback=fallback)
    if pacer.waits:  # paced by the provider's rate limit (F356): how often, and how long it waited
        out.detail["rate_limited"] = {"waits": pacer.waits, "waited_s": round(pacer.waited_s, 1)}
    if fallback:
        out.status = "partial"
        out.warnings.append(ParseWarning(
            code="enrich_fallback", count=fallback,
            message=f"{fallback} of {len(chunks)} passages kept the title and section prefix: the model gave no "
                    "usable context for them. The next rebuild asks again."))
    return out
