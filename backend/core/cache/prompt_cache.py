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
