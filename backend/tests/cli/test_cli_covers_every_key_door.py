"""The CLI reaches every door an API key has (CLI1, D294; Naveen 2026-10-16: "make it work just like the api keys").

Each key route under /api/v1 and /api/v2, and each product tool an MCP client may call, is named in
`synapse/client/coverage.py` with the command that goes through it, or with the reason it has none. A route or a
tool added without its line fails here, so the CLI never falls behind the key quietly.
"""
from __future__ import annotations

import argparse

import pytest

from synapse.client import coverage


def _key_routes(test_app) -> set[tuple[str, str]]:
    """Every method and path under /api/v1 and /api/v2 the app's schema lists (on FastAPI ≥ 0.139 `app.routes`
    holds routers, not routes — F166)."""
    paths = test_app.openapi()["paths"]
    return {(m.upper(), p) for p, ms in paths.items() if p.startswith(("/api/v1/", "/api/v2/")) for m in ms}


def _offered_tools() -> set[str]:
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    return {t.name for t in registry.all_tools() if not t.app_only}


def _parser() -> argparse.ArgumentParser:
    from synapse.client import commands

    parser = argparse.ArgumentParser(prog="synapse")
    commands.register(parser.add_subparsers(dest="command"))
    return parser


def _parses(parser: argparse.ArgumentParser, command: str) -> bool:
    """Whether `synapse <command>` names a command (its own `--help` would print, not an unknown choice)."""
    action = parser
    for word in command.split():
        sub = next((a for a in action._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None or word not in sub.choices:
            return False
        action = sub.choices[word]
    return not any(isinstance(a, argparse._SubParsersAction) and a.required for a in action._actions)


class TestEveryDoorIsNamed:
    def test_every_key_route(self, test_app):
        named = set(coverage.ROUTES) | {tuple(k.split(" ", 1)) for k in coverage.NOT_BY_THE_CLI if " /api/" in k}
        missing = sorted(f"{m} {p}" for m, p in _key_routes(test_app) - named)
        assert not missing, f"key routes with no CLI command and no reason in coverage.py: {missing}"

    def test_every_tool_a_key_may_call(self):
        named = set(coverage.TOOLS) | {k for k in coverage.NOT_BY_THE_CLI if " " not in k}
        missing = sorted(_offered_tools() - named)
        assert not missing, f"product tools with no CLI command and no reason in coverage.py: {missing}"

    def test_nothing_named_is_gone(self, test_app):
        """A line for a route or tool that no longer exists is a promise the CLI does not keep."""
        assert not set(coverage.ROUTES) - _key_routes(test_app)
        assert not set(coverage.TOOLS) - _offered_tools()

    @pytest.mark.parametrize("command", sorted(set(coverage.ROUTES.values()) | set(coverage.TOOLS.values())))
    def test_each_command_named_exists(self, command):
        assert _parses(_parser(), command), f"`synapse {command}` is not a command"
