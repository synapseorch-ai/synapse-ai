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
}
