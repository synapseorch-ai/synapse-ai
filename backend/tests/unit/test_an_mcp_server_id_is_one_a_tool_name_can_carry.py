"""An MCP server's short id holds to the rule its tools' names must, and one saved before the rule can still be removed
and retried — F1460 (ISS1, GitHub #377).

#377, 2.0.0-rc2: `POST /api/mcp/servers {"name": "github/prod", …}` answered "Server connected and saved.", and then
Remove (`DELETE /api/mcp/servers/github/prod`, or `github%2Fprod`) and Retry (`…/github/prod/reconnect`) answered 404:
the routes' `{name}` never matches a slash. The server stayed in the list for good. Spaces and an empty id were
accepted too, and the tools were named `github/prod__two_texts`, `my server/x__two_texts`, `__two_texts` — names
Claude and OpenAI refuse (`^[a-zA-Z0-9_-]{1,64}$`; `product_tools/registry.py`). The form said "Short id (no
spaces)" and checked nothing.

Now the id is letters, digits, `-` and `_`, 1 to 32 of them (room for `__<tool>`), on the request and in the
manager. The two routes take the id whole (`{name:path}`) and the page encodes it, so a server saved before the rule
is removed and retried like any other. Its label stays free text.
"""
from __future__ import annotations

import re
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from core.mcp_client import MCPClientManager

FRONTEND = Path(__file__).resolve().parents[3] / "frontend/src"
URL = "http://127.0.0.1:9/mcp"


@pytest.fixture
async def manager(monkeypatch):
    import core.server as server
    from core import mcp_client

    async def no_refusal(url):
        return None

    def opener_for(url, http_client):
        async def opener(stack):
            await http_client.aclose()
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionRefusedError(111, "refused")])
        return opener

    stack = AsyncExitStack()
    mgr = MCPClientManager(stack)
    monkeypatch.setattr(server, "mcp_manager", mgr)
    monkeypatch.setattr(mcp_client, "remote_url_refusal", no_refusal)
    monkeypatch.setattr(mcp_client, "_http_opener", opener_for)  # nothing is dialled
    try:
        yield mgr
    finally:
        await stack.aclose()


def _remote(name: str) -> dict:
    return {"name": name, "label": "Some server", "server_type": "remote", "url": URL, "token": "t0k3n-for-test"}


class TestTheAddHoldsToTheRule:
    @pytest.mark.parametrize("name", ["github/prod", "my server", "", "a" * 33, "dots.not.allowed"])
    async def test_an_id_a_tool_name_cannot_carry_is_refused_and_nothing_is_saved(self, client, manager, name):
        resp = await client.post("/api/mcp/servers", json=_remote(name))

        assert resp.status_code == 422, resp.text
        assert manager.servers_config == [], manager.servers_config

    @pytest.mark.parametrize("name", ["github-prod", "gh_prod", "A1", "a" * 32])
    async def test_an_id_that_holds_to_it_is_saved(self, client, manager, name):
        resp = await client.post("/api/mcp/servers", json=_remote(name))

        assert resp.status_code == 200, resp.text
        assert [s["name"] for s in manager.servers_config] == [name]

    async def test_the_refusal_says_the_rule(self, client, manager):
        resp = await client.post("/api/mcp/servers", json=_remote("github/prod"))

        said = str(resp.json()["detail"])
        assert "letters, digits" in said, said

    async def test_the_label_stays_free_text(self, client, manager):
        resp = await client.post("/api/mcp/servers", json={**_remote("github-prod"), "label": "GitHub / Prod (EU)"})
        assert resp.status_code == 200, resp.text

    async def test_the_manager_refuses_it_too(self, manager):
        """Any later door that adds a server goes through the manager."""
        with pytest.raises(ValueError, match="letters, digits"):
            await manager.add_server(**{k: v for k, v in _remote("github/prod").items()})
        assert manager.servers_config == []


class TestOneSavedBeforeTheRule:
    @pytest.fixture
    async def slashed(self, manager):
        manager.servers_config.append({**_remote("github/prod"), "status": "disconnected"})
        await manager.save_servers()
        return manager

    @pytest.mark.parametrize("path", ["github/prod", "github%2Fprod"])
    async def test_remove_takes_it_away(self, client, slashed, path):
        resp = await client.delete(f"/api/mcp/servers/{path}")

        assert resp.status_code == 200, resp.text
        assert [s["name"] for s in slashed.servers_config] == []

    async def test_retry_reaches_it(self, client, slashed):
        resp = await client.post("/api/mcp/servers/github/prod/reconnect")

        assert resp.status_code != 404, resp.text
        assert resp.json().get("connected") is False, resp.text


class TestThePage:
    def test_remove_and_retry_encode_the_id(self):
        screen = (FRONTEND / "components/settings/McpServersScreen.tsx").read_text()

        raw = re.findall(r"/api/mcp/servers/\$\{(?!encodeURIComponent\()[^}]*\}", screen)
        assert raw == [], raw
        assert screen.count("/api/mcp/servers/${encodeURIComponent(") >= 2
