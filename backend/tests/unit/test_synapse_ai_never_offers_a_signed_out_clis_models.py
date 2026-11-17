"""Synapse AI never offers a model CLI that is not signed in here, and says which ones are not, with how to sign in
(F1676).

Synapse AI reads the models it can recommend from its `models` topic: `models_you_can_pick`, every installed
provider's models. A CLI that is installed but not signed in (Gemini CLI, Codex, Copilot CLI on a stack where only
Claude Code is) was listed like any other, with nothing said of its sign-in, so asked which model to use it
recommended `cli.gemini.gemini-2.5-pro` and told the person they "can use any of them straight away". The pickers
already leave such a CLI out (F1418, F1592); the topic did not.

Now the topic leaves a CLI known to be signed out out of `models_you_can_pick`, and `cli_not_signed_in` names each
such CLI with its own sign-in hint.

Near misses: a signed-in CLI's models stay; a provider with a key stays; a CLI whose sign-in cannot be told without a
model call (None) stays; with no CLI status read yet, every listed model stays, as before.
"""
from __future__ import annotations

import pytest

from core import builder_readers, model_choices
from core.providers import cli_status

LISTED = {"anthropic_cli": ["cli.claude.claude-sonnet-4-6", "cli.claude.claude-haiku-4-5"],
          "gemini_cli": ["cli.gemini.gemini-2.5-pro", "cli.gemini.flash"],
          "github_copilot_cli": ["cli.copilot.auto"],
          "openai": ["gpt-4.1"]}


def _status(provider: str, signed_in: bool | None) -> cli_status.CliStatus:
    base = cli_status.PROVIDERS[provider]
    return cli_status.CliStatus(provider, base, cli_status.LABELS[base], installed=True, signed_in=signed_in,
                                sign_in_hint=cli_status.SIGN_IN_HINTS[base] if signed_in is False else None)


@pytest.fixture
def stack(monkeypatch):
    async def listed():
        return {k: list(v) for k, v in LISTED.items()}

    statuses: dict[str, cli_status.CliStatus] = {}
    monkeypatch.setattr(model_choices, "choices", listed)
    monkeypatch.setattr(cli_status, "known", lambda provider: statuses.get(provider))
    return statuses


async def _topic() -> dict:
    return await builder_readers._models({})


class TestASignedOutCli:
    async def test_its_models_are_not_offered(self, stack):
        stack.update(anthropic_cli=_status("anthropic_cli", True), gemini_cli=_status("gemini_cli", False),
                     github_copilot_cli=_status("github_copilot_cli", False))
        offered = (await _topic())["models_you_can_pick"]
        assert "gemini_cli" not in offered and "github_copilot_cli" not in offered, offered

    async def test_it_is_named_with_how_to_sign_in(self, stack):
        stack.update(anthropic_cli=_status("anthropic_cli", True), gemini_cli=_status("gemini_cli", False))
        said = (await _topic())["cli_not_signed_in"]
        assert said == {"Gemini CLI": cli_status.SIGN_IN_HINTS["cli.gemini"]}, said


class TestNearMisses:
    async def test_a_signed_in_cli_and_a_keyed_provider_stay(self, stack):
        stack.update(anthropic_cli=_status("anthropic_cli", True), gemini_cli=_status("gemini_cli", False))
        offered = (await _topic())["models_you_can_pick"]
        assert offered["anthropic_cli"] == LISTED["anthropic_cli"] and offered["openai"] == ["gpt-4.1"], offered

    async def test_a_cli_that_cannot_tell_stays(self, stack):
        stack.update(gemini_cli=_status("gemini_cli", None))
        topic = await _topic()
        assert topic["models_you_can_pick"]["gemini_cli"] == LISTED["gemini_cli"]
        assert "cli_not_signed_in" not in topic

    async def test_with_no_status_read_every_model_stays(self, stack):
        topic = await _topic()
        assert topic["models_you_can_pick"] == LISTED and "cli_not_signed_in" not in topic
