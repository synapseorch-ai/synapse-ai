"""A public, no-auth remote MCP server is connected when it is added (U61).

Adding DeepWiki or Context7 took the full 15 s and answered `connected: false`,
"Config saved. Use Retry to reconnect." — while the saved config already said
`status: connected`, and the log said "OAuth complete". With no token the add
starts the OAuth flow in the background and waits for the authorisation URL;
the SDK only asks for one when the server answers 401, and a public server
never does. So the flow simply connected, and never said so to the add, which
waited out its 15 s for a URL that was never coming.

Now the flow reports how it ended — a URL to authorise at, connected, or why it
failed — and the add answers as soon as it knows. A server that could not be
reached is still saved and listed, and the answer says why (it was a 400 over a
config already saved, which the screen then refused to add again).
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest


@pytest.fixture
async def public_mcp():
    """A real streamable-HTTP MCP server that needs no auth, like DeepWiki."""
    from mcp.server.fastmcp import FastMCP
    from _fakes.live_server import serve

    server = FastMCP("public-docs", stateless_http=True)

    @server.tool()
    def ask_docs(question: str) -> str:
        return "The docs say hi."

    app = server.streamable_http_app()
    # In a task of its own: its session manager's task group is entered and
    # left in one task, which a fixture's setup and teardown are not.
    ready: asyncio.Future = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def _run():
        async with server.session_manager.run():
            async with serve(app) as live:
                ready.set_result(live)
                await stop.wait()

    task = asyncio.create_task(_run())
    live = await asyncio.wait_for(ready, 10)
    try:
        yield live
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


@pytest.fixture
async def manager(test_app, monkeypatch):
    import core.server as server
    from core.mcp_client import MCPClientManager
    stack = AsyncExitStack()
    mgr = MCPClientManager(stack)
    monkeypatch.setattr(server, "mcp_manager", mgr)
    try:
        yield mgr
    finally:
        await stack.aclose()


class TestAddingAPublicServer:
    async def test_it_answers_connected_as_soon_as_it_is(self, live_server, public_mcp, manager):
        async with live_server.client() as c:
            added = await asyncio.wait_for(c.post("/api/mcp/servers", json={
                "name": "docs", "label": "Docs", "server_type": "remote", "url": f"{public_mcp.url}/mcp",
            }), 8)
        assert added.status_code == 200, added.text
        body = added.json()
        assert body["connected"] is True and body["status"] == "success", body
        assert body["config"]["status"] == "connected", "one answer, not two"
        assert "Retry" not in body["message"]

    async def test_a_server_that_cannot_be_reached_is_saved_and_says_why(self, live_server, manager):
        async with live_server.client() as c:
            added = await asyncio.wait_for(c.post("/api/mcp/servers", json={
                "name": "gone", "label": "Gone", "server_type": "remote", "url": "http://127.0.0.1:9/mcp",
            }), 20)
            assert added.status_code == 200, added.text
            body = added.json()
            assert body["connected"] is False and body["status"] == "saved"
            assert body["config"]["status"] != "connected"
            assert "did not connect" in body["message"]
            listed = (await c.get("/api/mcp/servers")).json()
        names = [s["name"] for s in (listed.get("servers") if isinstance(listed, dict) else listed)]
        assert "gone" in names, "saved, so listed, so it can be retried or removed"


class TestAServerThatAsksForOAuth:
    async def test_it_still_hands_back_the_url(self, manager, monkeypatch):
        import core.mcp_client as mc
        handlers = {}
        done = asyncio.Event()

        def provider(name, url, *, redirect_handler, callback_handler):
            import httpx
            handlers["redirect"] = redirect_handler
            return httpx.Auth()

        async def hold(self, name, opener):
            await handlers["redirect"]("https://auth.test/authorize?state=s_u61")
            await done.wait()
            raise RuntimeError("never authorised")

        monkeypatch.setattr(mc, "_make_oauth_provider", provider)
        monkeypatch.setattr(mc, "_http_opener", lambda url, client: None)
        monkeypatch.setattr(mc.MCPClientManager, "_hold", hold)
        try:
            result = await asyncio.wait_for(manager.add_server(
                name="oa", server_type="remote", url="https://oauth.test/mcp"), 5)
        finally:
            done.set()
            # The flow ends in the background, writing its status: let it,
            # before this test's loop and store go away.
            from core import background
            flows = [t for t in background._running if t.get_name() == "mcp-oauth-connect"]
            await asyncio.wait_for(asyncio.gather(*flows, return_exceptions=True), 5)
        assert result["status"] == "oauth_pending"
        assert result["auth_url"].startswith("https://auth.test/authorize")
        assert result["connected"] is False
