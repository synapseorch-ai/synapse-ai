"""A model CLI's alias has the facts of the model it runs (F1598 D).

Claude Code offers its own aliases beside full model ids: `haiku`, `sonnet`, `opus` and `fable`, each the model of
that tier Claude Code runs today. The pickers said nothing under them (no context, no tier) while a full id beside
them had its line, because the facts are kept by model id and an alias is none. Now each alias is read as the model it
runs, as Claude Code resolves it (its `--output-format json` answer names the model in `modelUsage`): its tier, its
context window and how it ranks are that model's.

Near misses: a full id is read as itself, the bare CLI with no model is unchanged, and a name that is not one of the
CLI's aliases, or another CLI's, is not taken for one.
"""
from __future__ import annotations

import pytest

from core.providers import model_facts, window_tokens

ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
           "fable": "claude-fable-5-1"}


@pytest.mark.parametrize("alias,target", sorted(ALIASES.items()))
def test_an_alias_has_its_models_facts(alias, target):
    said, runs = model_facts.size_of(f"cli.claude.{alias}"), model_facts.size_of(f"cli.claude.{target}")
    assert said is not None and said == runs, (alias, said, runs)


@pytest.mark.parametrize("alias,target", sorted(ALIASES.items()))
def test_an_alias_has_its_models_window(alias, target):
    assert window_tokens(f"cli.claude.{alias}") == window_tokens(f"cli.claude.{target}") is not None


def test_the_resolution_is_one_table():
    from core.providers.cli_aliases import resolved

    assert {a: resolved(f"cli.claude.{a}") for a in ALIASES} == {a: f"cli.claude.{t}" for a, t in ALIASES.items()}


@pytest.mark.parametrize("model", ["cli.claude.claude-haiku-4-5", "cli.claude", "cli.claude.haiku-ish",
                                   "cli.gemini.haiku", "anthropic.haiku", "haiku"])
def test_anything_else_is_read_as_itself(model):
    from core.providers.cli_aliases import resolved

    assert resolved(model) is None
    if model == "cli.claude.claude-haiku-4-5":
        assert model_facts.size_of(model).generation == (4, 5)
