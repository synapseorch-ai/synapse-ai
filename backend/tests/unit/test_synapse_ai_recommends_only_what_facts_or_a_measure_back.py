"""Synapse AI recommends a model for building only where its facts or a measure back it (F1598, commit A).

A model is Recommended for building from its facts alone when all three hold:
- the window it is served with is 200K tokens or more;
- it calls tools natively;
- its size is known and is not small.

A model the measured pass rated says what the measure said, either way. The host's own word (cloud's staff) says more
than either. A dense model of 9B or less is not for building unless a measure or the host says otherwise. A model its
provider deprecated, or the product knows is retired, is never Recommended for a workspace's own key; a platform's
own model retires by its catalogue instead.

Near misses: each gate alone keeps the mark off (199,999 tokens, tools described in the prompt, small, a size not
known, a window not known). The window a Synapse AI step refuses under 128K is as it was.
"""
from __future__ import annotations

import asyncio

import pytest

from core.native_builder import models_menu as MM
from core.providers import model_facts as F


@pytest.fixture(autouse=True)
def _no_measure():
    F.set_measured(None)
    yield
    F.set_measured(None)


def fit(model: str, window: int | None = 1_000_000, tools: str = "native", host: str | None = None) -> str | None:
    return F.builder_fit(model, window=window, tools=tools, host_fit=host)


class TestFromItsFacts:
    @pytest.mark.parametrize("model, window", [("claude-sonnet-4-6", 1_000_000), ("claude-haiku-4-5", 200_000),
                                               ("us.amazon.nova-pro-v1:0", 300_000), ("gemini-3-flash-preview", 1_048_576),
                                               ("cli.claude.claude-opus-4-6", 1_000_000)])
    def test_a_known_not_small_model_with_a_long_window_and_native_tools(self, model, window):
        assert fit(model, window) == "recommended"

    @pytest.mark.parametrize("model, window, tools", [
        ("claude-haiku-4-5", 199_999, "native"),
        ("claude-sonnet-4-6", 1_000_000, "prompt"),
        ("gemini-3.1-flash-lite-preview", 1_048_576, "native"),
        ("nvidia/llama-3.1-nemotron-70b-instruct", 128_000 * 2, "native"),
        ("acme-model-9", 1_000_000, "native"),
        ("claude-sonnet-4-6", None, "native"),
    ])
    def test_each_gate_alone_keeps_the_mark_off(self, model, window, tools):
        assert fit(model, window, tools) is None

    @pytest.mark.parametrize("model", ["gemini-2.5-pro", "gpt-5-mini", "gpt-5-2025-08-07", "gpt-4.1-nano"])
    def test_a_keys_model_its_provider_deprecated_or_retired_is_never_recommended(self, model):
        assert fit(model) is None

    def test_a_platforms_own_model_retires_by_its_catalogue_not_by_this(self):
        assert fit("synapse.gemini-2.5-pro") == "recommended"

    def test_a_newer_id_is_not_read_as_a_deprecated_one(self):
        assert F.deprecated("gpt-5.1") is False
        assert F.deprecated("gpt-5") is True and F.deprecated("openai.gpt-5-mini") is True


class TestNotForBuilding:
    @pytest.mark.parametrize("model", ["gemma-3-4b-it", "synapse.llama-3-1-8b", "ministral-3b-2410"])
    def test_whatever_its_window_and_tools(self, model):
        assert fit(model, 1_000_000, "native") == "not_for_building"


class TestAMeasureAndTheHost:
    def test_a_measure_takes_a_recommended_model_out(self):
        F.set_measured({"claude-haiku-4-5": ("not_for_building", None)})
        assert fit("claude-haiku-4-5", 200_000) == "not_for_building"
        assert fit("us.anthropic.claude-haiku-4-5-20251001-v1:0", 200_000) == "not_for_building"

    def test_a_measure_recommends_what_the_facts_would_not(self):
        F.set_measured({"gemini-3.1-flash-lite": ("recommended", 5), "gemma-3-4b": ("recommended", 1)})
        assert fit("gemini-3.1-flash-lite-preview") == "recommended"
        assert fit("gemma-3-4b-it", 128_000) == "recommended"
        assert F.measured("gemini-3.1-flash-lite-preview") == ("recommended", 5)

    def test_a_measured_rank_alone_leaves_the_fit_to_the_facts(self):
        F.set_measured({"claude-sonnet-4-6": (None, 9)})
        assert fit("claude-sonnet-4-6") == "recommended"
        assert fit("claude-sonnet-4-6", 128_000) is None

    def test_the_hosts_word_beats_a_measure_and_the_facts(self):
        F.set_measured({"claude-haiku-4-5": ("not_for_building", None)})
        assert fit("claude-haiku-4-5", 200_000, host="recommended") == "recommended"
        assert fit("claude-sonnet-4-6", host="not_for_building") == "not_for_building"


class TestTheMenuCarriesThem:
    def test_an_entry_has_its_facts_and_its_fit(self):
        e = MM._entry("claude-sonnet-4-6", {})
        assert (e.family, e.tier, e.small, e.fit) == ("claude", 3, False, "recommended"), e
        assert e.generation == (4, 6)

    def test_an_unknown_entry_has_none(self):
        e = MM._entry("acme-model-9", {})
        assert (e.family, e.tier, e.generation, e.small, e.fit, e.rank) == (None,) * 6

    def test_a_hosts_window_and_fit_are_read_over_the_tables(self):
        async def facts(ids):
            return {"synapse.claude-sonnet-4-6": {"window": 128_000}, "synapse.nova-pro": {"fit": "not_for_building"}}

        MM.set_model_facts(facts)
        try:
            entries = asyncio.run(MM.with_host_facts([MM._entry(i, {}) for i in ("synapse.claude-sonnet-4-6",
                                                                                   "synapse.nova-pro")]))
        finally:
            MM.set_model_facts(None)
        by = {e.id: e for e in entries}
        assert by["synapse.claude-sonnet-4-6"].fit is None, "128K is under the facts' 200K"
        assert by["synapse.nova-pro"].fit == "not_for_building"


def test_a_synapse_ai_step_still_refuses_a_model_under_128k():
    said = asyncio.run(MM.synapse_ai_refusal("mistral.mistral-large-2402-v1:0", source="pick"))
    assert said and "32K" in said and "128K" in said, said
