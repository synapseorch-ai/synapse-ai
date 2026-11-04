"""A model the product knows its provider retired is offered nowhere, before any call fails (F1299, CAP3's cap-01 A2;
79 11-03).

cap-01's A2 (63dfb505): the narrow job's cheapest pick gave the new "Order Parser" agent gemini-2.5-flash-lite from
the person's own Gemini key, and both runs failed on Google's 404 "no longer available to new users". F1213 marks a
model gone only after a call to it fails in that workspace, and took it off the Gemini fallback list by hand; Google's
own model list still names it, so every new workspace with a Gemini key could get it in its first workflow. Now one
shipped list (`model_availability.RETIRED`, each with its provider and the provider's own words) is left out of every
choice (the builder's menu and picks, the agent editor's picker, the Models screen, a fallback list), for that
provider only, and never lapses or clears; its rate stays priced, as agents already set to it still call it.
"""
from __future__ import annotations

import inspect

import pytest

from core import model_availability as A
from core import model_choices
from core.native_builder import models_menu as M

GONE = "gemini-2.5-flash-lite"


@pytest.fixture(autouse=True)
def fresh_cache():
    A._marked_cache.clear()
    yield
    A._marked_cache.clear()
    model_choices.set_model_choices(None)


def _listing(found: dict[str, list[str]]) -> None:
    async def listed():
        return {k: list(v) for k, v in found.items()}
    model_choices.set_model_choices(listed)


def _retired() -> dict:
    retired = getattr(A, "RETIRED", None) or {}
    assert GONE in retired, "the product's own list of retired models names it"
    return retired


class TestAGeminiKeyNeverGetsIt:
    async def test_the_narrow_jobs_cheapest_pick_is_a_live_model(self):
        _listing({"gemini": ["gemini-2.5-flash", GONE]})  # what Google's own list says, before any call
        picked = M.pick("simple", has_tools=True, menu=await M.menu(), narrow=True)
        assert picked.id == "gemini-2.5-flash", picked

    async def test_the_workspaces_choices_leave_it_out_with_no_call_made(self):
        _listing({"gemini": ["gemini-2.5-flash", GONE]})
        assert await A.unavailable() == {}, "nothing has failed in this workspace"
        assert await model_choices.choices() == {"gemini": ["gemini-2.5-flash"]}

    async def test_it_never_lapses_or_clears(self, monkeypatch):
        _listing({"gemini": ["gemini-2.5-flash", GONE]})
        await A.clear(GONE)
        await A.note_success(GONE)
        assert await model_choices.choices() == {"gemini": ["gemini-2.5-flash"]}

    async def test_another_providers_list_keeps_its_own(self):
        # The Gemini CLI signs in another way; the retirement is the Gemini API's, for new callers.
        _listing({"gemini": ["gemini-2.5-flash", GONE], "cli.gemini": [GONE]})
        assert (await model_choices.choices())["cli.gemini"] == [GONE]


class TestOneSource:
    def test_each_says_its_provider_and_why(self):
        for model, entry in _retired().items():
            assert entry["provider"] and entry["reason"], model

    def test_no_fallback_list_offers_one(self):
        from core.routes import data

        source = inspect.getsource(data.get_models)
        fallbacks = [line for line in source.splitlines() if "_FALLBACK = [" in line]
        assert fallbacks, "the fallback lists are where they were"
        for model in _retired():
            assert not [line for line in fallbacks if f'"{model}"' in line], model

    def test_each_is_still_priced(self):
        # Agents already set to it still call it (Google refuses new callers only): their usage keeps its cost.
        from core.model_pricing import DEFAULT_MODEL_PRICING

        for model in _retired():
            assert model in DEFAULT_MODEL_PRICING, model
