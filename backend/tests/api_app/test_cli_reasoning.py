"""Which models an agent's reasoning effort reaches — the Claude CLI included.

`/api/models` serves `reasoning_capable`, and the agent editor shows its
reasoning control for exactly those models: the list is served rather than
restated in TypeScript. Since F65 `cli.claude` passes the agent's effort as
`--effort`, so a Claude CLI model belongs on it. The other CLIs take no effort
at all. And the Claude CLI has no "off" — the engine runs off as low — so the
route also says which models have no off, rather than the screen offering an
Off that quietly means Low.
"""
from __future__ import annotations

import shutil

import pytest

from _fakes.fake_http import FakeResponse, install_httpx


@pytest.fixture
def two_clis(monkeypatch):
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {})
    installed = {"claude": "/usr/bin/claude", "gemini": "/usr/bin/gemini"}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: installed.get(name))
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse({}, status_code=404))


async def test_the_claude_cli_takes_an_effort(client, two_clis):
    body = (await client.get("/api/models")).json()

    claude = [m for m in body["all_available"] if m.startswith("cli.claude")]
    gemini = [m for m in body["all_available"] if m.startswith("cli.gemini")]
    assert claude and gemini, "the fixture should have listed both CLIs"

    assert set(claude) <= set(body["reasoning_capable"])
    assert not set(gemini) & set(body["reasoning_capable"])


async def test_the_claude_cli_has_no_off(client, two_clis):
    body = (await client.get("/api/models")).json()

    claude = {m for m in body["all_available"] if m.startswith("cli.claude")}
    assert set(body["reasoning_without_off"]) == claude
