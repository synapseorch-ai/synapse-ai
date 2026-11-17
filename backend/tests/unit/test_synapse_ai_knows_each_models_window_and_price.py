"""Synapse AI's models topic gives each model it can pick its window and price, from the model picker's own list, so
it never says one window for a whole family (F1684).

Asked which models it could use, Synapse AI said all Claude models have a 200K window: the topic listed model ids
only, so the window came from the model's memory. Fable 5 and Opus 5 hold 1M; Sonnet 4.5 holds 200K. Now the topic's
`model_windows_and_prices` gives each listed model's window and price as the model picker knows them
(`models_menu.facts_of`: the engine's window table and rate card, with a host's own facts over both), and its note
says to state a window or price only from it.

Near misses: a CLI's model has no price per token (its subscription is said instead), a local model is free, a model
with no known window or price has none said, a host's own price wins over the rate card, and a menu that cannot be
read leaves the topic as it was.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import builder_readers, model_choices
from core.model_pricing import DEFAULT_MODEL_PRICING
from core.native_builder import models_menu

LISTED = {"anthropic": ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-4-5-20250929"],
          "anthropic_cli": ["cli.claude.claude-opus-5-5"],
          "ollama": ["ollama.qwen3:8b-not-in-any-table"],
          "openai": ["gpt-unknown-preview"]}


@pytest.fixture
def stack(monkeypatch):
    async def choices():
        return {k: list(v) for k, v in LISTED.items()}

    monkeypatch.setattr(model_choices, "choices", choices)
    from core.providers import cli_status

    monkeypatch.setattr(cli_status, "known", lambda provider: None)
    models_menu.set_model_facts(None)
    yield
    models_menu.set_model_facts(None)


async def _said() -> dict:
    return (await builder_readers._models({}))["model_windows_and_prices"]


class TestEachModelsWindow:
    async def test_fable_and_opus_5_hold_1m_and_sonnet_4_5_holds_200k(self, stack):
        said = await _said()
        assert said["claude-fable-5-2"]["window"] == "1M tokens"
        assert said["claude-opus-5-5"]["window"] == "1M tokens"
        assert said["claude-sonnet-4-5-20250929"]["window"] == "200K tokens"

    async def test_each_window_is_the_picker_s_own(self, stack):
        said = await _said()
        for entry in await models_menu.facts_of([m for ms in LISTED.values() for m in ms]):
            if entry.window:
                assert said[entry.id]["window"] == f"{models_menu.window_words(entry.window)} tokens", entry.id

    async def test_a_model_with_no_known_window_has_none_said(self, stack):
        said = await _said()
        assert "window" not in said.get("gpt-unknown-preview", {})


class TestEachModelsPrice:
    async def test_an_api_model_s_price_is_the_rate_card_s(self, stack):
        said = await _said()
        card = DEFAULT_MODEL_PRICING["claude-opus-5-5"]
        assert said["claude-opus-5-5"]["price"] == f"${card['input_per_1m']} in, ${card['output_per_1m']} out per 1M tokens"
        for entry in await models_menu.facts_of(LISTED["anthropic"]):
            assert said[entry.id]["price"] == (f"${models_menu.money(entry.price_in)} in, "
                                               f"${models_menu.money(entry.price_out)} out per 1M tokens")

    async def test_a_cli_model_is_its_subscription_and_a_local_one_is_free(self, stack):
        said = await _said()
        assert "subscription" in said["cli.claude.claude-opus-5-5"]["price"]
        assert said["cli.claude.claude-opus-5-5"]["window"] == "1M tokens"
        assert said["ollama.qwen3:8b-not-in-any-table"]["price"].startswith("free")

    async def test_a_model_with_no_known_price_has_none_said(self, stack):
        assert "price" not in (await _said()).get("gpt-unknown-preview", {})

    async def test_a_host_s_own_price_and_window_win(self, stack):
        async def host(ids):
            return {"claude-opus-5-5": {"price_in": 5.5, "price_out": 27.5, "window": 500_000}}

        models_menu.set_model_facts(host)
        said = await _said()
        assert said["claude-opus-5-5"] == {"window": "500K tokens", "price": "$5.50 in, $27.50 out per 1M tokens"}


class TestTheTopic:
    async def test_its_note_says_to_state_a_window_or_price_only_from_it(self, stack):
        note = (await builder_readers._models({}))["note"]
        assert "model_windows_and_prices" in note and "never for a family as a whole" in note, note

    async def test_its_shape_names_the_new_key(self, stack):
        assert builder_readers.shape_problems("models", await builder_readers._models({})) == []

    async def test_a_menu_that_cannot_be_read_leaves_the_topic_as_it_was(self, stack, monkeypatch):
        async def broken(ids):
            raise RuntimeError("no rate card")

        monkeypatch.setattr(models_menu, "facts_of", broken)
        topic = await builder_readers._models({})
        assert "model_windows_and_prices" not in topic and "model_windows_and_prices" not in topic["note"]
        assert topic["models_you_can_pick"]

    def test_no_window_or_price_is_typed_in_the_topic(self):
        """The figures are the picker's: the topic's code names none."""
        source = Path(builder_readers.__file__).read_text(encoding="utf-8")
        body = source[source.index("async def _models"):source.index("async def _agents")]
        for typed in ("200_000", "1_000_000", "200K", "1M", "128K", "input_per_1m"):
            assert typed not in body, typed
