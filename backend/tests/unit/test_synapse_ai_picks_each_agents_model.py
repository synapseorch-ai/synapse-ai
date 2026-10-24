"""Synapse AI picks each agent's model in code, from a menu of what this workspace can call (F966, SAI2-P2; D300).

Every new agent got the workspace's default model: a classifier and a long-form writer the same, and a tools agent on
a default that cannot call tools. F966's plan (Naveen, 10-23): the planner states what an agent needs, never an id;
code picks from a typed menu (`models_menu`), with hard filters (tools, vision, a key or a wallet that pays; a CLI or
local model only when the person asks) and a ceiling on what an automatic pick may cost (~$15 per 1M output tokens),
above which Synapse AI may only offer a stronger model. A person's own wish is theirs: not bound by the ceiling, and
never guessed at. SAI2's review: one usable model is picked, and none is said, never a silent default.
"""
from __future__ import annotations

import pytest

from core import model_choices
from core.native_builder import models_menu as M


def _facts(table: dict[str, dict]):
    async def facts(ids):
        return {i: table[i] for i in ids if i in table}
    return facts


async def _menu(choices: dict[str, list[str]], facts: dict[str, dict] | None = None):
    async def listed():
        return choices
    model_choices.set_model_choices(listed)
    M.set_model_facts(_facts(facts) if facts is not None else None)
    try:
        return await M.menu()
    finally:
        model_choices.set_model_choices(None)
        M.set_model_facts(None)


def _entry(id, *, source="key", tools="native", vision=True, reasoning=False, price_out=None, can_pay=True,
           name=None, provider="anthropic"):
    return M.ModelEntry(id=id, name=name or id, provider=provider, source=source, tools=tools, vision=vision,
                        reasoning=reasoning, price_in=None if price_out is None else price_out / 5,
                        price_out=price_out, can_pay=can_pay)


# ── the menu ────────────────────────────────────────────────────────────────────────────────────────────────────────

class TestTheMenu:
    async def test_it_is_built_from_what_the_workspace_can_call_and_what_is_known_of_each(self):
        menu = await _menu({"anthropic": ["claude-sonnet-4-5"], "anthropic_cli": ["cli.claude"],
                            "ollama": ["ollama.llama3"], "model_gateway": ["synapse.nova-pro"]})
        by_id = {e.id: e for e in menu}
        sonnet = by_id["claude-sonnet-4-5"]
        assert (sonnet.provider, sonnet.source, sonnet.tools, sonnet.vision) == ("anthropic", "key", "native", True)
        assert (sonnet.price_in, sonnet.price_out, sonnet.can_pay) == (3, 15, True)
        assert (by_id["cli.claude"].source, by_id["cli.claude"].tools, by_id["cli.claude"].vision) == ("cli", "prompt",
                                                                                                        False)
        assert by_id["cli.claude"].price_out is None, "a CLI runs on a subscription: no per-token price"
        assert (by_id["ollama.llama3"].source, by_id["ollama.llama3"].price_out) == ("local", 0)
        assert by_id["synapse.nova-pro"].source == "managed"

    async def test_a_host_s_facts_are_its_own_per_model(self):
        menu = await _menu({"model_gateway": ["synapse.nova-pro", "synapse.nova-lite"]}, {
            "synapse.nova-pro": {"name": "Nova Pro", "vision": True, "price_out": 3.2, "can_pay": True},
            "synapse.nova-lite": {"name": "Nova Lite", "tools": "none", "can_pay": False}})
        by_id = {e.id: e for e in menu}
        assert (by_id["synapse.nova-pro"].name, by_id["synapse.nova-pro"].price_out) == ("Nova Pro", 3.2)
        assert (by_id["synapse.nova-lite"].tools, by_id["synapse.nova-lite"].can_pay) == ("none", False)

    async def test_a_list_that_cannot_be_read_is_an_empty_menu(self):
        async def broken():
            raise RuntimeError("providers down")
        model_choices.set_model_choices(broken)
        try:
            assert await M.menu() == []
        finally:
            model_choices.set_model_choices(None)


# ── pick ────────────────────────────────────────────────────────────────────────────────────────────────────────────

HAIKU = _entry("claude-haiku-4-5", price_out=5)
SONNET = _entry("claude-sonnet-4-5", price_out=15, reasoning=True)
OPUS = _entry("claude-opus-4-1", price_out=75, reasoning=True)
MINI = _entry("gpt-4.1-mini", price_out=1.6, provider="openai", vision=False)
UNPRICED = _entry("oaic.house-model", provider="openai_compatible", price_out=None)
CLI = _entry("cli.claude", source="cli", tools="prompt", vision=False)
LOCAL = _entry("ollama.llama3", source="local", price_out=0, provider="ollama")
BLIND_NO_TOOLS = _entry("hf.tiny", tools="none", vision=False, price_out=0.1, provider="huggingface")


class TestPick:
    def test_simple_is_the_lowest_cost_that_fits(self):
        got = M.pick("simple", has_tools=True, menu=[SONNET, HAIKU, MINI, CLI, LOCAL])
        assert got == M.Picked("gpt-4.1-mini", "the lowest-cost model that fits")

    def test_reasoning_is_the_strongest_within_the_ceiling(self):
        got = M.pick("reasoning", has_tools=True, menu=[HAIKU, SONNET, OPUS, MINI])
        assert got == M.Picked("claude-sonnet-4-5", "a stronger model, for reasoning")
        assert M.AUTO_PICK_CEILING_USD_PER_MTOK_OUT == 15

    def test_vision_needs_a_model_that_sees(self):
        assert M.pick("vision", has_tools=False, menu=[MINI, HAIKU, SONNET]) == M.Picked(
            "claude-haiku-4-5", "it can read images")

    def test_an_agent_with_tools_never_gets_a_model_without_them_and_prefers_native(self):
        prompt_only = _entry("bedrock.cheap", tools="prompt", price_out=0.5, provider="bedrock")
        assert M.pick("simple", has_tools=True, menu=[BLIND_NO_TOOLS, prompt_only, HAIKU]).id == "claude-haiku-4-5"
        assert M.pick("simple", has_tools=False, menu=[BLIND_NO_TOOLS, HAIKU]).id == "hf.tiny"

    def test_a_cli_or_local_model_is_never_picked_unasked(self):
        assert M.pick("simple", has_tools=True, menu=[CLI, LOCAL, HAIKU]).id == "claude-haiku-4-5"

    def test_a_model_the_wallet_cannot_pay_for_is_never_picked(self):
        broke = _entry("synapse.nova-pro", source="managed", price_out=3.2, can_pay=False, provider="model_gateway")
        assert M.pick("simple", has_tools=True, menu=[broke, HAIKU]).id == "claude-haiku-4-5"

    def test_an_unpriced_model_comes_after_priced_ones_never_excluded(self):
        assert M.pick("simple", has_tools=True, menu=[UNPRICED, SONNET]).id == "claude-sonnet-4-5"
        assert M.pick("simple", has_tools=True, menu=[UNPRICED]).id == "oaic.house-model"

    def test_one_usable_model_is_picked_and_said_so(self):
        """SAI2's review: "fewer than two → the default" left a tools agent on a default that cannot call tools."""
        assert M.pick("reasoning", has_tools=True, menu=[BLIND_NO_TOOLS, HAIKU]) == M.Picked(
            "claude-haiku-4-5", "the only model here that fits")

    def test_none_usable_is_said_never_a_silent_default(self):
        broke = _entry("synapse.nova-pro", source="managed", provider="model_gateway", price_out=3.2, can_pay=False)
        assert M.pick("simple", has_tools=False, menu=[broke]) == M.NoModel(
            "No model this workspace can use can be paid for now (the wallet).")
        got = M.pick("simple", has_tools=True, menu=[BLIND_NO_TOOLS, CLI])
        assert got == M.NoModel("No model this workspace can use calls tools.")
        assert M.pick("vision", has_tools=False, menu=[MINI]) == M.NoModel(
            "No model this workspace can use reads images.")

    def test_no_menu_is_the_workspace_default_as_today(self):
        assert M.pick("reasoning", has_tools=True, menu=[]) is None


class TestStrongerThan:
    def test_it_offers_the_strongest_above_the_ceiling_with_its_price(self):
        got = M.stronger_than("claude-sonnet-4-5", "reasoning", has_tools=True, menu=[HAIKU, SONNET, OPUS])
        assert got == M.Picked("claude-opus-4-1", "about $75 per 1M output tokens")

    def test_it_is_the_next_real_step_up_within_the_ceiling(self):
        """SAI2's probe (F1018): "stronger than nova-pro" jumped to Opus at $75. From a model below the ceiling, the
        offer is the strongest within it; above it only from a model already at the ceiling."""
        nova = _entry("synapse.nova-pro", source="managed", provider="model_gateway", price_out=3.2)
        assert M.stronger_than("synapse.nova-pro", "simple", has_tools=True, menu=[nova, SONNET, OPUS]) == M.Picked(
            "claude-sonnet-4-5", "about $15 per 1M output tokens")

    def test_nothing_above_the_ceiling_or_unpriced_is_no_offer(self):
        pricey_unknown = _entry("oaic.giant", price_out=None, reasoning=True)
        assert M.stronger_than("claude-sonnet-4-5", "reasoning", has_tools=True,
                               menu=[HAIKU, SONNET, pricey_unknown]) is None


class TestWishes:
    def test_a_family_word_stays_within_the_ceiling_and_the_stronger_one_is_offered(self):
        """SAI2's probe: "use Claude" (reasoning) picked Opus at $75. A family word is a kind of model, not a model:
        the ceiling holds, and the card offers the stronger one (`stronger_than`)."""
        menu = [MINI, HAIKU, SONNET, OPUS]
        assert M.resolve_wish("use Claude for the writer", "reasoning", has_tools=True, menu=menu) == M.Picked(
            "claude-sonnet-4-5", "you asked for Claude")
        assert M.resolve_wish("claude", "simple", has_tools=True, menu=menu).id == "claude-haiku-4-5"
        assert M.stronger_than("claude-sonnet-4-5", "reasoning", has_tools=True, menu=menu).id == "claude-opus-4-1"

    def test_a_model_named_outright_is_theirs_above_the_ceiling(self):
        """Only a model the person names ("Opus", its id) skips the ceiling: their choice, its price on the card."""
        menu = [MINI, HAIKU, SONNET, OPUS]
        assert M.resolve_wish("use Opus", "reasoning", has_tools=True, menu=menu) == M.Picked(
            "claude-opus-4-1", "you asked for Opus")
        assert M.resolve_wish("claude-opus-4-1", "simple", has_tools=True, menu=menu).id == "claude-opus-4-1"
        # The card's words name what matched, never the whole sentence (SAI2's re-check).
        assert M.resolve_wish("put it on Opus", "reasoning", has_tools=True, menu=menu) == M.Picked(
            "claude-opus-4-1", "you asked for Opus")

    def test_a_family_is_matched_by_its_provider_too_and_says_the_real_reason(self):
        """SAI2's probe: "gpt" said "no model calls tools", though an openai model with tools fitted, and gpt-oss
        matched but could not be paid for."""
        mystery = _entry("openai.mystery", provider="openai", price_out=None)
        oss = _entry("synapse.gpt-oss-120b", source="managed", provider="model_gateway", price_out=0.6, can_pay=False)
        assert M.resolve_wish("gpt", "simple", has_tools=True, menu=[oss, mystery, HAIKU]).id == "openai.mystery"
        assert M.resolve_wish("gpt", "simple", has_tools=True, menu=[oss, HAIKU]) == M.NoModel(
            "synapse.gpt-oss-120b can't be paid for now (the wallet).")

    def test_a_local_or_cli_model_named_by_its_family_is_a_wish_for_it(self):
        mistral = _entry("ollama.mistral", source="local", price_out=0, provider="ollama")
        assert M.resolve_wish("mistral", "simple", has_tools=True, menu=[HAIKU, mistral]).id == "ollama.mistral"
        asked = M.resolve_wish("banana", "simple", has_tools=True, menu=[HAIKU, mistral, CLI])
        assert isinstance(asked, M.Ask) and asked.options == ["claude-haiku-4-5", "cli.claude", "ollama.mistral"]

    def test_a_model_s_own_id_is_that_model(self):
        nova = _entry("synapse.nova-pro", source="managed", price_out=3.2, provider="model_gateway")
        assert M.resolve_wish("nova pro", "simple", has_tools=True, menu=[nova, HAIKU]) == M.Picked(
            "synapse.nova-pro", "you asked for nova pro")

    def test_cheapest_and_strongest_are_the_rules(self):
        menu = [MINI, HAIKU, SONNET, OPUS]
        assert M.resolve_wish("the cheapest one", "reasoning", has_tools=True, menu=menu).id == "gpt-4.1-mini"
        assert M.resolve_wish("the strongest model", "simple", has_tools=True, menu=menu).id == "claude-opus-4-1"
        assert M.resolve_wish("the most capable one", "simple", has_tools=True, menu=menu).id == "claude-opus-4-1"

    def test_a_cli_or_local_model_is_there_when_asked_for(self):
        assert M.resolve_wish("use the claude cli", "simple", has_tools=True, menu=[HAIKU, CLI]).id == "cli.claude"
        assert M.resolve_wish("a local model", "simple", has_tools=True, menu=[HAIKU, LOCAL]).id == "ollama.llama3"

    def test_two_families_or_none_is_a_question_with_the_menu_s_names(self):
        menu = [MINI, HAIKU]
        both = M.resolve_wish("claude or gpt", "simple", has_tools=True, menu=menu)
        assert isinstance(both, M.Ask) and both.options == ["claude-haiku-4-5", "gpt-4.1-mini"]
        unknown = M.resolve_wish("use mistral", "simple", has_tools=True, menu=menu)
        assert isinstance(unknown, M.Ask) and "mistral" in unknown.question.lower()

    def test_a_wished_model_that_cannot_do_the_job_is_said(self):
        got = M.resolve_wish("hf tiny", "simple", has_tools=True, menu=[BLIND_NO_TOOLS, HAIKU])
        assert got == M.NoModel("hf.tiny can't call tools, which this agent needs.")


@pytest.fixture(autouse=True)
def _clean():
    yield
    M.set_model_facts(None)
