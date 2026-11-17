"""Each model is sized from its provider's own tier or its published parameters (F1598, commit A: the facts).

Synapse AI's pickers listed every model alike. An automatic pick ranked a family's models by price, so a dearer, older
model read as stronger, and nothing kept a 4B model from writing. Each model the product knows now has a row of facts:
its family, its tier where its provider orders its own, its generation, and whether it is small. Every row names its
source, the provider's tier or the model card the figure was read from.

The rule for small is the one accepted for it, "as proposed":
- dense under 30B;
- a mixture of experts only when its active parameters are under 20B and its total is under 200B;
- the nano, lite, micro and flash-lite tiers;
- never Haiku, mini or Flash.

Dense models of 9B or less are not for building, and are left out of Synapse AI's pickers: "let's not show 8b 4b
models". A model whose size is not published, or whose architecture its own family does not state, gets no
facts-based mark.

Near misses: an id the table does not know has no row; each threshold's edge falls on the side the rule says; a
model's provider prefixes (a Bedrock region, a CLI, a platform's own) find the same row; a newer id that only shares an
older one's opening letters is not read as that one.
"""
from __future__ import annotations

import pytest

from core.providers import model_facts as F


class TestEveryRow:
    def test_each_row_names_its_source(self):
        assert F.ROWS
        bad = [r.prefix for r in F.ROWS if not (r.source.startswith("https://") or r.source == "provider tier")]
        assert not bad, bad

    def test_no_prefix_is_listed_twice(self):
        prefixes = [r.prefix for r in F.ROWS]
        assert len(prefixes) == len(set(prefixes))

    def test_a_row_sized_by_its_parameters_says_what_the_rule_says(self):
        sized = [r for r in F.ROWS if r.total is not None and r.tier is None]
        assert sized
        wrong = [r.prefix for r in sized if r.small is not F.is_small(r.total, r.active, r.dense)]
        assert not wrong, wrong


class TestTheLookup:
    @pytest.mark.parametrize("model, family, tier, small", [
        ("claude-sonnet-4-6", "claude", 3, False),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude", 2, False),
        ("cli.claude.claude-opus-4-6", "claude", 4, False),
        ("claude-fable-5", "claude", 5, False),
        ("gpt-5-mini", "gpt", 2, False),
        ("gpt-5-nano", "gpt", 1, True),
        ("gemini-2.5-flash", "gemini", 2, False),
        ("gemini-3.1-flash-lite-preview", "gemini", 1, True),
        ("us.amazon.nova-pro-v1:0", "nova", 3, False),
        ("amazon.nova-micro-v1:0", "nova", 1, True),
        ("synapse.llama-3-1-8b", "llama", None, True),
        ("us.meta.llama3-1-8b-instruct-v1:0", "llama", None, True),
        ("meta.llama4-maverick-17b-instruct-v1:0", "llama", None, False),
        ("groq.openai/gpt-oss-120b", "gpt-oss", None, True),
        ("qwen.qwen3-32b-v1:0", "qwen3", None, False),
        ("qwen3-30b-a3b", "qwen3", None, True),
        ("mistral.mistral-7b-instruct-v0:2", "mistral", None, True),
    ])
    def test_a_model_finds_its_row_through_its_prefixes(self, model, family, tier, small):
        row = F.size_of(model)
        assert row is not None, model
        assert (row.family, row.tier, row.small) == (family, tier, small), row

    @pytest.mark.parametrize("model", ["", "acme-model-9", "moonshot.kimi-k3", "minimax.MiniMax-M2.7",
                                       "gpt-5.1-mini", "qwen3.8-27b"])
    def test_an_id_it_does_not_know_has_no_facts(self, model):
        row = F.size_of(model)
        assert row is None or (row.small is None and row.tier is None), row

    def test_the_longest_prefix_wins(self):
        assert F.size_of("gemini-2.5-flash-lite").tier == 1
        assert F.size_of("gemini-2.5-flash").tier == 2
        assert F.size_of("claude-opus-4-5-20251101").generation == (4, 5)
        assert F.size_of("claude-opus-4-20250514").generation == (4,)

    def test_the_tiers_run_fable_and_mythos_then_opus_sonnet_haiku(self):
        tiers = [F.size_of(m).tier for m in ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5",
                                             "claude-haiku-5")]
        assert tiers[0] == tiers[1] > tiers[2] > tiers[3] > tiers[4], tiers


class TestTheSmallRule:
    @pytest.mark.parametrize("total, active, dense, small", [
        (29.9, None, True, True), (30, None, True, False),
        (199, 19, False, True), (200, 19, False, False), (199, 20, False, False),
        # Under 30B it is small whatever its architecture; above, an architecture not stated gives no mark.
        (24, None, None, True), (111, None, None, None), (None, None, None, None)])
    def test_its_edges(self, total, active, dense, small):
        assert F.is_small(total, active, dense) is small

    @pytest.mark.parametrize("model", ["claude-haiku-4-5", "gpt-5-mini", "gemini-2.5-flash", "gemini-3-flash-preview"])
    def test_haiku_mini_and_flash_are_not_small(self, model):
        assert F.size_of(model).small is False

    @pytest.mark.parametrize("model", ["gpt-4.1-nano", "us.amazon.nova-lite-v1:0", "amazon.nova-micro-v1:0",
                                       "gemini-2.5-flash-lite"])
    def test_nano_lite_micro_and_flash_lite_are(self, model):
        assert F.size_of(model).small is True


class TestNotForBuilding:
    def test_it_is_exactly_the_dense_models_of_9b_or_less(self):
        marked = {r.prefix for r in F.ROWS if r.fit == "not_for_building"}
        small_dense = {r.prefix for r in F.ROWS if r.total is not None and r.total <= 9 and r.active is None}
        assert marked == small_dense
        assert marked

    @pytest.mark.parametrize("model", ["gemma-3-4b-it", "gemma-3-1b-it", "ministral-3b-2410", "ministral-8b-latest",
                                       "us.meta.llama3-2-3b-instruct-v1:0", "synapse.llama-3-1-8b", "qwen3-8b",
                                       "mistral.mistral-7b-instruct-v0:2"])
    def test_each_is_marked(self, model):
        assert F.size_of(model).fit == "not_for_building", model

    @pytest.mark.parametrize("model", ["gemma-3-12b-it", "qwen3-14b", "us.meta.llama3-1-70b-instruct-v1:0",
                                       "openai.gpt-oss-20b-1:0", "qwen3-30b-a3b"])
    def test_a_small_model_larger_than_that_or_a_mixture_is_not(self, model):
        assert F.size_of(model).fit is None, model
