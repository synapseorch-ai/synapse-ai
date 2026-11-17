"""Each model's input window, in tokens: the one source the model pick (F1251) and a run's context budget (D339) read.

A host states its own models' windows over this (cloud: its platform rows, through `models_menu.set_model_facts`'s
`window` for the pick, and `set_host_windows` for a step's budget, F1591). None is "not known": the pick never rules a
model out for it, and D339 budgets a default.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

_log = logging.getLogger(__name__)

#: Known input windows: `(id prefix, tokens)`, matched against a model's id and each id its provider's prefix leaves
#: (`us.anthropic.claude-haiku-4-5` is also `claude-haiku-4-5`); the longest prefix that matches wins. The rows are
#: D339's (TRI1), each from its provider's docs.
WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic (the Claude API's models reference, 2026-10): the 5 family and Opus 4.6–4.8 / Sonnet 4.6 hold 1M by
    # default; Haiku 4.5, Sonnet 4.5 and Opus 4.5 hold 200K (Sonnet 4.5's 1M needs a beta header the engine never
    # sends). Any other Claude id is taken at the smaller window: a step that compacts early loses nothing.
    ("claude-", 200_000),
    ("claude-fable-5", 1_000_000),
    ("claude-mythos-5", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-haiku-5", 1_000_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-sonnet-4-5", 200_000),
    ("claude-opus-4-5", 200_000),
    # OpenAI (developers.openai.com model pages, 10-2026): gpt-5's input is 272K of its 400K; gpt-oss shares its
    # 131,072 between input and output, which the budget's output reserve covers.
    ("gpt-5", 272_000),
    # GPT-6 and 6.1 hold about 1M, but Bedrock bills an input over 272,000 tokens at twice the input side and 1.5x
    # output for the whole request (the GPT-6 cards, 11-2026): a step's budget keeps under the dearer tier (F1593).
    ("gpt-6", 272_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("o3", 200_000),
    ("o4-mini", 200_000),
    ("gpt-oss-", 131_072),
    # Google (ai.google.dev model pages and model cards): Gemini 2.x and 3.x hold 1,048,576; Gemma 3's card says 32K
    # (1B) and 128K, and Gemma 4's config holds 262,144.
    ("gemini-2.", 1_048_576),
    ("gemini-3", 1_048_576),
    ("gemma-3-1b", 32_000),
    ("gemma-3", 128_000),
    ("gemma-4", 262_144),
    # Amazon (the Nova user guide and Bedrock model cards).
    ("nova-pro", 300_000),
    ("nova-lite", 300_000),
    ("nova-micro", 128_000),
    ("nova-premier", 1_000_000),
    ("nova-2-lite", 1_000_000),
    # Meta (its model cards; Bedrock's ids spell 3.1 and 3.3 `llama3-1-` / `llama3-3-`). Llama 3 itself is 8K.
    ("llama-3.1", 128_000),
    ("llama-3.3", 128_000),
    ("llama3-1-", 128_000),
    ("llama3-3-", 128_000),
    ("llama3-70b", 8_000),
    ("llama3-8b", 8_000),
    # Mistral (docs.mistral.ai): the -latest aliases point at 256K models; Large 4 is 1M; any other id is taken at
    # the smaller window an older one had.
    ("mistral-large", 128_000),
    ("mistral-large-latest", 256_000),
    ("mistral-medium-latest", 256_000),
    ("mistral-small-latest", 256_000),
    ("mistral-large-4", 1_000_000),
    # Bedrock's 2024 Mistral models (their Bedrock model cards; F1586: Large 24.02 refused a 40k-token prompt with
    # "maximum context length is 32768 tokens"): older, smaller models than the names above on Mistral's own API.
    ("mistral-large-2402", 32_768),
    ("mistral-small-2402", 32_768),
    ("mistral-7b-instruct", 32_768),
    ("mixtral-8x7b", 32_768),
    # DeepSeek (api-docs.deepseek.com): deepseek-chat and -reasoner were retired 07-2026; V4 holds 1M.
    ("deepseek-v4", 1_000_000),
    ("deepseek-flash", 1_000_000),
    # Qwen (Alibaba Model Studio): 1M in all, of which input is 983,616 with thinking on (991,808 without).
    ("qwen3.7", 983_616),
    ("qwen3.8", 983_616),
)


#: A host's own window for a family it serves (F1649): `(host, family prefix, tokens, the page it was read from)`. A
#: host serves a model at its own window, often less than its maker's: Groq serves Qwen 3.8 27B at 131,072 tokens,
#: where Alibaba's own API reads 983,616. Read for that host's ids (`<host>.<model>`), and winning whenever it is less
#: than the family's row; a host that serves more leaves the family's, so a budget never plans past what fits.
HOST_WINDOWS: tuple[tuple[str, str, int, str], ...] = (
    ("groq", "qwen3.8", 131_072, "https://console.groq.com/docs/model/qwen/qwen3.8-27b"),
)


def window_tokens(model_id: str) -> int | None:
    """`model_id`'s input window in tokens, or None when it is not known: its family's row (the longest prefix of its
    id or an id its provider's prefix leaves), and its host's own row where that host serves less (`HOST_WINDOWS`)."""
    model_id = (model_id or "").strip().lower()
    if not model_id:
        return None
    ids = [model_id, *(model_id[n + 1:] for n, c in enumerate(model_id) if c in "./")]
    found = [(len(prefix), tokens) for prefix, tokens in WINDOWS for i in ids if i.startswith(prefix.lower())]
    family = max(found)[1] if found else None
    host, dot, _ = model_id.partition(".")
    served = [(len(prefix), tokens) for name, prefix, tokens, _ in HOST_WINDOWS if dot and host == name
              for i in ids[1:] if i.startswith(prefix.lower())]
    if not served:
        return family
    own = max(served)[1]
    return own if family is None else min(own, family)


# ── a host's word (F1591) ───────────────────────────────────────────────────────────────────────────────────────────

#: `fn(ids) -> {id: tokens}`: a host's windows for the ids it serves, by what serves them. A host's id can hide the
#: model: cloud's `synapse.mistral-large` is Bedrock's 32K 2402 model, which this table reads as `mistral-large`, Mistral's
#: own API (128K). An id it says nothing of is read from the table.
HostWindows = Callable[[list[str]], Awaitable[dict[str, int]]]
#: How long a host's word on a model is kept, in seconds: a window changes only when the host's catalogue does, and a
#: step asks for its model's every time (cloud: one read of its catalogue per model per process in that time).
HOST_TTL = 300.0
_host: HostWindows | None = None
#: `{id: (when asked, tokens or None)}`: what the host said, nothing said included; a failed ask is never kept.
_said: dict[str, tuple[float, int | None]] = {}
_clock = time.monotonic


def set_host_windows(fn: HostWindows | None) -> None:
    """Set (or clear) the host's word; what an earlier host said is forgotten."""
    global _host
    _host = fn
    _said.clear()


async def window_of(model_id: str) -> int | None:
    """`model_id`'s input window: the host's word first (kept `HOST_TTL` seconds), else `window_tokens`. A host that
    cannot answer now is logged, and the table answers this once."""
    model_id = (model_id or "").strip()
    if _host is None or not model_id:
        return window_tokens(model_id)
    now = _clock()
    kept = _said.get(model_id)
    if kept is None or now - kept[0] >= HOST_TTL:
        try:
            tokens = (await _host([model_id]) or {}).get(model_id)
        except Exception:  # noqa: BLE001 — the table answers; the next step asks again
            _log.warning("a model's window was not read from the host", exc_info=True)
            return window_tokens(model_id)
        kept = (now, int(tokens) if tokens else None)
        _said[model_id] = kept
    return kept[1] or window_tokens(model_id)
