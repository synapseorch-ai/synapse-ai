"""Connect › AI tools points every client at the one MCP door (GUIDE1, D318, F915).

Naveen: "we should have a separate mcp section menu that says connect to claude, codex, cursor with mcp
configuration setting". The page is its own rail item, and what it tells people to paste must reach the endpoint the
backend really serves (`core.mcp_server.endpoint.MCP_PATH`) or `synapse mcp` (CLI1's relay), never a new door. The
screen (`components/connect/`) is synced into cloud's web.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.mcp_server.endpoint import MCP_PATH

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
CONNECT = FRONTEND / "components" / "connect"


def test_the_page_is_its_own_rail_item():
    nav = (FRONTEND / "lib" / "nav.ts").read_text("utf-8")
    connect = nav[nav.index("group: 'Connect'"):]
    assert re.search(r"id: 'ai_tools', label: 'AI tools', href: '/ai-tools'", connect)
    assert (FRONTEND / "app" / "(app)" / "ai-tools" / "page.tsx").exists()


def test_the_address_is_the_one_the_backend_serves():
    screen = (FRONTEND / "components" / "app" / "AiToolsScreen.tsx").read_text("utf-8")
    assert MCP_PATH == "/api/v1/mcp"
    assert f"${{origin}}{MCP_PATH}" in screen
    assert "/api/settings/api-keys" in screen, "keys are made where Settings › API keys makes them"


def test_every_tool_named_has_a_snippet():
    clients = (CONNECT / "clients.ts").read_text("utf-8")
    ids = re.findall(r"\{ id: '([a-z_]+)', label: '([^']+)'", clients) + re.findall(
        r"id: '([a-z_]+)', label: '([^']+)', logo:", clients
    )
    labels = {label for _, label in ids}
    assert {"Claude Code", "Claude Desktop", "Codex", "Cursor", "VS Code"} <= labels
    switch = clients[clients.index("export function snippetFor"):]
    for client_id in ("claude_code", "claude_desktop", "codex", "cursor", "vscode"):
        assert f"case '{client_id}':" in switch, client_id
    # The stdio client starts CLI1's relay, nothing else.
    assert "command: 'synapse', args: ['mcp']" in clients
