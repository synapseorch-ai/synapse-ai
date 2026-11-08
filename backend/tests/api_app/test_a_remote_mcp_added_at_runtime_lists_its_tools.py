"""
A remote MCP server added while the backend runs lists its tools (F200).

The pass added this install's own `/api/v1/mcp` under MCP Servers: "Server
connected and saved", ACTIVE — and none of its tools ever appeared, every
listing logging `Error listing tools for agent 'ext_mcp_synapse-self':` with no
message. Measured by B2 (D134): a FastMCP remote added the same way failed the
same, so it is not the self-connection. `add_server` opened the transport — an
anyio task group — inside the request's task and parked it on the lifespan's
exit stack. When the request returned, Starlette's middleware left its own scope
while the transport's was still open ("Attempted to exit a cancel scope that
isn't the current tasks's current cancel scope"), and the transport's tasks
went with it. A server connected at startup (in the lifespan's task) listed
fine, so a restart hid it.

So this runs where it broke: the real app behind a real socket, the add made
through the whole middleware stack, and the tools listed by a later request.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest


@pytest.fixture
async def running(test_app, live_server, monkeypatch):
    """The app's MCP endpoint served, and a client manager like the lifespan's."""
    import core.server as server
    from core.mcp_client import MCPClientManager
    from core.mcp_server.endpoint import default_endpoint

    endpoint = default_endpoint()
    started, stop = asyncio.Event(), asyncio.Event()

    async def _serve():
        async with endpoint.serve():
            started.set()
            await stop.wait()

    serving = asyncio.create_task(_serve())
    await asyncio.wait_for(started.wait(), 5)
    stack = AsyncExitStack()
    manager = MCPClientManager(stack)
    monkeypatch.setattr(server, "mcp_manager", manager)
    try:
        yield live_server, manager
    finally:
        await stack.aclose()
        stop.set()
        await asyncio.wait_for(serving, 5)


async def _names(client) -> list[str]:
    tools = (await client.get("/api/tools/available")).json()["tools"]
    return [t["name"] for t in tools if t.get("source") == "self"]


async def test_its_tools_are_listed_after_the_add_returns(running, api_key):
    live, manager = running
    async with live.client() as c:
        added = await c.post("/api/mcp/servers", json={
            "name": "self", "label": "Self", "server_type": "remote",
            "url": f"{live.url}/api/v1/mcp", "token": api_key["raw"],
        })
        assert added.status_code == 200, added.text
        assert added.json()["connected"] is True

    async with live.client() as c:
        names = await _names(c)
    assert "self__list_agents" in names, names
    assert "self__run_orchestration" in names, names


async def test_a_retry_replaces_the_session_and_it_still_lists(running, api_key):
    live, manager = running
    async with live.client() as c:
        await c.post("/api/mcp/servers", json={
            "name": "self", "label": "Self", "server_type": "remote",
            "url": f"{live.url}/api/v1/mcp", "token": api_key["raw"],
        })
        first = manager.sessions["self"]
        retried = await c.post("/api/mcp/servers/self/reconnect")
        assert retried.status_code == 200, retried.text
    assert manager.sessions["self"] is not first
    async with live.client() as c:
        assert "self__list_agents" in await _names(c)


async def test_removing_it_closes_its_session(running, api_key):
    live, manager = running
    async with live.client() as c:
        await c.post("/api/mcp/servers", json={
            "name": "self", "label": "Self", "server_type": "remote",
            "url": f"{live.url}/api/v1/mcp", "token": api_key["raw"],
        })
        session = manager.sessions["self"]
        assert (await c.delete("/api/mcp/servers/self")).status_code == 200
    assert "self" not in manager.sessions
    with pytest.raises(Exception):
        await asyncio.wait_for(session.list_tools(), 5)
