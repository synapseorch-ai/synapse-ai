"""The AI builder's Connect card (BWA1, D274): a key, a token or a sign-in goes to a secure screen, never the chat.

`prepare_connection` never connects anything and never sees a secret. These pin what it checks before a card goes
up, and that the card is an event the panel receives:

- an app must be in this deployment's Apps catalog (a host's, through `set_connect_options`; self-hosted has none);
- a remote MCP server must be https and outward (the builder's own egress refusal);
- a provider key, a custom tool's header and a settings screen must be ones that exist;
- the card is a `connection_needed` event, and the model is told the card is where the connection happens;
- tools in their own modules join the builder through `register_builder_tool`, without editing builder_tools.py.
"""
from __future__ import annotations

import json

import pytest

from core import builder_connect
from core.builder_tools import BUILDER_TOOL_NAMES, execute_builder_tool_with_events, register_builder_tool


async def _call(**args) -> tuple[dict, list[dict]]:
    raw, events = await execute_builder_tool_with_events("prepare_connection", args, server_module=None)
    return json.loads(raw), events


@pytest.fixture
def catalog():
    async def options():
        return {"catalog": [{"slug": "notion", "name": "Notion", "auth": "oauth", "url": "https://mcp.notion.com/mcp"},
                            {"slug": "github", "name": "GitHub", "auth": "bearer", "token_hint": "A personal access token"}]}
    builder_connect.set_connect_options(options)
    yield
    builder_connect.set_connect_options(None)


def test_the_tool_joins_the_builder_through_the_registry():
    assert "prepare_connection" in BUILDER_TOOL_NAMES


def test_a_registered_tool_cannot_take_an_existing_tools_name():
    schema = {"type": "function", "function": {"name": "delete_agent", "parameters": {"type": "object"}}}
    with pytest.raises(ValueError):
        register_builder_tool(schema, lambda *a: None)


async def test_an_app_from_the_catalog_becomes_a_card(catalog):
    result, events = await _call(kind="app", title="Connect Notion", why="To read the clinic wiki.",
                                 catalog_slug="notion")
    assert result["status"] == "card_shown" and "Do not ask for the key" in result["say"]
    (card,) = events
    assert card["type"] == "connection_needed" and card["kind"] == "app"
    assert card["app"]["slug"] == "notion" and card["app"]["auth"] == "oauth"
    assert card["why"] == "To read the clinic wiki." and card["id"].startswith("conn_")


async def test_an_app_that_is_not_in_the_catalog_is_refused_with_what_is(catalog):
    result, events = await _call(kind="app", title="Connect Slack", catalog_slug="slack")
    assert "not in this workspace's Apps catalog" in result["error"] and "github, notion" in result["error"]
    assert events == []


async def test_self_hosted_has_no_catalog():
    result, _ = await _call(kind="app", title="Connect Notion", catalog_slug="notion")
    assert "error" in result and "kind=mcp_server" in result["error"]


@pytest.mark.parametrize("url,why", [
    ("http://mcp.example.com/mcp", "https"),
    ("https://127.0.0.1/mcp", "inside our own network"),
    ("https://10.0.0.5/mcp", "inside our own network"),
    ("mcp.example.com", "https"),
])
async def test_a_server_must_be_https_and_outward(url, why):
    result, events = await _call(kind="mcp_server", title="Connect it", url=url, server_name="Example")
    assert why in result["error"] and events == []


async def test_a_remote_server_becomes_a_card():
    result, events = await _call(kind="mcp_server", title="Connect Linear", url="https://mcp.linear.app/mcp",
                                 server_name="Linear issues")
    assert result["status"] == "card_shown"
    assert events[0]["server"] == {"name": "linear-issues", "label": "Linear issues",
                                   "url": "https://mcp.linear.app/mcp"}


async def test_a_provider_key_must_be_a_known_provider():
    result, _ = await _call(kind="provider_key", title="Add your key", provider="madeup")
    assert "Unknown provider" in result["error"]
    result, events = await _call(kind="provider_key", title="Add your OpenAI key", provider="OpenAI")
    assert events[0]["provider"] == {"id": "openai", "label": "OpenAI"}


async def test_a_tool_secret_needs_an_existing_tool_and_a_header():
    from core.store.resources import save_tool
    await save_tool({"name": "book_appointment", "description": "Book a slot.", "tool_type": "http",
                     "method": "POST", "url": "https://api.dentalbook.example/v2/appointments", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}})
    result, _ = await _call(kind="tool_secret", title="Add its key", tool_name="no_such_tool", header="X-Api-Key")
    assert "No custom tool" in result["error"]
    result, _ = await _call(kind="tool_secret", title="Add its key", tool_name="book_appointment", header="")
    assert "Name the header" in result["error"]
    result, events = await _call(kind="tool_secret", title="Add its key", tool_name="book_appointment",
                                 header="X-Api-Key")
    assert events[0]["tool"]["name"] == "book_appointment" and events[0]["tool"]["header"] == "X-Api-Key"


async def test_a_settings_link_must_be_a_known_screen():
    result, _ = await _call(kind="settings", title="Open it", screen="nowhere")
    assert "Unknown screen" in result["error"]
    result, events = await _call(kind="settings", title="Connect Slack", screen="channels")
    assert events[0]["screen"]["id"] == "channels"


async def test_an_unknown_kind_is_refused():
    result, events = await _call(kind="password", title="x")
    assert "kind must be one of" in result["error"] and events == []
