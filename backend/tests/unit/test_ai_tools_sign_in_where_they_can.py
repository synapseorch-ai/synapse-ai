"""An AI tool signs in where it can, and shows its maker's own mark (GUIDE1, D318; F1178, F1179).

F1178, Naveen: sign in instead of a token, for every client that can. Claude Code, Codex, Cursor and VS Code all
sign in to a remote MCP server with OAuth from a config that holds only its address (their own docs, 10-29). So on a
product with an OAuth server (cloud, once its connectors are on) each gets a snippet or one-click link with no key in
it, and "Use a key instead" keeps today's path. OSS has no OAuth server: its hosts never turn sign-in on, and every
tool keeps the key. F1179: Cursor's and VS Code's own icons, not letter tiles. `components/connect/` is synced into
cloud's web, so these hold for both products.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
CONNECT = FRONTEND / "src" / "components" / "connect"


def _clients() -> str:
    return (CONNECT / "clients.ts").read_text("utf-8")


def _body(source: str, name: str) -> str:
    start = source.index(f"export function {name}")
    end = source.find("\nexport ", start + 1)
    return source[start:end if end != -1 else None]


def test_the_four_tools_that_can_sign_in_are_named():
    clients = _clients()
    can = re.search(r"export const SIGN_IN_CLIENTS: [^=]+= \[([^\]]*)\]", clients)
    assert can, "SIGN_IN_CLIENTS is gone from clients.ts"
    assert set(re.findall(r"'([a-z_]+)'", can.group(1))) == {"claude_code", "codex", "cursor", "vscode"}


def test_a_sign_in_snippet_holds_no_key():
    body = _body(_clients(), "signInSnippet")
    for leak in ("Authorization", "Bearer", "KEY_PLACEHOLDER", "--header", "bearer-token", "headers"):
        assert leak not in body, f"a sign-in snippet carries {leak!r}"
    assert "claude mcp add --transport http synapse ${url}" in body
    assert "codex mcp add synapse --url ${url}" in body and "codex mcp login synapse" in body
    assert "mcpServers: { synapse: { url } }" in body
    assert "servers: { synapse: { type: 'http', url } }" in body


def test_each_tool_says_how_it_signs_in():
    words = _body(_clients(), "signInWhere")
    assert "/mcp" in words and "Authenticate" in words, "Claude Code signs in from /mcp"
    for client_id in ("claude_code", "codex", "cursor", "vscode"):
        assert f"case '{client_id}':" in words, client_id


def test_both_screens_sign_in_first_and_keep_the_key():
    host = (CONNECT / "ConnectAiTools.tsx").read_text("utf-8")
    card = (CONNECT / "ConnectCard.tsx").read_text("utf-8")
    assert re.search(r"signIn\?: boolean", host), "ConnectHost says whether this product signs in"
    assert "Use a key instead" in host, "the key stays one click away"
    assert "signInSnippet(" in host and "signInSnippet(" in card
    # A one-click link with no key in it: the tool signs in when it opens.
    assert re.search(r"installLink\(c\.id, \{ url: host\.mcpUrl \}\)", card)


def test_the_screens_say_what_signing_in_gives():
    host = (CONNECT / "ConnectAiTools.tsx").read_text("utf-8")
    assert "as you" in host and "your role" in host, "a sign-in acts as the person, by their role"
    assert "connectedAppsHref" in host, "and says where it is removed"


def test_oss_never_signs_in():
    for host in (FRONTEND / "src" / "components" / "settings" / "McpServerTab.tsx",
                 FRONTEND / "src" / "components" / "app" / "Dashboard.tsx"):
        assert "signIn" not in host.read_text("utf-8"), f"{host.name} turns sign-in on; OSS has no OAuth server"


def test_cursor_and_vs_code_show_their_own_marks():
    clients = _clients()
    assert re.search(r"logo\?: 'claude' \| 'openai' \| 'cursor' \| 'vscode'", clients)
    assert re.search(r"id: 'cursor', label: 'Cursor', logo: 'cursor'", clients)
    assert re.search(r"id: 'vscode', label: 'VS Code', logo: 'vscode'", clients)
    for name in ("cursor-icon.svg", "vscode-icon.svg"):
        svg = (FRONTEND / "public" / name).read_text("utf-8")
        assert "<svg" in svg and "<script" not in svg, name
    for host in (FRONTEND / "src" / "components" / "settings" / "McpServerTab.tsx",
                 FRONTEND / "src" / "components" / "app" / "Dashboard.tsx"):
        text = host.read_text("utf-8")
        assert "cursor: '/cursor-icon.svg'" in text and "vscode: '/vscode-icon.svg'" in text, host.name
