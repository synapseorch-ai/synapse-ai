"""Asked which models it can use, Synapse AI says plainly which model CLIs are signed in here, and names each one that
is installed but not signed in, with how to sign in (F1683).

The `models` topic left a signed-out CLI's models out and listed the CLI under `cli_not_signed_in` (F1676), but it
never said which CLIs were signed in, and nothing said what to do with either list. Asked "Which models can I use in
Synapse right now?" on a stack where Claude Code was signed in and Gemini CLI, Codex and Copilot CLI were not,
Synapse AI listed the API models, said Claude Code's models were there "if you have a Claude CLI session signed in",
and named none of the three signed-out CLIs.

Now the topic names the signed-in CLIs (`clis_signed_in`), and its note says every model it lists can be used now and
that each CLI not signed in is to be named with its sign-in line.

Near misses: a CLI whose sign-in cannot be told without a model call is in neither list and the note claims nothing
for it; with no CLI status read, or no CLI installed, nothing is said of CLIs; the note still says keys are never
shown.
"""
from __future__ import annotations

import pytest

from core import builder_readers, model_choices
from core.providers import cli_status

LISTED = {"anthropic_cli": ["cli.claude.sonnet", "cli.claude.claude-opus-5-5"],
          "gemini_cli": ["cli.gemini.gemini-2.5-pro"],
          "codex_cli": ["cli.codex.gpt-5.5"],
          "github_copilot_cli": ["cli.copilot.auto"],
          "anthropic": ["claude-sonnet-5-5"]}


def _status(provider: str, signed_in: bool | None) -> cli_status.CliStatus:
    base = cli_status.PROVIDERS[provider]
    return cli_status.CliStatus(provider, base, cli_status.LABELS[base], installed=True, signed_in=signed_in,
                                sign_in_hint=cli_status.SIGN_IN_HINTS[base] if signed_in is False else None)


@pytest.fixture
def stack(monkeypatch):
    listed = dict(LISTED)

    async def choices():
        return {k: list(v) for k, v in listed.items()}

    statuses: dict[str, cli_status.CliStatus] = {}
    monkeypatch.setattr(model_choices, "choices", choices)
    monkeypatch.setattr(cli_status, "known", lambda provider: statuses.get(provider))
    return statuses, listed


async def _topic() -> dict:
    return await builder_readers._models({})


def _the_stack_that_hedged(statuses: dict) -> None:
    statuses.update(anthropic_cli=_status("anthropic_cli", True), gemini_cli=_status("gemini_cli", False),
                    codex_cli=_status("codex_cli", False), github_copilot_cli=_status("github_copilot_cli", False))


class TestTheStackThatHedged:
    async def test_it_names_claude_code_as_signed_in(self, stack):
        _the_stack_that_hedged(stack[0])
        assert (await _topic())["clis_signed_in"] == ["Claude Code"]

    async def test_its_note_says_every_listed_model_can_be_used_now(self, stack):
        _the_stack_that_hedged(stack[0])
        note = (await _topic())["note"]
        assert "Every model in models_you_can_pick can be used here now" in note, note
        assert "clis_signed_in" in note and "never as if a sign-in might be missing" in note, note

    async def test_its_note_says_to_name_each_signed_out_cli_with_its_sign_in_line(self, stack):
        _the_stack_that_hedged(stack[0])
        topic = await _topic()
        assert set(topic["cli_not_signed_in"]) == {"Gemini CLI", "Codex", "Copilot CLI"}
        assert "name every CLI in cli_not_signed_in" in topic["note"] and "its sign-in line" in topic["note"]

    async def test_its_note_still_says_keys_are_never_shown(self, stack):
        _the_stack_that_hedged(stack[0])
        assert (await _topic())["note"].startswith("Keys are never shown, only whether one is set.")


class TestNearMisses:
    async def test_a_cli_that_cannot_tell_is_in_neither_list(self, stack):
        stack[0].update(anthropic_cli=_status("anthropic_cli", True), gemini_cli=_status("gemini_cli", None))
        topic = await _topic()
        assert topic["clis_signed_in"] == ["Claude Code"] and "cli_not_signed_in" not in topic
        assert "cli_not_signed_in" not in topic["note"], topic["note"]

    async def test_with_no_status_read_nothing_is_said_of_clis(self, stack):
        topic = await _topic()
        assert "clis_signed_in" not in topic and "cli_not_signed_in" not in topic
        assert "clis_signed_in" not in topic["note"] and "cli_not_signed_in" not in topic["note"]

    async def test_with_no_cli_installed_nothing_is_said_of_clis(self, stack):
        statuses, listed = stack
        for provider in list(listed):
            if provider.endswith("_cli"):
                del listed[provider]
        topic = await _topic()
        assert topic["models_you_can_pick"] == {"anthropic": ["claude-sonnet-5-5"]}
        assert "clis_signed_in" not in topic and "CLI" not in topic["note"]

    async def test_with_no_list_read_the_note_is_as_before(self, stack, monkeypatch):
        async def unread():
            return None

        monkeypatch.setattr(model_choices, "choices", unread)
        topic = await _topic()
        assert topic["note"] == "Keys are never shown, only whether one is set."
        assert "models_you_can_pick" not in topic

    async def test_the_topic_s_shape_names_the_new_list(self, stack):
        _the_stack_that_hedged(stack[0])
        assert builder_readers.shape_problems("models", await _topic()) == []
