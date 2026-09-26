"""A remote MCP server that could not be reached at boot is tried again once the backend listens (F237).

`connect_all` runs in the lifespan, before uvicorn binds, so a remote server at
this install's own `/api/v1/mcp` was refused on every restart and read
Disconnected until someone pressed Retry (a server started beside Synapse, in a
compose file say, had the same race). Now one pass retries every remote server
that failed at boot, once the backend's own port accepts connections.
"""
import asyncio

from core import server as server_mod


class FakeManager:
    def __init__(self, configs, connects):
        self.servers_config = configs
        self.sessions = {c["name"]: object() for c in configs if c.get("status") == "connected"}
        self._connects = connects
        self.tried: list[str] = []

    async def reconnect_server(self, name):
        self.tried.append(name)
        if self._connects.get(name):
            self.sessions[name] = object()
            return True
        return False


def _configs():
    return [
        {"name": "self", "server_type": "remote", "status": "disconnected", "token": "sk"},
        {"name": "oauth", "server_type": "remote", "status": "reauth_needed"},
        {"name": "up", "server_type": "remote", "status": "connected", "token": "sk"},
        {"name": "local", "server_type": "stdio", "status": "disconnected"},
        {"name": "off", "server_type": "stdio", "status": "disabled"},
    ]


async def test_it_retries_each_remote_that_failed_at_boot_once_listening(monkeypatch):
    waited = []

    async def listening(port, seconds):
        waited.append(port)
        return True
    monkeypatch.setattr(server_mod, "_listening", listening)
    registered = []

    async def register(name):
        registered.append(name)

    manager = FakeManager(_configs(), {"self": True, "oauth": False})
    connected = await server_mod._retry_failed_remote_mcp(manager, register, port=8866)

    assert waited == [8866]                                  # it waited for the listener first
    assert manager.tried == ["self", "oauth"]                # remote and failed only, once each
    assert connected == ["self"] and registered == ["self"]  # tools registered for what connected


async def test_nothing_to_retry_waits_for_nothing(monkeypatch):
    async def listening(port, seconds):
        raise AssertionError("waited with nothing to retry")
    monkeypatch.setattr(server_mod, "_listening", listening)
    manager = FakeManager([c for c in _configs() if c["name"] in ("up", "local")], {})

    async def register(name):
        raise AssertionError("registered nothing")

    assert await server_mod._retry_failed_remote_mcp(manager, register, port=8866) == []
    assert manager.tried == []


async def test_one_server_s_error_does_not_stop_the_others(monkeypatch):
    async def listening(port, seconds):
        return True
    monkeypatch.setattr(server_mod, "_listening", listening)

    class Flaky(FakeManager):
        async def reconnect_server(self, name):
            if name == "self":
                raise RuntimeError("boom")
            return await super().reconnect_server(name)

    async def register(name):
        pass

    manager = Flaky(_configs(), {"oauth": True})
    assert await server_mod._retry_failed_remote_mcp(manager, register, port=8866) == ["oauth"]


async def test_listening_is_true_once_the_port_accepts():
    async def handle(reader, writer):
        writer.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        assert await server_mod._listening(port, 2) is True
    finally:
        srv.close()
        await srv.wait_closed()


async def test_listening_gives_up_on_a_closed_port():
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    srv.close()
    await srv.wait_closed()
    assert await server_mod._listening(port, 0.6) is False
