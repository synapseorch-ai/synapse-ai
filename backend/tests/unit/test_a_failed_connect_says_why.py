"""A server that does not connect says why, in the same words wherever it is asked (F1306, REACH1; CAP3's probe, eb 11-04).

A remote MCP server whose certificate the machine refused was added with "Config saved. Use Retry to reconnect.", and
Retry answered "Could not reconnect.": the connect caught the transport's task-group error, logged "unhandled errors in
a TaskGroup (1 sub-exception)" and returned nothing. The reason was one unwrap away. These pin the class:

- a failed connect is worded once (`mcp_client.connect_failure`): the first real error inside the group, scrubbed as a
  stored error is, and without the server's own token, the credentials in its address or its query's values;
- the manager keeps the last reason per server (`last_error`), cleared by a connect that succeeds;
- the add's answer, Retry's answer and the server list all say that same reason, and none of them a credential.
"""
from __future__ import annotations

import ssl
from contextlib import AsyncExitStack

import pytest

from core.mcp_client import MCPClientManager

SSL_SAID = "certificate verify failed: CA cert does not include key usage extension"
TOKEN = "plainsecretvalue2024x"
PASSWORD = "hunter2-very-secret"
QKEY = "q9k8j7h6g5f4d3s2"
URL = "https://mcp.example.com/mcp"
WITH_SECRETS = f"https://ops:{PASSWORD}@mcp.example.com/mcp?api_key={QKEY}"
SECRETS = (TOKEN, PASSWORD, QKEY)


@pytest.fixture
async def manager(monkeypatch):
    import core.server as server
    from core import mcp_client

    async def no_refusal(url):
        return None

    stack = AsyncExitStack()
    mgr = MCPClientManager(stack)
    monkeypatch.setattr(server, "mcp_manager", mgr)
    monkeypatch.setattr(mcp_client, "remote_url_refusal", no_refusal)
    try:
        yield mgr
    finally:
        await stack.aclose()


def _transport(monkeypatch, error: BaseException | None):
    """Every remote connect over a fake transport: one that fails inside its task group with `error`, or connects."""
    from core import mcp_client

    def opener_for(url, http_client):
        async def opener(stack):
            await http_client.aclose()
            if error is None:
                return object()
            raise ExceptionGroup("unhandled errors in a TaskGroup", [error])
        return opener

    monkeypatch.setattr(mcp_client, "_http_opener", opener_for)


def _leaked(*texts) -> list[str]:
    return [s for s in SECRETS for t in texts if s in str(t)]


class TestItSaysWhy:
    async def test_the_add_says_why_it_did_not_connect(self, client, manager, monkeypatch):
        _transport(monkeypatch, ssl.SSLCertVerificationError(SSL_SAID))
        added = await client.post("/api/mcp/servers", json={
            "name": "tracker", "label": "Tracker", "server_type": "remote", "url": URL, "token": TOKEN})
        assert added.status_code == 200, added.text
        body = added.json()
        assert body["connected"] is False
        assert SSL_SAID in body["message"] and "Use Retry to reconnect" not in body["message"], body["message"]
        assert manager.last_error("tracker") == SSL_SAID

    async def test_retry_and_the_list_say_the_same(self, client, manager, monkeypatch):
        _transport(monkeypatch, ssl.SSLCertVerificationError(SSL_SAID))
        await client.post("/api/mcp/servers", json={
            "name": "tracker", "label": "Tracker", "server_type": "remote", "url": URL, "token": TOKEN})
        retried = (await client.post("/api/mcp/servers/tracker/reconnect")).json()
        assert retried["connected"] is False
        assert retried["message"] == f"Could not reconnect: {SSL_SAID}.", retried["message"]
        [listed] = (await client.get("/api/mcp/servers")).json()
        assert listed["last_error"] == SSL_SAID

    async def test_a_connect_that_succeeds_clears_it(self, client, manager, monkeypatch):
        _transport(monkeypatch, ssl.SSLCertVerificationError(SSL_SAID))
        await client.post("/api/mcp/servers", json={
            "name": "tracker", "label": "Tracker", "server_type": "remote", "url": URL, "token": TOKEN})
        assert manager.last_error("tracker") == SSL_SAID
        _transport(monkeypatch, None)
        assert await manager.connect_remote_server(manager.get_server_config("tracker")) is not None
        assert manager.last_error("tracker") is None


class TestItNeverSaysACredential:
    def _quoting(self):
        return RuntimeError(f"Client error '401 Unauthorized' for url '{WITH_SECRETS}' "
                            f"(sent Authorization: Bearer {TOKEN})")

    async def test_with_a_token(self, client, manager, monkeypatch):
        _transport(monkeypatch, self._quoting())
        added = (await client.post("/api/mcp/servers", json={
            "name": "tracker", "label": "Tracker", "server_type": "remote", "url": WITH_SECRETS,
            "token": TOKEN})).json()
        retried = (await client.post("/api/mcp/servers/tracker/reconnect")).json()
        [listed] = (await client.get("/api/mcp/servers")).json()
        said = manager.last_error("tracker")
        assert "401 Unauthorized" in said, said
        assert _leaked(added["message"], retried["message"], listed["last_error"], said) == []

    async def test_without_one(self, client, manager, monkeypatch):
        _transport(monkeypatch, self._quoting())
        added = (await client.post("/api/mcp/servers", json={
            "name": "tracker", "label": "Tracker", "server_type": "remote", "url": WITH_SECRETS})).json()
        assert added["connected"] is False and "401 Unauthorized" in added["message"], added
        assert _leaked(added["message"], manager.last_error("tracker")) == []

    def test_the_one_function_words_it(self):
        from core.mcp_client import connect_failure

        said = connect_failure(ExceptionGroup("g", [self._quoting()]),
                               {"name": "tracker", "url": WITH_SECRETS, "token": TOKEN})
        assert "401 Unauthorized" in said and _leaked(said) == []
        assert connect_failure(ExceptionGroup("g", [ssl.SSLCertVerificationError(SSL_SAID)]), {}) == SSL_SAID
