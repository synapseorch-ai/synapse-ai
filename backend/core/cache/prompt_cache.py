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

Caching is gated by:
1. global settings.prompt_cache_enabled
2. system prompt length (Anthropic minimum ~1024 tokens for Sonnet/Opus,
   ~2048 for Haiku; we use a conservative 4000-char floor).

When unsure or when a provider isn't supported, these helpers no-op so the
caller's payload is unchanged.
"""
from typing import Any

# Anthropic charges for cache writes; only worth it when the prefix is meaningful.
# 4000 chars ≈ 1000 tokens — under Anthropic's minimum, the cache_control marker
# is silently ignored, so this floor avoids paying for ineligible writes.
MIN_CACHEABLE_CHARS = 4000

# Separator emitted by core.tools.build_system_prompt between the stable section
# (cacheable) and the volatile section (turn budget, current time, RAG context).
# Splitting here keeps the cache prefix byte-stable across turns.
VOLATILE_SEPARATOR = "\n---\n"


def is_cacheable_system(system: str | None) -> bool:
    return bool(system) and len(system) >= MIN_CACHEABLE_CHARS


def split_stable_volatile(system: str | None) -> tuple[str, str]:
    """Return (stable_prefix, volatile_suffix). Empty suffix when no separator."""
    if not system:
        return "", ""
    idx = system.find(VOLATILE_SEPARATOR)
    if idx < 0:
        return system, ""
    return system[:idx], system[idx:]


# ── Anthropic ────────────────────────────────────────────────────────────────

def decorate_anthropic_kwargs(kwargs: dict, system: str | None) -> dict:
    """Mutate `kwargs` so the system prompt + tool block become cache breakpoints.

    Anthropic supports up to 4 cache_control markers per request; we use 2:
      - end of stable section of system prompt (1 marker)
      - end of tools array (1 marker)

    The system prompt is split on the VOLATILE_SEPARATOR ("\\n---\\n"). The
    stable prefix is marked as cacheable; the volatile suffix (turn budget,
    current time, RAG context) goes into a second uncached text block so
    cache reads stay valid across turns even when those values change.
    """
    if not is_cacheable_system(system):
        return kwargs

    stable, volatile = split_stable_volatile(str(system))

    blocks: list[dict] = [{
        "type": "text",
        "text": stable,
        "cache_control": {"type": "ephemeral"},
    }]
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    kwargs["system"] = blocks

    # Mark the last tool definition so the whole tools array is part of the prefix.
    tools = kwargs.get("tools")
    if isinstance(tools, list) and tools:
        last = dict(tools[-1])  # shallow copy — don't mutate caller's list
        last["cache_control"] = {"type": "ephemeral"}
        kwargs["tools"] = tools[:-1] + [last]

    return kwargs


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
