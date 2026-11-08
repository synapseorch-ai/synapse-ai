"""
What the MCP endpoint says to a client newer than it (F175).

The endpoint speaks MCP up to 2025-11-25 (the pinned 1.x SDK). A 2026-07-28
client opens with a `server/discover` probe and falls back to `initialize` on an
error — measured with mcp 2.2.0's own `Client` (D134): 400, then initialize
negotiates 2025-11-25, and tools list and call. These pin both halves, so an
SDK bump that changes either is seen.
"""
from __future__ import annotations

from tests.api_app.test_mcp_server import URL, _MCP_HEADERS, _http, mcp_app  # noqa: F401 — fixture

_DISCOVER = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
_INITIALIZE_NEW = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
    "protocolVersion": "2026-07-28", "capabilities": {},
    "clientInfo": {"name": "modern", "version": "0"}}}


async def test_a_newer_clients_probe_is_refused_and_initialize_negotiates_down(mcp_app, api_key):
    async with _http(mcp_app, api_key["headers"]) as http:
        probe = await http.post(URL, json=_DISCOVER,
                                headers={**_MCP_HEADERS, "mcp-protocol-version": "2026-07-28"})
        assert probe.status_code == 400, probe.text
        assert "Unsupported protocol version" in probe.text

        init = await http.post(URL, json=_INITIALIZE_NEW, headers=_MCP_HEADERS)
        assert init.status_code == 200, init.text
        assert init.json()["result"]["protocolVersion"] == "2025-11-25"
