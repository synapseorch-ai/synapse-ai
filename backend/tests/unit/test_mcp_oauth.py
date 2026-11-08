"""Refreshing a remote MCP server's OAuth access token.

An access token lasts about an hour and nothing renewed one, so a server
authorised in the morning stopped working by lunchtime — and reported it as a
cancelled job, because the `401` crashes the MCP client's task group rather than
surfacing as a status code. `reauth_needed` has been in the cloud router's
documented vocabulary since it was written with nothing able to set it.

No network here: `httpx.AsyncClient.post` is replaced. What is worth asserting is
the shape of what gets sent, what gets kept, and what happens when a refresh is
refused — not that httpx works.
"""
import pytest

from core import mcp_oauth

pytestmark = pytest.mark.anyio


FULL = {
    "name": "vercel",
    "refresh_token": "rt_old",
    "token_endpoint": "https://auth.example.com/token",
    "client_id": "cl_1",
    "client_secret": "cs_1",
}


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    @property
    def text(self):
        """A real `httpx.Response` has one, and a refusal is logged from it."""
        import json as _json

        return _json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_persister():
    mcp_oauth.set_token_persister(None)
    yield
    mcp_oauth.set_token_persister(None)


def _stub(monkeypatch, response, sent=None):
    async def post(self, url, data=None, headers=None):
        if sent is not None:
            sent.append({"url": url, "data": data})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("httpx.AsyncClient.post", post)


class TestWhatItRefusesToTry:
    """A server with no refresh material must be reported as needing
    re-authorisation, not as a refresh that failed."""

    @pytest.mark.parametrize(
        "missing", ["refresh_token", "token_endpoint", "client_id"]
    )
    async def test_a_config_missing_any_piece_cannot_refresh(self, missing):
        cfg = {k: v for k, v in FULL.items() if k != missing}
        assert mcp_oauth.can_refresh(cfg) is False
        assert await mcp_oauth.refresh_access_token(cfg) is None

    async def test_a_bearer_token_server_cannot_refresh(self):
        """A PAT the customer pasted has no refresh flow, and never had one."""
        assert mcp_oauth.can_refresh({"name": "x", "token": "pat_123"}) is False


class TestTheExchange:
    async def test_it_sends_the_refresh_grant_with_the_stored_client(
        self, monkeypatch
    ):
        sent: list = []
        _stub(monkeypatch, _Response(200, {"access_token": "at_new"}), sent)

        assert await mcp_oauth.refresh_access_token(FULL) == {"access_token": "at_new"}

        assert sent[0]["url"] == "https://auth.example.com/token"
        assert sent[0]["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "rt_old",
            "client_id": "cl_1",
            "client_secret": "cs_1",
        }

    async def test_a_public_client_sends_no_secret(self, monkeypatch):
        """Dynamic registration does not always return one, and sending an empty
        `client_secret` is rejected by some servers."""
        sent: list = []
        _stub(monkeypatch, _Response(200, {"access_token": "at_new"}), sent)

        cfg = {k: v for k, v in FULL.items() if k != "client_secret"}
        await mcp_oauth.refresh_access_token(cfg)
        assert "client_secret" not in sent[0]["data"]

    async def test_the_endpoint_is_the_stored_one_not_a_rediscovered_one(
        self, monkeypatch
    ):
        """A server that later advertises a different token endpoint must not be
        handed this org's refresh token on its say-so. The endpoint is the one
        recorded when the person authorised, which has already been through the
        egress guard."""
        sent: list = []
        _stub(monkeypatch, _Response(200, {"access_token": "at_new"}), sent)

        await mcp_oauth.refresh_access_token({**FULL, "url": "https://elsewhere.example.com"})
        assert sent[0]["url"] == "https://auth.example.com/token"


class TestWhenItFails:
    async def test_a_rejected_refresh_returns_none(self, monkeypatch):
        """Revoked, expired or already spent all mean the same thing to the
        person: authorise it again."""
        _stub(monkeypatch, _Response(400, {"error": "invalid_grant"}))
        assert await mcp_oauth.refresh_access_token(FULL) is None

    async def test_the_refusal_body_reaches_the_log(self, monkeypatch, capsys):
        """`invalid_grant` and `invalid_client` are both `400` and want opposite
        responses — one is "authorise it again", the other means the client
        registration is wrong and re-authorising will fail identically forever.
        Logging the status alone sent a real diagnosis down the wrong path."""
        _stub(monkeypatch, _Response(400, {"error": "invalid_client"}))

        await mcp_oauth.refresh_access_token(FULL)

        assert "invalid_client" in capsys.readouterr().out

    async def test_a_transport_failure_returns_none(self, monkeypatch):
        _stub(monkeypatch, RuntimeError("connection reset"))
        assert await mcp_oauth.refresh_access_token(FULL) is None

    async def test_a_200_with_no_access_token_returns_none(self, monkeypatch):
        _stub(monkeypatch, _Response(200, {"token_type": "bearer"}))
        assert await mcp_oauth.refresh_access_token(FULL) is None


class TestPersisting:
    async def test_a_rotated_refresh_token_is_handed_on(self, monkeypatch):
        """Several providers issue a new refresh token every time and invalidate
        the old one. Keeping only the access token works once and fails forever
        after."""
        _stub(monkeypatch, _Response(200, {"access_token": "at_new", "refresh_token": "rt_new"}))
        seen: list = []

        async def persist(name, tokens):
            seen.append((name, tokens))

        mcp_oauth.set_token_persister(persist)
        assert await mcp_oauth.refresh_and_persist(FULL) == "at_new"

        assert seen == [("vercel", {"access_token": "at_new", "refresh_token": "rt_new"})]

    async def test_no_persister_is_a_supported_state(self, monkeypatch):
        """Refresh still works for the life of the process; it is simply asked
        for again next time. Keeps this usable in a test and in a CLI."""
        _stub(monkeypatch, _Response(200, {"access_token": "at_new"}))
        assert await mcp_oauth.refresh_and_persist(FULL) == "at_new"

    async def test_a_persister_that_raises_does_not_lose_the_token(self, monkeypatch):
        """A token that works but was not written down is still worth using for
        this job — far better than failing a turn that could have run."""
        _stub(monkeypatch, _Response(200, {"access_token": "at_new"}))

        async def persist(name, tokens):
            raise RuntimeError("kms is away")

        mcp_oauth.set_token_persister(persist)
        assert await mcp_oauth.refresh_and_persist(FULL) == "at_new"


def test_the_client_registers_under_the_products_name():
    """It registered as "Synapse AI" — the old name, shown on every provider's
    consent screen (F74). A registration already stored is reused as it is,
    so a server keeps the old name until it is disconnected and connected again."""
    from core.mcp_client import _make_oauth_provider

    provider = _make_oauth_provider("vercel", "https://mcp.example.com/mcp")

    assert provider.context.client_metadata.client_name == "SynapseOrch"
