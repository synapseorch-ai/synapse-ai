"""A model is priced by its own rate or not at all (F63, D104).

The lookup used to fall back to "any key sharing the model's first dash-word",
checked longest key first — which fires before a shorter exact prefix can win.
Every unknown `claude-*` was priced as Sonnet 4.5, `gpt-5` as `gpt-4.1-mini`,
`grok-4` as `grok-3-mini`. Usage figures and cost caps read these numbers, so a
wrong guess is worse than an honest "unpriced".
"""
import pytest

from core.model_pricing import DEFAULT_MODEL_PRICING
from core.usage_tracker import _resolve_pricing_entry, calculate_cost


def _rate(model):
    entry = _resolve_pricing_entry(model, DEFAULT_MODEL_PRICING)
    return None if entry is None else (entry["input_per_1m"], entry["output_per_1m"])


class TestAModelNeverBorrowsAnotherFamilysRate:
    @pytest.mark.parametrize("model", [
        "gpt-5", "grok-4", "claude-mythos-99", "mistral-large", "synapse.nova-pro-v1-0",
        "bedrock.us.anthropic.claude-opus-5",
    ])
    def test_an_unknown_model_is_unpriced(self, model):
        assert _resolve_pricing_entry(model, DEFAULT_MODEL_PRICING) is None

    def test_a_dated_model_takes_its_familys_undated_rate(self):
        assert _rate("gpt-4o-2024-08-06") == (2.5, 10)
        assert _rate("claude-opus-4-6-20260101") == (5, 25)

    def test_the_longest_prefix_wins(self):
        assert _rate("gpt-4.1-mini") == (0.4, 1.6)
        assert _rate("grok-3-mini") == (0.3, 0.5)


class TestTheCurrentClaudeRates:
    """From the claude-api reference (cached 2026-06-24), not from memory."""

    @pytest.mark.parametrize("model,rate", [
        ("claude-opus-5", (5, 25)),
        ("claude-sonnet-5", (2, 10)),
        ("claude-haiku-4-5", (1, 5)),
        ("claude-haiku-4-5-20251001", (1, 5)),
        ("claude-opus-4-8", (5, 25)),
        ("claude-opus-4-7", (5, 25)),
        ("claude-sonnet-4-5", (3, 15)),
        ("claude-fable-5-1", (10, 50)),
    ])
    def test_rate(self, model, rate):
        assert _rate(model) == rate

    def test_fable_5_1_reads_its_cache_at_a_quarter_dollar(self, monkeypatch):
        import core.usage_tracker as ut
        monkeypatch.setattr(ut, "_pricing_snapshot", DEFAULT_MODEL_PRICING)
        assert calculate_cost("claude-fable-5-1", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.25)
