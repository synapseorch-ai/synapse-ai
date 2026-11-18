"""One ordering ranks models: today's order, with each family's own tiers and generations kept within the places
that family holds, and a measured rank only among one tier's measured models (F1598, commit A; F1598 D).

An automatic pick ranked by reasoning, native tools and then price. Within a family, price put a dearer, older model
ahead of a newer, stronger one: Opus 4.6 over Opus 5.5, since 4.6 cost more. Now, within the places a family's
models hold in that order, they run by their provider's tier, then the newer generation, then price.
- Fable and Mythos run before Opus, Opus before Sonnet, and Sonnet before Haiku.
- Across families nothing moves, and a measured rank orders only the measured models of one tier among
  themselves: a measure never lifts a model past a stronger tier, nor sets its fit.
- The pick, the stronger-model offer, the "strongest" wish and `/api/models`' `ranked` list all read this one order,
  so they never disagree.

An automatic pick never gives a small model the job when one that is not small fits too (the 4B writer). A model the
person names is theirs, small or not.

Near misses: a menu of one model from each family is in exactly today's order; a family whose models have no tier
keeps today's order; with only small models fitting, the pick is still made, and its reason says plainly that it is
a small model, a fact and not a judgement.
"""
from __future__ import annotations

import pytest

from core.native_builder import models_menu as MM
from core.providers import model_facts as F


@pytest.fixture(autouse=True)
def _no_measure():
    F.set_measured(None)
    yield
    F.set_measured(None)


def entry(model: str, out: float, *, inp: float | None = None, reasoning: bool | None = None) -> MM.ModelEntry:
    """`model` as the menu has it, at the price given (USD per 1M output tokens)."""
    e = MM._entry(model, {model: {"input_per_1m": inp if inp is not None else out / 5, "output_per_1m": out}})
    return e if reasoning is None else MM.replace(e, reasoning=reasoning)


def ids(entries) -> list[str]:
    return [e.id for e in entries]


class TestWithinAFamily:
    def test_a_newer_generation_runs_before_a_dearer_older_one(self):
        menu = [entry("claude-opus-4-6", 75), entry("claude-opus-5-5", 25)]
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-opus-5-5", "claude-opus-4-6"]

    def test_the_tiers_run_fable_opus_sonnet_haiku_whatever_they_cost(self):
        menu = [entry("claude-haiku-5-5", 12), entry("claude-sonnet-5-5", 14), entry("claude-opus-5-5", 13),
                entry("claude-fable-5", 11)]
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-fable-5", "claude-opus-5-5", "claude-sonnet-5-5",
                                                         "claude-haiku-5-5"]

    def test_a_family_keeps_only_the_places_it_held(self):
        menu = [entry("claude-opus-4-6", 75, reasoning=True), entry("gpt-4.1", 40, reasoning=True),
                entry("claude-opus-5-5", 25, reasoning=True)]
        today = ids(sorted(menu, key=lambda e: MM._strongest_first(e, True)))
        assert today == ["claude-opus-4-6", "gpt-4.1", "claude-opus-5-5"]
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-opus-5-5", "gpt-4.1", "claude-opus-4-6"]


class TestAcrossFamilies:
    def test_one_model_from_each_family_is_in_todays_order(self):
        menu = [entry("gemini-2.5-flash", 2.5), entry("claude-sonnet-4-6", 15), entry("gpt-4.1", 8),
                entry("us.amazon.nova-pro-v1:0", 3.2), entry("groq.openai/gpt-oss-120b", 0.75)]
        assert ids(MM.ordered(menu, has_tools=True)) == ids(sorted(menu, key=lambda e: MM._strongest_first(e, True)))

    def test_a_family_with_no_tiers_keeps_todays_order(self):
        menu = [entry("qwen3-8b", 0.2), entry("qwen3-235b-a22b", 1.2), entry("qwen3-32b", 0.6)]
        assert ids(MM.ordered(menu, has_tools=True)) == ids(sorted(menu, key=lambda e: MM._strongest_first(e, True)))

    def test_across_families_a_measured_rank_moves_nothing(self):
        """A measure orders the measured models of one tier among themselves, never a model past another tier or
        family: a few asks are too thin to outrank the tiers ("ordered by how they did", never "go first")."""
        F.set_measured({"gemini-2.5-flash": (None, 7), "gpt-4.1": (None, 9)})
        menu = [entry("claude-sonnet-4-6", 15), entry("gemini-2.5-flash", 2.5), entry("gpt-4.1", 8)]
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-sonnet-4-6", "gemini-2.5-flash", "gpt-4.1"]

    def test_an_unmeasured_stronger_tier_stays_above_a_measured_haiku(self):
        F.set_measured({"claude-haiku-4-5": (None, 9)})
        menu = [entry("claude-haiku-4-5", 5), entry("claude-opus-4-6", 25), entry("claude-sonnet-5-5", 15),
                entry("claude-haiku-5-5", 5)]
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-opus-4-6", "claude-sonnet-5-5", "claude-haiku-5-5",
                                                         "claude-haiku-4-5"]

    def test_two_measured_models_of_one_tier_follow_their_rank(self):
        F.set_measured({"claude-haiku-4-5": (None, 9), "claude-haiku-5-5": (None, 3)})
        menu = [entry("claude-haiku-4-5", 5), entry("claude-opus-4-6", 25), entry("claude-haiku-5-5", 5)]
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-opus-4-6", "claude-haiku-4-5", "claude-haiku-5-5"]

    def test_a_measure_never_sets_a_models_fit(self):
        """Its rank only: "Recommended" stays the facts' call, for a model that passed every ask too."""
        for model in ("claude-haiku-4-5", "claude-sonnet-4-6", "gemini-2.5-flash"):
            F.set_measured(None)
            facts = MM._entry(model, {}).fit
            F.set_measured({model: (None, 9)})
            assert MM._entry(model, {}).fit == facts, model


class TestAHostsRank:
    """A host's own rank (cloud's staff strength rank, through `with_host_facts`) is a deliberate decision: its model
    runs first, ahead of every tier, as the host's console says. The engine's measured rank stays within a tier, and
    where a model has both, the host's wins."""

    @pytest.fixture
    def host(self):
        said: dict[str, dict] = {}

        async def facts(ids):
            return {i: said[i] for i in ids if i in said}

        MM.set_model_facts(facts)
        yield said
        MM.set_model_facts(None)

    async def test_a_host_ranked_model_runs_first_ahead_of_its_tier(self, host):
        host["claude-haiku-4-5"] = {"rank": 99}
        menu = await MM.with_host_facts([entry("claude-opus-4-6", 25), entry("claude-haiku-4-5", 5),
                                         entry("claude-sonnet-5-5", 15)])
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-haiku-4-5", "claude-opus-4-6", "claude-sonnet-5-5"]

    async def test_two_host_ranked_models_follow_their_ranks(self, host):
        host.update({"claude-haiku-4-5": {"rank": 3}, "gpt-4.1": {"rank": 8}})
        menu = await MM.with_host_facts([entry("claude-opus-4-6", 25), entry("claude-haiku-4-5", 5),
                                         entry("gpt-4.1", 8)])
        assert ids(MM.ordered(menu, has_tools=True))[:2] == ["gpt-4.1", "claude-haiku-4-5"]

    async def test_where_a_model_has_both_the_hosts_rank_wins(self, host):
        F.set_measured({"claude-haiku-4-5": (None, 1), "claude-haiku-5-5": (None, 9)})
        host["claude-haiku-4-5"] = {"rank": 5}
        menu = await MM.with_host_facts([entry("claude-opus-4-6", 25), entry("claude-haiku-4-5", 5),
                                         entry("claude-haiku-5-5", 5)])
        assert ids(MM.ordered(menu, has_tools=True)) == ["claude-haiku-4-5", "claude-opus-4-6", "claude-haiku-5-5"]

    async def test_with_no_host_rank_the_order_is_as_before(self, host):
        host["claude-haiku-4-5"] = {"price_out": 5.0}
        menu = [entry("claude-haiku-4-5", 5), entry("claude-opus-4-6", 25), entry("claude-sonnet-5-5", 15)]
        assert ids(MM.ordered(await MM.with_host_facts(menu), has_tools=True)) == ids(MM.ordered(menu, has_tools=True))


class TestEveryReaderReadsIt:
    MENU = [("claude-opus-4-6", 14.5), ("claude-opus-5-5", 12), ("gpt-4.1", 8), ("claude-sonnet-4-6", 10)]

    def menu(self):
        return [entry(m, p, reasoning=True) for m, p in self.MENU]

    def test_the_pick_the_wish_and_the_route_agree(self):
        menu = self.menu()
        first = MM.ordered(menu, has_tools=True)[0].id
        assert first == "claude-opus-5-5"
        assert MM.pick("reasoning", has_tools=True, menu=menu).id == first
        assert MM.resolve_wish("the strongest", "simple", has_tools=True, menu=menu).id == first

    def test_the_stronger_offer_steps_up_in_that_order(self):
        menu = self.menu()
        assert MM.stronger_than("claude-sonnet-4-6", "simple", has_tools=True, menu=menu).id == "claude-opus-5-5"
        assert MM.stronger_than("claude-opus-4-6", "simple", has_tools=True, menu=menu).id == "claude-opus-5-5"
        assert MM.stronger_than("claude-opus-5-5", "simple", has_tools=True, menu=menu) is None


class TestNeverASmallModelOnAnAutomaticPick:
    def test_a_narrow_job_skips_a_small_model_for_one_that_is_not(self):
        menu = [entry("gemini-3.1-flash-lite-preview", 0.75), entry("gemini-2.5-flash", 2.5)]
        assert MM.pick("simple", has_tools=True, menu=menu, narrow=True).id == "gemini-2.5-flash"

    def test_a_writer_or_a_reasoner_too(self):
        menu = [entry("gemma-3-4b-it", 0.1), entry("gpt-5-nano", 0.4, reasoning=True), entry("gemini-2.5-flash", 2.5)]
        assert MM.pick("simple", has_tools=True, menu=menu).id == "gemini-2.5-flash"
        assert MM.pick("reasoning", has_tools=True, menu=menu).id == "gemini-2.5-flash"

    def test_with_only_small_models_the_pick_is_still_made(self):
        menu = [entry("gemini-3.1-flash-lite-preview", 0.75), entry("gpt-5-nano", 0.4)]
        assert isinstance(MM.pick("simple", has_tools=True, menu=menu), MM.Picked)

    @pytest.mark.parametrize("models", [["gemini-3.1-flash-lite-preview", "gpt-5-nano"], ["gpt-5-nano"]])
    def test_and_its_reason_says_plainly_it_is_a_small_model(self, models):
        menu = [entry(m, 0.4) for m in models]
        said = MM.pick("simple", has_tools=True, menu=menu)
        assert said.reason.endswith("; it is a small model, as no larger one here fits"), said.reason

    def test_a_pick_that_is_not_small_says_nothing_of_size(self):
        menu = [entry("gemini-3.1-flash-lite-preview", 0.75), entry("gemini-2.5-flash", 2.5), entry("acme-model-9", 1)]
        for narrow in (True, False):
            assert "small" not in MM.pick("simple", has_tools=True, menu=menu, narrow=narrow).reason

    def test_a_model_the_person_names_is_theirs(self):
        menu = [entry("gemini-3.1-flash-lite-preview", 0.75), entry("gemini-2.5-flash", 2.5)]
        said = MM.resolve_wish("gemini-3.1-flash-lite-preview", "simple", has_tools=True, menu=menu)
        assert said.id == "gemini-3.1-flash-lite-preview"
