"""One ordering ranks models: a measured rank first, then today's order, with each family's own tiers and generations
kept within the places that family holds (F1598, commit A).

An automatic pick ranked by reasoning, native tools and then price. Within a family, price put a dearer, older model
ahead of a newer, stronger one: Opus 4.6 over Opus 5.5, since 4.6 cost more. Now, within the places a family's
models hold in that order, they run by their provider's tier, then the newer generation, then price.
- Fable and Mythos run before Opus, Opus before Sonnet, and Sonnet before Haiku.
- Across families nothing moves until a measure says so: a measured rank runs first.
- The pick, the stronger-model offer, the "strongest" wish and `/api/models`' `ranked` list all read this one order,
  so they never disagree.

An automatic pick never gives a small model the job when one that is not small fits too (the 4B writer). A model the
person names is theirs, small or not.

Near misses: a menu of one model from each family is in exactly today's order; a family whose models have no tier
keeps today's order; with only small models fitting, the pick is still made.
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

    def test_a_measured_rank_runs_first(self):
        F.set_measured({"gemini-2.5-flash": (None, 7), "gpt-4.1": (None, 9)})
        menu = [entry("claude-sonnet-4-6", 15), entry("gemini-2.5-flash", 2.5), entry("gpt-4.1", 8)]
        assert ids(MM.ordered(menu, has_tools=True)) == ["gpt-4.1", "gemini-2.5-flash", "claude-sonnet-4-6"]


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

    def test_a_model_the_person_names_is_theirs(self):
        menu = [entry("gemini-3.1-flash-lite-preview", 0.75), entry("gemini-2.5-flash", 2.5)]
        said = MM.resolve_wish("gemini-3.1-flash-lite-preview", "simple", has_tools=True, menu=menu)
        assert said.id == "gemini-3.1-flash-lite-preview"
