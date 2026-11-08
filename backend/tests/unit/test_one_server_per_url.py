"""A server pressed again on its card is the server it saved, not a second one (F1290, CAP3's cap-03; 79 11-02).

The Connect card's press adds the server (`POST /api/mcp/servers`, `MCPClientManager.add_server`). The first press of
cap-03's A1 saved it and did not connect ("Config saved. Use Retry to reconnect."); every press after was "Server
'custom-ticket-system' already exists.", and the build ended with nothing made. Now an add of a name already saved
**at the same URL**, a remote server, is that server again: it takes the token given, if one is, and connects, and
answers as an add does. Nothing else of it changes. Anything else is still "already exists": the same name at another
URL, or the same name with other settings (change those on the Apps screen). The add's own checks run first: the URL
rule and the stdio gate, as for any add.
"""
from __future__ import annotations

import pytest

from core.mcp_client import MCPClientManager

URL = "https://tracker.example/mcp"


@pytest.fixture
def manager(monkeypatch):
    m = MCPClientManager.__new__(MCPClientManager)
    m.servers_config = []
    m.connects: list = []
    m.reachable = False

    async def save_servers():
        pass

    async def connect(config):
        m.connects.append(dict(config))
        return object() if m.reachable else None

    async def connect_or_authorise(config, wait=15.0):
        m.connects.append(dict(config))
        return {"connected": m.reachable, "status": "connected" if m.reachable else "disconnected"}

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(m, "save_servers", save_servers, raising=False)
    monkeypatch.setattr(m, "connect_remote_server", connect, raising=False)
    monkeypatch.setattr(m, "connect_or_authorise", connect_or_authorise, raising=False)
    monkeypatch.setattr(m, "_set_status", nothing, raising=False)
    monkeypatch.setattr(m, "_auto_register", nothing, raising=False)
    return m


def _add(manager, **more):
    return manager.add_server(**{"name": "custom-ticket-system", "label": "Custom Ticket System",
                                 "server_type": "remote", "url": URL, **more})


class TestPressedAgain:
    async def test_the_loop_ends_one_server_connected(self, manager):
        first = await _add(manager)
        assert first["connected"] is False
        manager.reachable = True
        again = await _add(manager)
        assert again["connected"] is True, again
        assert [s["name"] for s in manager.servers_config] == ["custom-ticket-system"]

    async def test_a_token_given_on_the_press_is_kept_as_an_add_keeps_it(self, manager):
        await _add(manager)
        manager.reachable = True
        await _add(manager, token="tok-123")
        [saved] = manager.servers_config
        assert saved["token"] == "tok-123" and manager.connects[-1]["token"] == "tok-123"

    async def test_no_token_keeps_the_one_it_has(self, manager):
        await _add(manager, token="tok-123")
        await _add(manager)
        assert manager.servers_config[0]["token"] == "tok-123"

    async def test_its_url_with_a_slash_is_the_same_url(self, manager):
        await _add(manager)
        await _add(manager, url=URL + "/")
        assert len(manager.servers_config) == 1


class TestStillAlreadyExists:
    async def test_another_url(self, manager):
        await _add(manager)
        with pytest.raises(ValueError, match="already exists"):
            await _add(manager, url="https://other.example/mcp")
        assert manager.servers_config[0]["url"] == URL

    async def test_other_settings(self, manager):
        await _add(manager)
        with pytest.raises(ValueError, match="Apps screen"):
            await _add(manager, env={"X-Extra": "1"})
        assert "env" not in manager.servers_config[0] or not manager.servers_config[0]["env"]

    async def test_another_kind_of_server(self, manager, monkeypatch):
        import core.mcp_client as mcp_client

        monkeypatch.setattr(mcp_client, "stdio_mcp_allowed", lambda: True)
        await _add(manager)
        with pytest.raises(ValueError, match="already exists"):
            await manager.add_server(name="custom-ticket-system", server_type="stdio", command="python")
