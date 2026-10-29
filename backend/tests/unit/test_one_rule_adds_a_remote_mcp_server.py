"""One rule for a remote MCP server's URL, at every door that adds one (F1201, BLD1; CAP3's F-f).

The Apps screen (`POST /api/mcp/servers`, through `MCPClientManager.add_server`) saved any URL it was given; the
builder's Connect card refused all but https, so "connect my tracker at http://…" could be done by hand and never
by the builder. Both now ask `core.mcp_client.remote_url_problem`: http or https, with a host. A server on this
machine or network stays the person's to add on the Apps screen (the card's own refusal, in its tests). Checked on
save only: a server saved before keeps loading as it is.
"""
from __future__ import annotations

import pytest

from core.mcp_client import MCPClientManager, remote_url_problem


@pytest.mark.parametrize("url", ["https://mcp.linear.app/mcp", "http://tracker.example.com:9416/mcp",
                                 "http://localhost:9415/mcp", "http://10.0.0.5/mcp"])
def test_http_and_https_with_a_host_pass(url):
    assert remote_url_problem(url) is None


@pytest.mark.parametrize("url,words", [
    ("", "URL is required for remote servers."),
    ("mcp.example.com/mcp", "starts with http:// or https://"),
    ("ws://mcp.example.com/mcp", "starts with http:// or https://"),
    ("http:///mcp", "names its host"),
])
def test_anything_else_is_refused(url, words):
    assert words in (remote_url_problem(url) or "")


@pytest.fixture
def manager(monkeypatch):
    m = MCPClientManager.__new__(MCPClientManager)
    m.servers_config = []
    saved = []

    async def save_servers():
        saved.append([dict(s) for s in m.servers_config])

    monkeypatch.setattr(m, "save_servers", save_servers, raising=False)
    m.saved = saved
    return m


async def test_the_apps_screen_refuses_what_the_card_does(manager):
    with pytest.raises(ValueError, match="starts with http:// or https://"):
        await manager.add_server(name="tracker", server_type="remote", url="mcp.example.com/mcp")
    assert manager.servers_config == [] and manager.saved == []


async def test_a_hosts_rule_is_asked_at_both_doors(manager):
    """Cloud's seam (8a 10-29): its egress check is the rule at the Apps screen and the Connect card alike."""
    import json

    from core import mcp_client
    from core.builder_tools import execute_builder_tool_with_events

    asked = []

    async def egress(url):
        asked.append(url)
        return "That address is inside a private network, which this workspace can't reach." if "10.0.0" in url else None

    mcp_client.set_url_rule(egress)
    try:
        with pytest.raises(ValueError, match="private network"):
            await manager.add_server(name="inner", server_type="remote", url="https://10.0.0.9/mcp")
        raw, events = await execute_builder_tool_with_events("prepare_connection", {
            "kind": "mcp_server", "title": "Connect it", "url": "https://10.0.0.9/mcp", "server_name": "Inner"},
            server_module=None)
        assert "private network" in json.loads(raw)["error"] and events == []
        assert await mcp_client.remote_url_refusal("https://mcp.example.com/mcp") is None
    finally:
        mcp_client.set_url_rule(None)
    assert asked == ["https://10.0.0.9/mcp", "https://10.0.0.9/mcp", "https://mcp.example.com/mcp"]
    assert manager.servers_config == []
