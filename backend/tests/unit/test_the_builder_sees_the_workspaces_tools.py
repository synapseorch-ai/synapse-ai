"""The builder's tool discovery reads the workspace's catalogue, never one agent's list (F866, SAI2, D300).

Found by QA1c on production (Kestrel): `list_all_tools` and `get_tools_detail` filtered the catalogue through
`active_agent = next(non-system agents)`, the workspace's first agent. Kestrel's first agent holds no tools, so the
builder said the workspace's `rotterdam_weather` "does not exist" and planned around it. Which tools a workspace has
is a fact about the workspace; which one agent may call is a different fact. `builder_guard`'s missing-tool check
already asks the workspace (`{"tools": ["all"]}`); discovery now asks the same way.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool

WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}}


class _Server(types.SimpleNamespace):
    """`server_module` as the dispatcher uses it: a bag of MCP sessions (none here)."""

    def __init__(self):
        super().__init__(agent_sessions={})


@pytest.fixture
def server():
    return _Server()


async def call(tool: str, args: dict, server) -> dict | list:
    return json.loads(await execute_builder_tool(tool, args, server))


@pytest.fixture
async def kestrel():
    """The first agent holds no tools; a later one holds the workspace's custom tool."""
    from core.store.resources import save_tool
    await save_tool(dict(WEATHER))
    await S.seed_agents([
        S.make_agent(id="agent_first", name="Greeter", tools=[]),
        S.make_agent(id="agent_weather", name="Weather desk", tools=["rotterdam_weather"]),
    ])


async def test_list_all_tools_sees_a_tool_the_first_agent_does_not_hold(server, kestrel):
    result = await call("list_all_tools", {"limit": 500}, server)
    assert isinstance(result, list)
    assert "rotterdam_weather" in {t["name"] for t in result}


async def test_get_tools_detail_finds_a_tool_the_first_agent_does_not_hold(server, kestrel):
    result = await call("get_tools_detail", {"tool_names": ["rotterdam_weather"]}, server)
    assert "rotterdam_weather" in result, result
    assert not result.get("_not_found")


async def test_a_workspace_with_no_agents_still_lists_its_tools(server):
    from core.store.resources import save_tool
    await save_tool(dict(WEATHER))
    result = await call("list_all_tools", {"limit": 500}, server)
    assert "rotterdam_weather" in {t["name"] for t in result}
