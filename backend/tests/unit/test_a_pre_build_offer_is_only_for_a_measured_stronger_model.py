"""The offer of a bigger model before a build is made only when it is worth making, and by the model's own name (F1606).

A browser test of the panel: in 7 of 10
sessions, before anything had failed, Synapse AI said "This looks like a bigger build … Claude Opus 4.5 usually does
these better, at about 2× the price of Claude Sonnet 4.6": to a person who had just saved Sonnet 4.6 as their Builder
model, for asks as small as a weekly email summary, with a claim no measure of ours backs (`models_menu.stronger_than`
ranks by reasoning, then price). A model was also named by its id: "claude-fable-5 usually does these better".

Now:
- No offer before a build when the person chose its model: the panel's pick or the Builder model setting
  (`effort.MODEL_SOURCE_KEY`, recorded by the route).
- Otherwise only a model F1598's measured rank says is stronger (`models_menu.measured_stronger`, empty until F1598
  measures; `set_measured_stronger` is its seam): until then there is no offer before a build at all.
- Every model in it is said by its name (`models_menu.display_name`: the menu's, else the host's facts), never an id
  where a name exists; and its price is said as it is ("about 1.4× the price", never "about 1×").
- The offer after a failure (F1018's try_stronger) is its own and unchanged.
"""
from __future__ import annotations

import pytest

from core.native_builder import effort
from core.native_builder import models_menu as MM
from test_a_heavy_build_is_offered_a_stronger_model_first import _check, menu  # noqa: F401  (menu: autouse)


def _named(id_: str, name: str, out: float) -> MM.ModelEntry:
    return MM.ModelEntry(id=id_, name=name, provider="claude", source="managed", tools="native", vision=False,
                         reasoning=True, price_in=out / 4, price_out=out, can_pay=True)


NAMED = [_named("synapse.claude-haiku", "Claude Haiku 4.5", 5), _named("synapse.claude-sonnet", "Claude Sonnet 4.6", 15)]


@pytest.fixture
def measured(monkeypatch):
    """F1598's rank, as a stub: Sonnet measured stronger than Haiku for building."""
    MM.set_measured_stronger({"synapse.claude-haiku": ["synapse.claude-sonnet"]})
    yield
    MM.set_measured_stronger({})


def _offers(events):
    return [e for e in events if e.get("type") == effort.KIND]


class TestAChosenModelIsNotSecondGuessed:
    @pytest.mark.parametrize("source", ["pick", "builder_model"])
    async def test_no_offer_when_the_person_chose_it(self, client, measured, source):
        out, events = await _check(**{effort.MODEL_SOURCE_KEY: source})
        assert out["next"] != "offer" and not _offers(events), source

    async def test_the_everyday_default_may_still_be_offered_a_measured_one(self, client, measured):
        out, events = await _check(**{effort.MODEL_SOURCE_KEY: "everyday"})
        assert out["next"] == "offer" and _offers(events)[0]["model"] == "synapse.claude-sonnet"


class TestOnlyAMeasuredStrongerModel:
    async def test_with_no_measure_there_is_no_offer_before_a_build(self, client):
        out, events = await _check(**{effort.MODEL_SOURCE_KEY: "everyday"})
        assert out["next"] != "offer" and not _offers(events)

    def test_the_rank_is_empty_until_f1598_measures(self):
        assert MM.measured_stronger("synapse.claude-haiku", NAMED) is None


class TestItSaysModelsByName:
    async def test_the_offer_names_both_models_never_their_ids(self, client, measured, menu):
        menu["menu"] = list(NAMED)
        out, events = await _check(**{effort.MODEL_SOURCE_KEY: "everyday"})
        [offer] = _offers(events)
        assert offer["use"] == "Use Claude Sonnet 4.6" and offer["continue"] == "Continue with Claude Haiku 4.5"
        assert "synapse.claude" not in out["say"]

    async def test_a_model_the_menu_lacks_is_named_by_the_hosts_facts(self, client, menu):
        async def facts(ids):
            return {i: {"name": "Claude Fable 5"} for i in ids if i == "synapse.claude-fable-5"}
        MM.set_model_facts(facts)
        try:
            assert await MM.display_name("synapse.claude-fable-5", NAMED) == "Claude Fable 5"
            assert await MM.display_name("synapse.claude-sonnet", NAMED) == "Claude Sonnet 4.6"
            assert await MM.display_name("cli.claude.sonnet", NAMED) == "cli.claude.sonnet", "no name: the id, as is"
        finally:
            MM.set_model_facts(None)


class TestThePrice:
    @pytest.mark.parametrize("up, now, said", [(14, 10, "about 1.4× the price"), (30, 10, "about 3× the price"),
                                                (25, 10, "about 2.5× the price")])
    def test_it_is_said_as_it_is(self, up, now, said):
        assert effort._price(_named("u", "U", up), _named("n", "N", now)) == said


class TestEvenAMeasuredStepUpHoldsSynapseAIsPrompt:
    """F1591's rule on the measured path: a model too small for Synapse AI's long prompt is never offered, measured or
    not (a tester's Mistral Large, 32K, offered as "better" on prod)."""

    def test_a_too_small_model_ranked_first_is_passed_over(self):
        small = MM.ModelEntry(id="synapse.mistral-large", name="Mistral Large", provider="mistral", source="managed",
                              tools="native", vision=False, reasoning=True, price_in=4, price_out=12, can_pay=True,
                              window=32_768)
        menu = [*NAMED, small]
        MM.set_measured_stronger({"synapse.claude-haiku": ["synapse.mistral-large", "synapse.claude-sonnet"]})
        try:
            picked = MM.measured_stronger("synapse.claude-haiku", menu)
        finally:
            MM.set_measured_stronger({})
        assert picked is not None and picked.id == "synapse.claude-sonnet"
