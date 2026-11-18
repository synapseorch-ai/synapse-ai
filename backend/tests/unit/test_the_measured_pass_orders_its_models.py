"""The measured pass orders the models it measured, and nothing else (F1598 D).

Four models were measured building a set of real asks on a scratch install: Claude Sonnet 4.6, Haiku 4.5 and Haiku 5.5
through the Claude CLI, and Gemini 3 Flash through its API. Two asks were measured on each: a support assistant over
docs and an order API (cap-02), and a refund that waits for an approval before it is sent (cap-05); a third, a change
to an existing form, was not measured. The table holds each model's rank, from the asks it got right first and then
how helpful a judge found it, and no fit: "Recommended" stays the facts' call. The rank orders only the measured models
of one family's tier, so of these four only Haiku 4.5 and Haiku 5.5 change places, Haiku 4.5 first.

Near misses: a model's fit is the same with the table as without it; an unmeasured model has no rank; a stronger tier
still runs above a measured Haiku; clearing the table (`set_measured({})`) clears it, and `set_measured(None)` puts
the shipped table back.
"""
from __future__ import annotations

import pytest

from core.native_builder import models_menu as MM
from core.providers import model_facts as F

IDS = ("claude-sonnet-4-6", "claude-haiku-4-5", "claude-haiku-5-5", "gemini-3-flash-preview")


@pytest.fixture(autouse=True)
def shipped():
    F.set_measured(None)
    yield
    F.set_measured(None)


def entry(model: str, out: float) -> MM.ModelEntry:
    return MM._entry(model, {model: {"input_per_1m": out / 5, "output_per_1m": out}})


@pytest.mark.parametrize("model", IDS)
def test_each_measured_model_has_a_rank_and_no_fit(model):
    said = F.measured(model)
    assert said is not None and said[0] is None and isinstance(said[1], int), (model, said)


def test_its_cli_and_api_ids_read_the_same_row():
    assert F.measured("cli.claude.claude-haiku-4-5") == F.measured("claude-haiku-4-5") is not None


def test_the_ranks_follow_how_they_did():
    rank = {m: F.measured(m)[1] for m in IDS}
    assert (rank["claude-sonnet-4-6"] > rank["claude-haiku-4-5"] > rank["claude-haiku-5-5"]
            > rank["gemini-3-flash-preview"]), rank


def test_only_haiku_4_5_and_5_5_change_places_within_their_tier():
    menu = [entry("claude-haiku-5-5", 5), entry("claude-opus-4-6", 25), entry("claude-haiku-4-5", 5)]
    assert [e.id for e in MM.ordered(menu, has_tools=True)] == ["claude-opus-4-6", "claude-haiku-4-5",
                                                              "claude-haiku-5-5"]


@pytest.mark.parametrize("model", IDS)
def test_the_table_never_changes_a_fit(model):
    with_table = MM._entry(model, {}).fit
    F.set_measured({})
    assert MM._entry(model, {}).fit == with_table, model


def test_an_unmeasured_model_has_no_rank_and_clearing_clears():
    assert F.measured("claude-opus-4-6") is None
    F.set_measured({})
    assert F.measured("claude-haiku-4-5") is None
