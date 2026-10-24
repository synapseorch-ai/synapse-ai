"""Connecting AI tools reaches the one MCP door, from Home's card and from Settings › MCP server (GUIDE1, D318).

F915: "connect to claude, codex, cursor with mcp configuration setting". F1027, Naveen on the first version: "I don't
want new ai tools menu... Let's have a home page card... a button that will just get the things done of connecting...
a button that takes to an MCP server page in settings". So the page is a Settings entry, its old address redirects
there, every option it had is still on it (the same `ConnectAiTools`), and Home's card connects in one click where a
tool allows. Whatever it hands a tool reaches the endpoint the backend serves (`MCP_PATH`) or `synapse mcp` (CLI1's
relay), never a new door. `components/connect/` is synced into cloud's web.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.mcp_server.endpoint import MCP_PATH

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
CONNECT = FRONTEND / "components" / "connect"


def test_the_page_is_a_settings_entry_and_no_rail_item():
    nav = (FRONTEND / "lib" / "nav.ts").read_text("utf-8")
    hubs = nav[nav.index("export const HUB_NAV"):nav.index("export const SETTINGS_NAV")]
    settings = nav[nav.index("export const SETTINGS_NAV"):]
    assert "ai_tools" not in hubs, "the rail has no AI tools item (Naveen, F1027)"
    assert re.search(r"id: 'mcp_server', label: 'MCP server', href: '/settings/mcp_server'", settings)
    assert not (FRONTEND / "app" / "(app)" / "ai-tools").exists()


def test_the_old_address_goes_to_the_new_page():
    moved = (FRONTEND / "lib" / "moved-routes.ts").read_text("utf-8")
    assert "'/ai-tools': '/settings/mcp_server'" in moved


def test_every_option_the_page_had_is_still_on_it():
    tab = (FRONTEND / "components" / "settings" / "McpServerTab.tsx").read_text("utf-8")
    view = (FRONTEND / "components" / "SettingsView.tsx").read_text("utf-8")
    assert "<ConnectAiTools host={host} />" in tab, "Settings › MCP server is the whole step-by-step screen"
    assert "activeTab === 'mcp_server' && <McpServerTab />" in view


def test_the_address_is_the_one_the_backend_serves():
    tab = (FRONTEND / "components" / "settings" / "McpServerTab.tsx").read_text("utf-8")
    assert MCP_PATH == "/api/v1/mcp"
    assert f"${{origin}}{MCP_PATH}" in tab
    assert "/api/settings/api-keys" in tab, "keys are made where Settings › API keys makes them"


def test_every_tool_named_has_a_snippet():
    clients = (CONNECT / "clients.ts").read_text("utf-8")
    labels = set(re.findall(r"id: '[a-z_]+', label: '([^']+)'", clients))
    assert {"Claude Code", "Claude Desktop", "Codex", "Cursor", "VS Code"} <= labels
    switch = clients[clients.index("export function snippetFor"):]
    for client_id in ("claude_code", "claude_desktop", "codex", "cursor", "vscode"):
        assert f"case '{client_id}':" in switch, client_id
    # The stdio client starts CLI1's relay, nothing else.
    assert "command: 'synapse', args: ['mcp']" in clients


def test_home_connects_in_one_click_where_a_tool_allows():
    clients = (CONNECT / "clients.ts").read_text("utf-8")
    card = (CONNECT / "ConnectCard.tsx").read_text("utf-8")
    home = (FRONTEND / "components" / "app" / "Dashboard.tsx").read_text("utf-8")
    assert "cursor://anysphere.cursor-deeplink/mcp/install?name=" in clients and "&config=" in clients
    assert "vscode:mcp/install?" in clients
    assert 'settingsHref="/settings/mcp_server"' in home
    assert "<ConnectCard" in home
    assert "installLink(" in card and "CLAUDE_CONNECTORS" in card


def test_learn_synapse_shows_only_when_the_docs_answer():
    """F1030: Home's "Learn Synapse with your AI assistant" adds the docs' public MCP server. No workspace may meet a
    dead link before DOC5 publishes them, so the card shows only when that server answers, asked on the server."""
    probe = (CONNECT / "docsMcp.ts").read_text("utf-8")
    page = (FRONTEND / "app" / "(app)" / "dashboard" / "page.tsx").read_text("utf-8")
    home = (FRONTEND / "components" / "app" / "Dashboard.tsx").read_text("utf-8")
    clients = (CONNECT / "clients.ts").read_text("utf-8")
    assert "DOCS_MCP_URL = `${DOCS_BASE}/api/mcp`" in clients
    assert "res.status === 405" in probe and "return false" in probe, "404 or no answer is not yet"
    assert "docsMcp={await docsMcpAnswers()}" in page
    assert "{docsMcp && showStart && learnHidden !== '1' && (" in home, "a new workspace's, once the docs answer"


def test_home_s_one_click_keys_are_least_named_and_never_pile_up():
    """F1027, the manager's points: the install link carries the key into the app (browser history, the OS handler's
    log), so the key Home makes is the least a client needs, named for the client and where it came from, said on the
    card with where to remove it; and a repeat click replaces that client's unused Home key rather than adding one."""
    clients = (CONNECT / "clients.ts").read_text("utf-8")
    card = (CONNECT / "ConnectCard.tsx").read_text("utf-8")
    assert "export const homeKeyName = (client: string) => `${client}, from Home`;" in clients
    assert "k.name === name && !k.last_used_at" in clients, "only unused Home keys of that client are replaced"
    assert "unusedHomeKeys(await host.listKeys(), label)" in card and "await host.revokeKey(id)" in card
    assert "host.makeKey(homeKeyName(label), false)" in card, "run-only where a product has the choice"
    assert "Remove it any time in" in card and "host.keysHref" in card
