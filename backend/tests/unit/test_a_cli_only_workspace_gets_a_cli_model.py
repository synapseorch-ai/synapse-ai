"""A builder-made agent on a workspace whose only models are CLI ones gets a CLI model (F1410; BLD1's clips 4 and 5 on
v2.0 c52f0775; Naveen 11-06, in eb's tab and CAP3's: mirror the local rule, the default first, then a CLI model, then
a local one, ranked by what the same model lists at).

F966 picks a CLI or a local model only when the person asks, and a local one when it is the only kind there is. A
workspace signed in to Claude Code and Gemini CLI, with no key of its own and no platform model, had neither: every
entry was dropped, and the agent could not be saved, "No model this workspace can use reads long inputs (a window of
128k tokens or more)", for an agent that never asked for long inputs, on a menu whose Claude models read 1M tokens.
With no own-key or platform model at all, the workspace's default is the agent's where it is a CLI or local model that
fits; else the CLI models are ranked as the own keys are (the cheapest for a narrow job, the strongest within the
ceiling otherwise), each by the list price of the model it runs, which is never said as its cost; a local model only
when no CLI model fits. A workspace with an own-key or platform model picks as before, and a model that cannot be
picked is said by its real reason.
"""
from __future__ import annotations

import pytest

from core import model_choices
from core.native_builder import models_menu as M

SIMPLE_TOOLS = "the strongest that fits, within the automatic price ceiling, from your CLI models (this workspace has " \
               "no own key or platform model): one that calls tools"


def _entry(id, *, source="cli", tools="prompt", vision=False, reasoning=False, price_out=None, rank_out=None,
           provider="anthropic_cli", can_pay=True, window=None):
    return M.ModelEntry(id=id, name=id, provider=provider, source=source, tools=tools, vision=vision,
                        reasoning=reasoning, price_in=None if price_out is None else price_out / 5,
                        price_out=price_out, can_pay=can_pay, window=window, rank_out=rank_out)


# take2's Claude Code models, each ranked by its model's own list price (USD per 1M output tokens).
FABLE = _entry("cli.claude.claude-fable-5-1", rank_out=50, window=1_000_000)
OPUS = _entry("cli.claude.claude-opus-5-5", rank_out=20, window=1_000_000)
SONNET_46 = _entry("cli.claude.claude-sonnet-4-6", rank_out=15, window=1_000_000)
SONNET_55 = _entry("cli.claude.claude-sonnet-5-5", rank_out=10, window=1_000_000)
HAIKU = _entry("cli.claude.claude-haiku-4-5", rank_out=5, window=200_000)
ALIAS = _entry("cli.claude.sonnet")          # a short alias: no rate-card row, ranked after them
TAKE2 = [ALIAS, FABLE, HAIKU, OPUS, SONNET_55, SONNET_46]
LOCAL = _entry("ollama.llama3", source="local", tools="native", price_out=0, provider="ollama")
KEY_NO_TOOLS = _entry("hf.tiny", source="key", tools="none", price_out=0.1, provider="huggingface")


class TestACliOnlyWorkspace:
    def test_take2s_agent_gets_the_strongest_within_the_ceiling(self):
        assert M.pick("simple", has_tools=True, menu=TAKE2) == M.Picked(SONNET_46.id, SIMPLE_TOOLS)

    def test_a_narrow_job_gets_the_lowest_listed(self):
        got = M.pick("simple", has_tools=True, menu=TAKE2, narrow=True)
        assert got == M.Picked(HAIKU.id, "the lowest-cost model that fits a narrow job, from your CLI models (this "
                                         "workspace has no own key or platform model): one that calls tools")

    def test_reasoning_gets_the_strongest_within_the_ceiling(self):
        assert M.pick("reasoning", has_tools=True, menu=TAKE2).id == SONNET_46.id

    def test_the_workspaces_default_first_where_it_fits(self):
        got = M.pick("simple", has_tools=True, menu=TAKE2, default=OPUS.id)
        assert got == M.Picked(OPUS.id, "your workspace's default model, which fits, from your CLI models (this "
                                        "workspace has no own key or platform model): one that calls tools")
        # A default that does not fit is passed over.
        assert M.pick("vision", has_tools=False, menu=[*TAKE2, _entry("cli.gemini.flash", vision=True,
                                                                      provider="gemini_cli")],
                      default=OPUS.id).id == "cli.gemini.flash"

    def test_long_inputs_read_the_window_as_for_any_model(self):
        small = _entry("cli.codex.mini", rank_out=2, window=64_000, provider="codex_cli")
        assert M.pick("simple", has_tools=True, menu=[small, HAIKU], long_inputs=True, narrow=True).id == HAIKU.id

    def test_a_cli_model_is_never_said_to_cost_anything(self):
        assert all(e.price_out is None for e in TAKE2)
        assert "$" not in M.pick("simple", has_tools=True, menu=TAKE2).reason

    def test_an_alias_alone_is_picked(self):
        assert M.pick("simple", has_tools=True, menu=[ALIAS]).id == ALIAS.id


class TestCliAndLocal:
    def test_a_cli_model_before_a_local_one(self):
        assert M.pick("simple", has_tools=True, menu=[LOCAL, HAIKU]).id == HAIKU.id

    def test_a_local_default_first(self):
        assert M.pick("simple", has_tools=True, menu=[LOCAL, HAIKU], default=LOCAL.id).id == LOCAL.id

    def test_a_local_one_when_no_cli_model_fits(self):
        """Today's local rule, as it was: the only local model that fits."""
        seeing_local = _entry("ollama.llava", source="local", tools="native", vision=True, price_out=0,
                              provider="ollama")
        assert M.pick("vision", has_tools=False, menu=[HAIKU, seeing_local]) == M.Picked(
            "ollama.llava", "the only model here that fits: one that reads images")


class TestAsBefore:
    def test_an_own_key_or_platform_model_present_that_does_not_fit_is_said(self):
        """Mirroring the local rule: a CLI model only with no own-key or platform model at all (Naveen)."""
        assert M.pick("simple", has_tools=True, menu=[KEY_NO_TOOLS, *TAKE2]) == M.NoModel(
            "No model this workspace can use calls tools.")

    def test_one_that_fits_is_picked_and_no_cli_one(self):
        key = _entry("claude-haiku-4-5", source="key", tools="native", price_out=5, provider="anthropic")
        assert M.pick("simple", has_tools=True, menu=[key, *TAKE2]).id == "claude-haiku-4-5"

    def test_stronger_than_offers_no_cli_model(self):
        """By design (eb): an offer says what it costs, and a CLI model has no price."""
        assert M.stronger_than(HAIKU.id, "simple", has_tools=True, menu=TAKE2) is None


class TestTheRealReason:
    def test_never_long_inputs_when_they_were_not_asked(self):
        got = M.pick("vision", has_tools=False, menu=TAKE2)
        assert got == M.NoModel("No model this workspace can use reads images.")
        assert "long inputs" not in M._no_model(TAKE2, "simple", True).issue

    def test_long_inputs_when_they_were(self):
        assert M.pick("simple", has_tools=True, menu=[HAIKU], long_inputs=True, narrow=True).id == HAIKU.id
        small = _entry("cli.codex.mini", rank_out=2, window=64_000, provider="codex_cli")
        assert M.pick("simple", has_tools=True, menu=[small], long_inputs=True) == M.NoModel(
            "No model this workspace can use reads long inputs (a window of 128k tokens or more).")


class TestTheMenu:
    async def test_a_cli_model_is_ranked_by_its_models_list_price_and_priced_at_nothing(self):
        async def listed():
            return {"anthropic_cli": ["cli.claude.claude-sonnet-4-6", "cli.claude.claude-haiku-4-5",
                                      "cli.claude.sonnet"], "anthropic": ["claude-haiku-4-5"]}
        model_choices.set_model_choices(listed)
        try:
            by_id = {e.id: e for e in await M.menu()}
        finally:
            model_choices.set_model_choices(None)
        assert (by_id["cli.claude.claude-sonnet-4-6"].rank_out, by_id["cli.claude.claude-haiku-4-5"].rank_out) == (
            15, 5)
        assert by_id["cli.claude.sonnet"].rank_out is None
        assert all(by_id[i].price_out is None for i in by_id if i.startswith("cli."))
        # An own-key model is ranked by its price, as ever.
        assert by_id["claude-haiku-4-5"].rank_out is None and by_id["claude-haiku-4-5"].price_out == 5


@pytest.fixture(autouse=True)
def _clean():
    yield
    model_choices.set_model_choices(None)
    M.set_model_facts(None)
