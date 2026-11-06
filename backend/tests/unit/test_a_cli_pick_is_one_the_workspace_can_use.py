"""A CLI-only workspace's agent gets the workspace's default, and never a CLI that is not signed in (F1418; BLD1's clip 4
take2 on v2.0 9649b4cd; F1410 to Naveen's own answer, and his in CAP3's tab 11-06: the default counts when its CLI is
listed; a signed-out CLI is not picked, and how to sign it in is said).

take2's workspace had Claude Code signed in and Gemini CLI installed but not signed in, and its default model was
`cli.claude.claude-sonnet-4-6`. Its browser agent got `cli.gemini.gemini-3.1-pro-preview`, "the strongest that fits",
and Chat failed: Gemini's CLI could not answer. The default was read, but Claude Code's list (its aliases and the ids
its version names) has no `claude-sonnet-4-6`, so the menu had no entry for it to be picked as; and a CLI's sign-in,
known to Settings → Models, was not read. Now a CLI default off its CLI's list counts where that CLI is on the menu and
signed in, and a CLI known to be signed out is never picked: a pick passes it over, and a wish for it, or a workspace
with nothing else, is told how to sign it in. A CLI whose sign-in is not known counts as usable, as before.
"""
from __future__ import annotations

import time

import pytest

from core import config, model_choices
from core.native_builder import models_menu as M
from core.providers import cli_status

DEFAULT = "cli.claude.claude-sonnet-4-6"
#: What 9649b4cd's /api/models lists for Claude Code 2.1 and Gemini CLI 0.64 (the builder's models, take2's log :843).
LISTED = {
    "anthropic_cli": [*(f"cli.claude.{n}" for n in ("fable", "opus", "sonnet", "haiku", "claude-fable-5-1",
                                                    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5")),
                      "cli.claude"],
    "gemini_cli": [*(f"cli.gemini.{n}" for n in (
        "auto", "pro", "flash", "flash-lite", "gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-flash-preview",
        "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
        "gemini-2.5-pro", "gemma-4-31b-it", "gemma-4-26b-a4b-it")), "cli.gemini"],
}
GEMINI_OUT = ("Gemini CLI isn't signed in here: Run `gemini` in a terminal and choose Sign in with Google, or set "
              "GEMINI_API_KEY.")
CLAUDE_OUT = "Claude Code isn't signed in here: Run `claude` in a terminal and sign in."
NO_POOL = "from your CLI models (this workspace has no own key or platform model)"
TOOLS = ": one that calls tools"


def _probed(monkeypatch, provider: str, signed_in: bool | None) -> None:
    """What Settings → Models' probe found, as /api/models leaves it in cli_status's cache."""
    base = cli_status.PROVIDERS[provider]
    st = cli_status.CliStatus(provider, base, cli_status.LABELS[base], installed=True, signed_in=signed_in,
                              sign_in_hint=cli_status.SIGN_IN_HINTS[base] if signed_in is False else None)
    monkeypatch.setitem(cli_status._cache, provider, (time.monotonic() + 600, (None, None), st))


@pytest.fixture
def take2(monkeypatch):
    async def listed():
        return {k: list(v) for k, v in LISTED.items()}
    model_choices.set_model_choices(listed)
    config.set_settings_provider(lambda: {"model": DEFAULT})
    _probed(monkeypatch, "anthropic_cli", True)
    _probed(monkeypatch, "gemini_cli", False)
    yield
    config.set_settings_provider(None)


class TestTake2:
    async def test_the_workspaces_default_is_picked_though_its_cli_does_not_list_it(self, take2):
        menu, default = await M.menu(), M.workspace_model()
        assert default == DEFAULT and DEFAULT not in {e.id for e in menu}, "read, and off Claude Code's list"
        assert M.pick("simple", has_tools=True, menu=menu, default=default) == M.Picked(
            DEFAULT, f"your workspace's default model, which fits, {NO_POOL}{TOOLS}")

    async def test_with_no_default_a_signed_in_cli_is_picked(self, take2):
        config.set_settings_provider(lambda: {})
        got = M.pick("simple", has_tools=True, menu=await M.menu(), default=M.workspace_model())
        assert isinstance(got, M.Picked) and got.id.startswith("cli.claude"), got

    async def test_the_menu_knows_which_cli_is_signed_out(self, take2):
        by_id = {e.id: e for e in await M.menu()}
        assert by_id["cli.gemini.gemini-3.1-pro-preview"].sign_in == GEMINI_OUT
        assert by_id["cli.gemini"].sign_in == GEMINI_OUT
        assert by_id["cli.claude.claude-sonnet-5-5"].sign_in is None

    @pytest.mark.parametrize("signed_in", [None, "never probed"])
    async def test_a_sign_in_not_known_is_usable(self, take2, monkeypatch, signed_in):
        if signed_in is None:
            _probed(monkeypatch, "gemini_cli", None)
        else:
            monkeypatch.delitem(cli_status._cache, "gemini_cli")
        assert all(e.sign_in is None for e in await M.menu())


def _entry(id, *, rank_out=None, sign_in=None, provider=None, vision=False):
    return M.ModelEntry(id=id, name=id, provider=provider or ("gemini_cli" if ".gemini" in id else "anthropic_cli"),
                        source="cli", tools="prompt", vision=vision, reasoning=False, price_in=None, price_out=None,
                        can_pay=True, window=1_000_000, rank_out=rank_out, sign_in=sign_in)


FABLE = _entry("cli.claude.claude-fable-5-1", rank_out=50)
HAIKU = _entry("cli.claude.claude-haiku-4-5", rank_out=5)
SONNET = _entry("cli.claude.claude-sonnet-5-5", rank_out=10)
G_PRO = _entry("cli.gemini.gemini-3.1-pro-preview", rank_out=12, sign_in=GEMINI_OUT)
G_FLASH = _entry("cli.gemini.gemini-3-flash-preview", rank_out=3, sign_in=GEMINI_OUT)
MENU = [FABLE, HAIKU, SONNET, G_PRO, G_FLASH]


class TestAPick:
    def test_a_signed_out_cli_is_passed_over(self):
        assert M.pick("simple", has_tools=True, menu=MENU).id == SONNET.id
        assert M.pick("simple", has_tools=True, menu=MENU, narrow=True).id == HAIKU.id

    def test_a_signed_out_default_is_passed_over(self):
        assert M.pick("simple", has_tools=True, menu=MENU, default=G_PRO.id).id == SONNET.id
        assert M.pick("simple", has_tools=True, menu=MENU, default="cli.gemini.gemini-2.5-pro").id == SONNET.id

    def test_a_default_off_the_list_needs_its_cli_on_the_menu(self):
        assert M.pick("simple", has_tools=True, menu=MENU, default="cli.codex.gpt-5").id == SONNET.id
        assert M.pick("simple", has_tools=True, menu=MENU, default=DEFAULT).id == DEFAULT

    def test_a_default_off_the_list_fits_as_any_model(self):
        """Claude Code reads no images: a default of its own is passed over for a job that does."""
        seeing = _entry("cli.gemini.gemini-3-pro-preview", vision=True)
        assert M.pick("vision", has_tools=False, menu=[*MENU, seeing], default=DEFAULT).id == seeing.id

    def test_only_signed_out_clis_say_how_to_sign_in(self):
        assert M.pick("simple", has_tools=True, menu=[G_PRO, G_FLASH]) == M.NoModel(GEMINI_OUT)
        claude_out = _entry("cli.claude.claude-haiku-4-5", sign_in=CLAUDE_OUT)
        assert M.pick("simple", has_tools=True, menu=[G_PRO, claude_out]) == M.NoModel(f"{CLAUDE_OUT} {GEMINI_OUT}")


class TestAWish:
    @pytest.mark.parametrize("wish", ["use Gemini", "use the gemini cli", "cli.gemini.gemini-3.1-pro-preview",
                                      "gemini-3.1-pro-preview"])
    def test_a_signed_out_cli_asked_for_is_said(self, wish):
        assert M.resolve_wish(wish, "simple", has_tools=True, menu=MENU) == M.NoModel(GEMINI_OUT)

    def test_a_rule_passes_it_over(self):
        assert M.resolve_wish("the cheapest one", "simple", has_tools=True, menu=MENU).id == HAIKU.id
        assert M.resolve_wish("the strongest model", "simple", has_tools=True, menu=MENU).id == FABLE.id
        assert M.resolve_wish("use a cli model", "simple", has_tools=True, menu=MENU).id == HAIKU.id

    def test_a_rule_with_only_signed_out_clis_says_how_to_sign_in(self):
        assert M.resolve_wish("the strongest model", "simple", has_tools=True, menu=[G_PRO, G_FLASH]) == M.NoModel(
            GEMINI_OUT)


@pytest.fixture(autouse=True)
def _clean():
    yield
    model_choices.set_model_choices(None)
    M.set_model_facts(None)
