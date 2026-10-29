"""An agent step's context budget is a share of its model's window, not two fixed character counts (D339 P1).

Prod, 10-29 (TRI1's triage): compaction fired at 80,000 characters — about 20k tokens, a tenth of Haiku 4.5's window —
and the 100,000-character spill line sat above it, so one result could set off a compaction on its own. Naveen
(10-30): compact at 50% of the model's window by default, a setting in General; spill and reads drawn from the same
budget; the fixed limits kept for a model whose window is not known, or a share of 0.
"""

from __future__ import annotations

import json

from core.config import default_settings, set_settings_provider
from core.context_budget import budget
from core.providers import window_tokens


class TestTheWindowTable:
    def test_a_provider_prefix_is_looked_through(self):
        assert window_tokens("synapse.claude-sonnet-4-6") == 200_000
        assert window_tokens("us.anthropic.claude-haiku-4-5") == 200_000
        assert window_tokens("synapse.nova-pro") == 300_000

    def test_the_longest_prefix_wins(self):
        assert window_tokens("gemma-3-1b-it") == 32_000
        assert window_tokens("synapse.gemma-3-4b-it") == 128_000

    def test_an_unknown_model_is_not_known(self):
        assert window_tokens("some-local-model") is None
        assert window_tokens("") is None


class TestTheBudget:
    def test_haiku_compacts_far_later_than_the_fixed_limit(self):
        b = budget("claude-haiku-4-5", default_settings())
        assert b.source == "window" and b.window == 200_000
        assert b.compact_at > 4 * 80_000
        assert b.read_cap < b.spill_at < b.compact_at

    def test_a_small_window_gets_a_small_budget(self):
        small, large = budget("gemma-3-1b-it", default_settings()), budget("claude-haiku-4-5", default_settings())
        assert small.compact_at < large.compact_at / 4
        assert small.read_cap < small.spill_at

    def test_the_share_is_the_workspaces(self):
        s = default_settings()
        half = budget("claude-haiku-4-5", s).compact_at
        s["auto_compact_window_pct"] = 25
        assert abs(budget("claude-haiku-4-5", s).compact_at * 2 - half) <= 8

    def test_an_unknown_window_or_a_share_of_0_keeps_the_fixed_limits(self):
        s = default_settings()
        s["auto_compact_threshold"], s["vault_threshold"] = 120_000, 90_000
        for model, pct in (("some-local-model", 50), ("claude-haiku-4-5", 0)):
            s["auto_compact_window_pct"] = pct
            b = budget(model, s)
            assert (b.source, b.compact_at, b.spill_at) == ("fixed", 120_000, 90_000)
            assert b.read_cap <= 45_000

    def test_the_prompts_last_cut_leaves_room_in_the_window(self):
        b = budget("claude-haiku-4-5", default_settings())
        assert b.compact_at < b.prompt_max < 200_000 * 4


class TestTheEngineHoldsReadsToTheCap:
    def test_a_vault_read_is_given_the_steps_cap(self):
        from core.react_engine import _vault_read_capped

        schema = {"type": "object", "properties": {"path": {}, "max_chars": {}}}
        assert _vault_read_capped("read_file", {"path": "a"}, schema, 9_000)["max_chars"] == 9_000
        assert _vault_read_capped("vault__read_json", {"path": "a", "max_chars": 99_000}, schema, 9_000)[
            "max_chars"] == 9_000
        assert _vault_read_capped("read_file", {"path": "a", "max_chars": 500}, schema, 9_000)["max_chars"] == 500

    def test_any_other_call_is_left_as_it_was(self):
        from core.react_engine import _vault_read_capped

        no_cap = {"type": "object", "properties": {"path": {}}}
        assert _vault_read_capped("read_file", {"path": "a"}, no_cap, 9_000) == {"path": "a"}
        with_cap = {"type": "object", "properties": {"max_chars": {}}}
        assert _vault_read_capped("scrape_url", {"url": "u"}, with_cap, 9_000) == {"url": "u"}

    def test_the_spill_line_is_the_steps(self):
        from core.vault import maybe_vault

        s = default_settings()
        s["vault_threshold"] = 100_000
        set_settings_provider(lambda: s)
        try:
            out = "x" * 50_000
            assert maybe_vault("scrape_url", out) == out
            assert "vault_file" in json.loads(maybe_vault("scrape_url", out, threshold=40_000))
        finally:
            set_settings_provider(None)
