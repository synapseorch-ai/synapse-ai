"""`/api/models` lists each model CLI's own models and says how it stands (CLIM1, D299, F749, F751).

Settings → Models shows a CLI's card from this: installed or not, signed in or
how to sign in, and its models with where they came from. A typed list still
comes first: it is the override the card has always had.
"""
from __future__ import annotations

import pytest

import core.providers.cli_status as cli_status
from core.providers.cli_status import CliStatus


@pytest.fixture
def clis(monkeypatch):
    import core.routes.data as data

    settings = {"codex_cli_models": "my-model, gpt-5.5"}
    monkeypatch.setattr(data, "load_settings", lambda: settings)
    asked = []

    async def fake_status(provider, refresh=False):
        asked.append((provider, refresh))
        base = cli_status.PROVIDERS[provider]
        if provider == "github_copilot_cli":
            return CliStatus(provider, base, "Copilot CLI", installed=False)
        if provider == "gemini_cli":
            return CliStatus(provider, base, "Gemini CLI", installed=True, version="0.38.0", signed_in=False,
                             sign_in_hint="Run `gemini`…", models=["auto", "gemini-2.5-pro"],
                             models_source="list", models_note="Synapse's list for Gemini CLI 0.38")
        if provider == "codex_cli":
            return CliStatus(provider, base, "Codex", installed=True, version="0.137.0", signed_in=True,
                             models=["gpt-5.5", "gpt-5.4"], models_source="cli", models_note="From Codex")
        return CliStatus(provider, base, "Claude Code", installed=True, version="2.1.291", signed_in=True,
                         models=["opus", "claude-opus-5-5"], models_source="list")

    monkeypatch.setattr(cli_status, "cli_status", fake_status)
    return asked


async def test_each_cli_lists_typed_names_then_its_own_then_its_default(client, clis):
    body = (await client.get("/api/models")).json()
    codex = body["providers"]["codex_cli"]
    assert codex["models"] == ["cli.codex.my-model", "cli.codex.gpt-5.5", "cli.codex.gpt-5.4", "cli.codex"]
    assert body["providers"]["anthropic_cli"]["models"] == ["cli.claude.opus", "cli.claude.claude-opus-5-5",
                                                           "cli.claude"]


async def test_each_cli_says_how_it_stands(client, clis):
    providers = (await client.get("/api/models")).json()["providers"]
    gemini = providers["gemini_cli"]
    assert gemini["available"] is True
    assert gemini["cli"] == {"version": "0.38.0", "signed_in": False, "sign_in_hint": "Run `gemini`…",
                             "models_source": "list", "models_note": "Synapse's list for Gemini CLI 0.38",
                             "note": None}
    assert providers["codex_cli"]["cli"]["models_source"] == "cli"
    copilot = providers["github_copilot_cli"]
    assert copilot["available"] is False and copilot["models"] == []


async def test_refresh_asks_the_clis_again(client, clis):
    await client.get("/api/models")
    assert {r for _, r in clis} == {False}
    clis.clear()
    await client.get("/api/models?refresh=1")
    assert len(clis) == 4 and {r for _, r in clis} == {True}
