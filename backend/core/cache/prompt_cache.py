"""
Provider-payload decorators that turn on prompt caching.

Caching pricing is asymmetric and provider-specific:
- Anthropic: cache writes cost ~1.25x base input; cache reads cost ~0.1x base input.
- OpenAI:    automatic for >=1024-token stable prefixes; reads are ~0.5x base input.
- DeepSeek:  server-side automatic; reads ~0.1x base input. Reported via
             `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.
- Gemini:    requires an explicit `cached_content` handle via client.caches.create().
             Reads ~0.25x base input. Minimum TTL: 5 min.
- Bedrock:   Anthropic-on-Bedrock supports `cachePoint` content blocks in Converse.

Every one of these is a PREFIX cache, checked in the order tools -> system ->
messages. So the layout the engine sends is: a stable system prompt, then the
append-only transcript as one text block per turn, then whatever changes every
turn (date, turn budget, task list) as the final block. `core.tools` builds the
first and last of those as two separate strings — they are never concatenated,
so nothing in a user's prompt can move the boundary.

Caching is gated by:
1. global settings.prompt_cache_enabled
2. prefix length. Anthropic's minimum cacheable prefix is 512 tokens on Opus 5,
   1024 on Opus 4.8 / Sonnet 5 / 4.6 / 4.5, 2048 on Opus 4.7 and 4096 on Opus
   4.6 / 4.5 and Haiku 4.5. Under the minimum a marker is silently ignored and
   nothing is charged, so the conservative 4000-char floor below costs nothing
   when wrong — it only avoids decorating payloads that cannot be cached.

When unsure or when a provider isn't supported, these helpers no-op so the
caller's payload is unchanged.
"""
from typing import Any

# 4000 chars ≈ 1000 tokens — around the floor for the models most runs use.
MIN_CACHEABLE_CHARS = 4000


def is_cacheable_system(system: str | None) -> bool:
    return bool(system) and len(system) >= MIN_CACHEABLE_CHARS


def is_cacheable_prefix(chars: int) -> bool:
    """Whether a prefix of `chars` characters is worth a cache marker."""
    return chars >= MIN_CACHEABLE_CHARS


# ── Anthropic ────────────────────────────────────────────────────────────────

def decorate_anthropic_kwargs(kwargs: dict, system: str | None) -> dict:
    """Mark the system prompt and the tools array as cache breakpoints.

    Anthropic allows four `cache_control` markers per request. This uses two —
    the whole system block and the last tool definition — and
    `build_anthropic_user_content` uses a third on the conversation. The system
    prompt is stable by construction (see `core.tools.build_system_prompt`), so
    it is one block; there is no longer a volatile suffix to carve off.
    """
    if not is_cacheable_system(system):
        return kwargs

    kwargs["system"] = [{
        "type": "text",
        "text": str(system),
        "cache_control": {"type": "ephemeral"},
    }]

    # Mark the last tool definition so the whole tools array is part of the prefix.
    tools = kwargs.get("tools")
    if isinstance(tools, list) and tools:
        last = dict(tools[-1])  # shallow copy — don't mutate caller's list
        last["cache_control"] = {"type": "ephemeral"}
        kwargs["tools"] = tools[:-1] + [last]

    return kwargs


def build_anthropic_user_content(
    prompt_blocks: list[str],
    volatile_tail: str | None,
    *,
    prefix_chars: int,
) -> list[dict]:
    """The last user message as content blocks: one per transcript turn, the
    cache marker on the last of them, then the volatile tail unmarked.

    `prefix_chars` is everything that precedes the marker — system, tools and
    the transcript itself — because the minimum is on the cumulative prefix,
    not on the system prompt. A short agent prompt in front of a long
    transcript is still worth caching.

    Next turn the transcript has one more block and the marker moves with it;
    Anthropic looks back ~20 block positions for the previous entry, which is
    why the split is one block per TURN and not per tool observation.
    """
    content: list[dict] = [{"type": "text", "text": b} for b in prompt_blocks if b]
    if content and is_cacheable_prefix(prefix_chars):
        content[-1]["cache_control"] = {"type": "ephemeral"}
    if volatile_tail:
        content.append({"type": "text", "text": volatile_tail})
    return content


# ── OpenAI-shaped marked parts (F1588): the endpoints that cache only what a request marks ───────────────────────

#: OpenRouter models that cache only when the request marks them (OpenRouter's prompt-caching page, read
#: 2026-11-14): Anthropic's, and the Alibaba models it lists for explicit caching. Its snapshot endpoints do not cache
#: this way, and every other model there caches by itself (or not at all), so they are sent as before.
MARKED_OPENROUTER_PREFIXES = ("anthropic/",)
MARKED_OPENROUTER_MODELS = frozenset({
    "qwen/qwen3-max", "qwen/qwen-plus", "qwen/qwen3.6-plus", "qwen/qwen3-coder-plus", "qwen/qwen3-coder-flash",
})
#: The setting that says an OpenAI-compatible server passes Anthropic-style markers on (LiteLLM in front of Claude).
#: Off by default: a server is not asked to take content parts it may refuse.
OAIC_MARKERS_SETTING = "openai_compatible_cache_markers"


def wants_markers(provider_id: str, bare_model: str, settings: dict | None) -> bool:
    """Whether an agent turn to this provider and model is sent with marked parts (F1588).

    Our model gateway: always (D191: it caches Claude only when the caller marks the prompt, and decides per model
    whether to cache at all). OpenRouter: its Claude and listed Qwen models. An OpenAI-compatible server: only with
    `openai_compatible_cache_markers`. Every other provider, a local v1 server included, caches by itself or not at
    all, and is sent plain strings as before."""
    if provider_id == "model_gateway":
        return True
    if provider_id == "openrouter":
        model = (bare_model or "").strip().lower()
        return model.startswith(MARKED_OPENROUTER_PREFIXES) or model in MARKED_OPENROUTER_MODELS
    if provider_id == "openai_compatible":
        return bool((settings or {}).get(OAIC_MARKERS_SETTING))
    return False


def openai_marked_messages(messages: list[dict], prompt_blocks: list[str] | None, volatile_tail: str | None,
                           *, prefix_chars: int) -> list[dict] | None:
    """`messages` (the system prompt first, this turn's message last) with the prompt marked for a prefix cache, as
    OpenAI-style content parts; None to send them as they are.

    The system prompt becomes one text part with `cache_control`; this turn's message a text part per transcript
    turn, the last one marked, then the per-turn tail unmarked, with the "\\n\\n" the engine joins it by at its head
    (react_engine.py: transcript, "\\n\\n", tail), so the marked part is byte-identical on the next turn. History in
    between is untouched. The joined text is exactly what was there: None unless this turn's message is the blocks and
    the tail, and unless the prefix (`prefix_chars`: system, tools and the transcript) reaches the floor the Anthropic
    path uses. No `ttl`: the 5-minute default (a 1-hour write costs 2x, and Nova refuses the field)."""
    if not prompt_blocks or not messages or messages[-1].get("role") != "user":
        return None
    blocks = [b for b in prompt_blocks if b]
    tail = f"\n\n{volatile_tail}" if volatile_tail else ""
    if not blocks or messages[-1].get("content") != "".join(blocks) + tail or not is_cacheable_prefix(prefix_chars):
        return None
    marked: list[dict] = []
    for message in messages[:-1]:
        content = message.get("content")
        if message.get("role") == "system" and isinstance(content, str) and content:
            message = {**message, "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}
        marked.append(message)
    parts: list[dict] = [{"type": "text", "text": b} for b in blocks]
    parts[-1]["cache_control"] = {"type": "ephemeral"}
    if tail:
        parts.append({"type": "text", "text": tail})
    marked.append({**messages[-1], "content": parts})
    return marked


def extract_anthropic_cache_tokens(response) -> tuple[int, int]:
    """Return (cache_read_tokens, cache_write_tokens) from an Anthropic SDK response."""
    usage = getattr(response, "usage", None)
    if not usage:
        return 0, 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return int(read), int(write)


# ── OpenAI / Grok / v1-compatible ────────────────────────────────────────────

def extract_openai_cache_tokens(usage: dict) -> tuple[int, int]:
    """Return (cache_read_tokens, cache_write_tokens) from an OpenAI-style usage dict.

    OpenAI's auto-caching only reports reads (`prompt_tokens_details.cached_tokens`).
    There is no separate write cost — the first call just pays the normal input rate.
    """
    if not isinstance(usage, dict):
        return 0, 0
    details = usage.get("prompt_tokens_details") or {}
    read = int(details.get("cached_tokens") or 0)
    return read, 0


# ── DeepSeek ─────────────────────────────────────────────────────────────────

def extract_deepseek_cache_tokens(usage: dict) -> tuple[int, int]:
    """DeepSeek surfaces hit/miss separately."""
    if not isinstance(usage, dict):
        return 0, 0
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    # DeepSeek has no explicit write tier — misses are billed at the normal rate.
    return hit, 0


# ── Gemini ───────────────────────────────────────────────────────────────────

def extract_gemini_cache_tokens(response) -> tuple[int, int]:
    """Gemini reports cached tokens in usage_metadata.cached_content_token_count."""
    um = getattr(response, "usage_metadata", None)
    if not um:
        return 0, 0
    read = int(getattr(um, "cached_content_token_count", 0) or 0)
    return read, 0


# ── Bedrock ──────────────────────────────────────────────────────────────────

def decorate_bedrock_system_blocks(system_blocks: list[dict], system: str | None) -> list[dict]:
    """Append a cachePoint marker after the system text block.

    Bedrock's Converse API uses `{"cachePoint": {"type": "default"}}` instead
    of inline cache_control. Only supported on a subset of models (Anthropic
    Claude on Bedrock, Nova). Unsupported models silently ignore the marker.
    """
    if not is_cacheable_system(system):
        return system_blocks
    if not system_blocks:
        return system_blocks
    # Append a cachePoint after the existing text blocks.
    return list(system_blocks) + [{"cachePoint": {"type": "default"}}]


def extract_bedrock_cache_tokens(resp: dict) -> tuple[int, int]:
    """Bedrock returns cache metrics under response['usage']."""
    if not isinstance(resp, dict):
        return 0, 0
    usage = resp.get("usage") or {}
    read = int(usage.get("cacheReadInputTokens") or 0)
    write = int(usage.get("cacheWriteInputTokens") or usage.get("cacheCreationInputTokens") or 0)
    return read, write


# ── Helper for callers ───────────────────────────────────────────────────────

def cache_enabled(settings: dict | None) -> bool:
    """Honor the global toggle. Defaults to True when the key is missing."""
    if not settings:
        return True
    return bool(settings.get("prompt_cache_enabled", True))
