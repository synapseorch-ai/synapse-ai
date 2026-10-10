"""
The shipped model rate card.

This is the seed for the ``model_pricing`` table, not the live table. It used
to live in ``setup.py`` and be copied into ``DATA_DIR/model_pricing.json`` by
the installer — which meant the only version-controlled copy of the rate card
sat in the installer, and an install whose data folder was rebuilt silently
lost cost tracking rather than falling back to anything.

Seeding inserts only the models that are missing, so an edit made on the usage
screen — or, in a hosted deployment, by an operator or an automated price feed
— survives every restart. Correcting a price here therefore reaches new
installs and leaves existing ones alone; changing an existing install's rate is
a write to the table, deliberately.

Rates are USD per million tokens. Entries may also carry
``cache_read_per_1m`` / ``cache_write_per_1m``; when they do not,
``core/usage_tracker.py`` applies the provider defaults documented there.
"""
from __future__ import annotations

DEFAULT_MODEL_PRICING: dict[str, dict] = {
    "gpt-4o": {"provider": "openai", "input_per_1m": 2.5, "output_per_1m": 10},
    "gpt-4o-mini": {"provider": "openai", "input_per_1m": 0.15, "output_per_1m": 0.6},
    "gpt-4.1": {"provider": "openai", "input_per_1m": 2, "output_per_1m": 8},
    "gpt-4.1-mini": {"provider": "openai", "input_per_1m": 0.4, "output_per_1m": 1.6},
    "gpt-4.1-nano": {"provider": "openai", "input_per_1m": 0.1, "output_per_1m": 0.4},
    "claude-sonnet-4-20250514": {"provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15},
    "claude-opus-4-20250514": {"provider": "anthropic", "input_per_1m": 15, "output_per_1m": 75},
    "claude-3-5-haiku-20241022": {"provider": "anthropic", "input_per_1m": 0.8, "output_per_1m": 4},
    "gemini-2.5-pro": {"provider": "gemini", "input_per_1m": 1.25, "output_per_1m": 10},
    "gemini-2.5-flash": {"provider": "gemini", "input_per_1m": 0.3, "output_per_1m": 2.5},
    "grok-3": {"provider": "grok", "input_per_1m": 3, "output_per_1m": 15},
    "grok-3-mini": {"provider": "grok", "input_per_1m": 0.3, "output_per_1m": 0.5},
    "deepseek-chat": {"provider": "deepseek", "input_per_1m": 0.27, "output_per_1m": 1.1},
    "deepseek-reasoner": {"provider": "deepseek", "input_per_1m": 0.55, "output_per_1m": 2.19},
    "gemini-3.1-pro-preview": {"provider": "gemini", "input_per_1m": 2, "output_per_1m": 12},
    "gemini-3-flash-preview": {"provider": "gemini", "input_per_1m": 0.5, "output_per_1m": 3},
    "gemini-3.1-flash-lite-preview": {"provider": "gemini", "input_per_1m": 0.125, "output_per_1m": 0.75},
    "gemini-2.5-flash-lite": {"provider": "gemini", "input_per_1m": 0.1, "output_per_1m": 0.4},
    "claude-sonnet-4-5-20250929": {"provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15},
    "claude-sonnet-4-6": {"provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15},
    "claude-opus-4-5-20251101": {"provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25},
    "claude-opus-4-6": {"provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25},
    # F63 (2026-09-17), from the claude-api reference's rate table (cached
    # 2026-06-24). Undated keys, so a dated id matches by prefix.
    "claude-opus-4-8": {"provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25},
    "claude-opus-4-7": {"provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25},
    "claude-opus-4-5": {"provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25},
    "claude-sonnet-4-5": {"provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15},
    "claude-haiku-4-5": {"provider": "anthropic", "input_per_1m": 1, "output_per_1m": 5},
    "claude-opus-5": {"provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25},
    "claude-sonnet-5": {"provider": "anthropic", "input_per_1m": 2, "output_per_1m": 10},
    "claude-fable-5": {"provider": "anthropic", "input_per_1m": 10, "output_per_1m": 50},
    # Cache reads at $0.25/M, a quarter of the usual tenth of the input rate.
    "claude-fable-5-1": {
        "provider": "anthropic", "input_per_1m": 10, "output_per_1m": 50, "cache_read_per_1m": 0.25,
    },
    # The hosted OpenAI-style providers (PROV1, D245), keyed by the routed id.
    # NVIDIA's hosted catalogue is a free trial with no per-token price, and
    # OpenRouter prices every model itself (its /models says), so neither has
    # rows: their calls are unpriced, as an unlisted model's always were.
    # Moonshot (Kimi) — platform.kimi.ai/docs/pricing/chat, read 2026-10-10.
    "moonshot.kimi-k3": {"provider": "moonshot", "input_per_1m": 3.0, "output_per_1m": 15.0, "cache_read_per_1m": 0.3},
    "moonshot.kimi-k2.6": {"provider": "moonshot", "input_per_1m": 0.95, "output_per_1m": 4.0, "cache_read_per_1m": 0.16},
    "moonshot.kimi-k2.7-code": {"provider": "moonshot", "input_per_1m": 0.95, "output_per_1m": 4.0, "cache_read_per_1m": 0.19},
    # Mistral — docs.mistral.ai/models, read 2026-10-10 (list prices; Large 4 is on sale).
    "mistral.mistral-large-latest": {"provider": "mistral", "input_per_1m": 0.5, "output_per_1m": 1.5},
    "mistral.mistral-medium-latest": {"provider": "mistral", "input_per_1m": 1.5, "output_per_1m": 7.5},
    "mistral.mistral-small-latest": {"provider": "mistral", "input_per_1m": 0.15, "output_per_1m": 0.6},
    "mistral.ministral-14b-latest": {"provider": "mistral", "input_per_1m": 0.2, "output_per_1m": 0.2},
    "mistral.mistral-large-4": {"provider": "mistral", "input_per_1m": 1.36, "output_per_1m": 4.18, "cache_read_per_1m": 0.14},
    # Qwen — Alibaba Cloud Model Studio international, model-pricing (updated 2026-10-09), read 2026-10-10.
    "qwen.qwen3.8-max": {"provider": "qwen", "input_per_1m": 2.0, "output_per_1m": 6.0},
    "qwen.qwen3.8-flash": {"provider": "qwen", "input_per_1m": 0.15, "output_per_1m": 0.47},
    "qwen.qwen3.7-plus": {"provider": "qwen", "input_per_1m": 0.4, "output_per_1m": 1.6},
    "qwen.qwen3.7-max": {"provider": "qwen", "input_per_1m": 2.5, "output_per_1m": 7.5},
    # Z.ai — docs.z.ai/guides/overview/pricing, read 2026-10-10. glm-4.7-flash is free.
    "zai.glm-5.3": {"provider": "zai", "input_per_1m": 1.4, "output_per_1m": 4.4, "cache_read_per_1m": 0.26},
    "zai.glm-5.3-flashx": {"provider": "zai", "input_per_1m": 0.37, "output_per_1m": 1.25, "cache_read_per_1m": 0.075},
    "zai.glm-5.3-flash": {"provider": "zai", "input_per_1m": 0.15, "output_per_1m": 0.5, "cache_read_per_1m": 0.03},
    "zai.glm-5.2": {"provider": "zai", "input_per_1m": 1.4, "output_per_1m": 4.4, "cache_read_per_1m": 0.26},
    "zai.glm-4.7-flash": {"provider": "zai", "input_per_1m": 0, "output_per_1m": 0, "cache_read_per_1m": 0},
    # MiniMax — platform.minimax.io/docs/guides/pricing-paygo, read 2026-10-10 (M3 up to 512K input).
    "minimax.MiniMax-M3": {"provider": "minimax", "input_per_1m": 0.3, "output_per_1m": 1.2, "cache_read_per_1m": 0.06},
    "minimax.MiniMax-M2.7": {"provider": "minimax", "input_per_1m": 0.3, "output_per_1m": 1.2, "cache_read_per_1m": 0.06},
    "minimax.MiniMax-M2.7-highspeed": {"provider": "minimax", "input_per_1m": 0.6, "output_per_1m": 2.4, "cache_read_per_1m": 0.06},
    # Groq — console.groq.com/docs/models, read 2026-10-10. Cached gpt-oss input is half price.
    "groq.openai/gpt-oss-120b": {"provider": "groq", "input_per_1m": 0.15, "output_per_1m": 0.6, "cache_read_per_1m": 0.075},
    "groq.openai/gpt-oss-20b": {"provider": "groq", "input_per_1m": 0.075, "output_per_1m": 0.3, "cache_read_per_1m": 0.0375},
    "groq.qwen/qwen3.8-27b": {"provider": "groq", "input_per_1m": 0.8, "output_per_1m": 4.0},
    # Cerebras — inference-docs.cerebras.ai/models, read 2026-10-10. Cached input bills at the input rate.
    "cerebras.gpt-oss-120b": {"provider": "cerebras", "input_per_1m": 0.35, "output_per_1m": 0.75, "cache_read_per_1m": 0.35},
    "cerebras.qwen-3.8-27b": {"provider": "cerebras", "input_per_1m": 0.99, "output_per_1m": 1.49, "cache_read_per_1m": 0.99},
    # Together AI — docs.together.ai/docs/serverless/models, read 2026-10-10.
    "together.openai/gpt-oss-120b": {"provider": "together", "input_per_1m": 0.15, "output_per_1m": 0.6},
    "together.moonshotai/Kimi-K3": {"provider": "together", "input_per_1m": 2.7, "output_per_1m": 13.5, "cache_read_per_1m": 0.3},
    "together.zai-org/GLM-5.3": {"provider": "together", "input_per_1m": 1.4, "output_per_1m": 4.4, "cache_read_per_1m": 0.26},
    "together.deepseek-ai/DeepSeek-V4.1-Flash": {"provider": "together", "input_per_1m": 0.3, "output_per_1m": 1.2, "cache_read_per_1m": 0.006},
    "together.Qwen/Qwen3.7-Plus": {"provider": "together", "input_per_1m": 0.32, "output_per_1m": 1.28},
    "together.MiniMaxAI/MiniMax-M3": {"provider": "together", "input_per_1m": 0.3, "output_per_1m": 1.2, "cache_read_per_1m": 0.06},
    # Fireworks — docs.fireworks.ai/serverless/pricing (Standard tier), read 2026-10-10.
    "fireworks.accounts/fireworks/models/gpt-oss-120b": {"provider": "fireworks", "input_per_1m": 0.15, "output_per_1m": 0.6, "cache_read_per_1m": 0.015},
    "fireworks.accounts/fireworks/models/kimi-k3": {"provider": "fireworks", "input_per_1m": 3.0, "output_per_1m": 15.0, "cache_read_per_1m": 0.3},
    "fireworks.accounts/fireworks/models/glm-5p3": {"provider": "fireworks", "input_per_1m": 1.4, "output_per_1m": 4.4, "cache_read_per_1m": 0.26},
    "fireworks.accounts/fireworks/models/glm-5p3-flash": {"provider": "fireworks", "input_per_1m": 0.15, "output_per_1m": 0.5, "cache_read_per_1m": 0.03},
    "fireworks.accounts/fireworks/models/deepseek-v4p1-flash": {"provider": "fireworks", "input_per_1m": 0.3, "output_per_1m": 1.2, "cache_read_per_1m": 0.006},
    "fireworks.accounts/fireworks/models/qwen3p8-max": {"provider": "fireworks", "input_per_1m": 2.0, "output_per_1m": 6.0, "cache_read_per_1m": 0.25},
    "fireworks.accounts/fireworks/models/minimax-m3": {"provider": "fireworks", "input_per_1m": 0.3, "output_per_1m": 1.2, "cache_read_per_1m": 0.06},
}
