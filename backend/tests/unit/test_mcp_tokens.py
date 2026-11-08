"""
MCP OAuth material in the database, per tenant.

Access tokens, refresh tokens and dynamic client registrations were per-server
JSON files in one shared folder under DATA_DIR. They are tenant secrets: with
one folder, any tenant's stored credential for `github` is the credential every
tenant reconnects with.

They live in the database rather than the blob store, matching the Google OAuth
credentials — the blob store is for tenant content, and secrets belong where a
hosted deployment's encryption and row-level security already are.
"""
import pytest
from mcp.shared.auth import OAuthToken

from core.mcp_client import StoreTokenStorage, _safe_server_name
from core.scale.context import set_resource_provider
from core.tenancy import tenant_scope


class _Provider:
    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


def _token(value="tok-1"):
    return OAuthToken(access_token=value, token_type="Bearer")


async def test_tokens_round_trip():
    storage = StoreTokenStorage("github")

    assert await storage.get_tokens() is None
    await storage.set_tokens(_token())
    assert (await storage.get_tokens()).access_token == "tok-1"


async def test_delete_all_removes_the_whole_document():
    storage = StoreTokenStorage("github")
    await storage.set_tokens(_token())

    await storage.delete_all()

    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None


async def test_one_tenants_token_is_not_anothers(multi_tenant):
    """The reason these left DATA_DIR.

    Same server name, two tenants: each must reconnect with its own
    credential, not with whichever was stored last.
    """
    with tenant_scope("acme"):
        await StoreTokenStorage("github").set_tokens(_token("acme-token"))

    with tenant_scope("globex"):
        assert await StoreTokenStorage("github").get_tokens() is None
        await StoreTokenStorage("github").set_tokens(_token("globex-token"))

    with tenant_scope("acme"):
        assert (await StoreTokenStorage("github").get_tokens()).access_token == "acme-token"


@pytest.mark.parametrize("name,expected", [
    ("github", "github"),
    ("my server", "my_server"),
    ("scope/thing", "scope_thing"),
    ("../escape", "_escape"),
    ("..", "_"),
])
def test_server_names_cannot_shape_a_path(name, expected):
    """The old sanitiser replaced only `/` and spaces, so `../x` stayed
    traversal-shaped when these were files. Keeping every unexpected character
    out means a badly-named server is never a question about path handling."""
    assert _safe_server_name(name) == expected


# ---------------------------------------------------------------------------
# Silent renewal
# ---------------------------------------------------------------------------
#
# A server authorised in the morning asked to be authorised again after every
# restart, and the refresh token sat unused through all of it. Three separate
# causes, all upstream in the MCP SDK and all invisible from here:
#
#   * `OAuthClientProvider._initialize()` restores the tokens but not
#     `token_expiry_time`;
#   * `is_token_valid()` treats a `None` expiry as *valid*, so the proactive
#     refresh guarded on `not is_token_valid()` never fires;
#   * the `401` that follows starts a **full re-authorisation**, not a refresh,
#     into the `noop_callback` a startup reconnect deliberately installs.
#
# So the expiry is written down as an absolute time, and the renewal is done
# here rather than hoped for. These tests pin the parts that made it fail.

import time
from contextlib import AsyncExitStack

from core.mcp_client import MCPClientManager


def _expiring(value="tok-1", seconds=3600, refresh="ref-1"):
    return OAuthToken(
        access_token=value,
        token_type="Bearer",
        expires_in=seconds,
        refresh_token=refresh,
    )


def _client_info():
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id="cl_1",
        client_secret="cs_1",
        redirect_uris=["http://localhost:8000/cb"],
    )


async def _prepare(name="jira", *, expires_in=3600, endpoint="https://auth.example.com/token"):
    storage = StoreTokenStorage(name)
    await storage.set_tokens(_expiring(seconds=expires_in))
    await storage.set_client_info(_client_info())
    if endpoint:
        await storage.set_token_endpoint(endpoint)
    return storage


class TestTheExpiryIsWrittenDown:
    async def test_set_tokens_records_an_absolute_expiry(self):
        """`expires_in` is a duration from issue time. Storing it and adding it
        to `now` at load time would call an hours-dead token fresh."""
        storage = StoreTokenStorage("jira")
        before = time.time()
        await storage.set_tokens(_expiring(seconds=3600))

        expires_at = await storage.get_expires_at()
        assert expires_at is not None
        assert before + 3599 <= expires_at <= time.time() + 3601

    async def test_a_token_with_no_expires_in_records_nothing(self):
        storage = StoreTokenStorage("jira")
        await storage.set_tokens(_token())
        assert await storage.get_expires_at() is None


class TestRenewal:
    async def test_a_live_token_is_left_alone(self, monkeypatch):
        """No exchange at all — the common case must cost nothing."""
        await _prepare(expires_in=3600)
        called = []

        async def never(cfg):
            called.append(cfg)
            return None

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", never)

        async with AsyncExitStack() as stack:
            renewed = await MCPClientManager(stack)._renew_if_expired(
                "jira", "https://mcp.example.com/mcp"
            )
        assert renewed is False
        assert called == []

    async def test_an_expired_token_is_renewed_and_stored(self, monkeypatch):
        storage = await _prepare(expires_in=-10)

        async def fake_refresh(cfg):
            assert cfg["token_endpoint"] == "https://auth.example.com/token"
            assert cfg["client_id"] == "cl_1"
            assert cfg["refresh_token"] == "ref-1"
            return {
                "access_token": "tok-2",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "ref-2",
            }

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", fake_refresh)

        async with AsyncExitStack() as stack:
            renewed = await MCPClientManager(stack)._renew_if_expired(
                "jira", "https://mcp.example.com/mcp"
            )

        assert renewed is True
        assert (await storage.get_tokens()).access_token == "tok-2"
        assert await storage.get_expires_at() > time.time() + 3000

    async def test_a_rotated_refresh_token_replaces_the_old_one(self, monkeypatch):
        """Several providers invalidate the old one on every refresh — keeping
        it would work once and fail forever after."""
        storage = await _prepare(expires_in=-10)

        async def rotated(cfg):
            return {"access_token": "tok-2", "refresh_token": "ref-2"}

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", rotated)

        async with AsyncExitStack() as stack:
            await MCPClientManager(stack)._renew_if_expired("jira", "https://x/mcp")

        assert (await storage.get_tokens()).refresh_token == "ref-2"

    async def test_a_provider_that_returns_no_refresh_token_keeps_the_old(
        self, monkeypatch
    ):
        """Dropping it would leave nothing to refresh with next time."""
        storage = await _prepare(expires_in=-10)

        async def fake_refresh(cfg):
            return {"access_token": "tok-2"}

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", fake_refresh)

        async with AsyncExitStack() as stack:
            await MCPClientManager(stack)._renew_if_expired("jira", "https://x/mcp")

        assert (await storage.get_tokens()).refresh_token == "ref-1"

    async def test_an_unknown_expiry_is_treated_as_expired(self, monkeypatch):
        """A token stored before the expiry was recorded. Assuming it is fine is
        precisely the assumption that produced the bug."""
        storage = StoreTokenStorage("jira")
        # A refresh token, and deliberately no `expires_in` — the shape of a
        # document written before the expiry was recorded.
        await storage.set_tokens(
            OAuthToken(access_token="tok-1", token_type="Bearer", refresh_token="ref-1")
        )
        await storage.set_client_info(_client_info())
        await storage.set_token_endpoint("https://auth.example.com/token")
        assert await storage.get_expires_at() is None

        attempted = []

        async def fake_refresh(cfg):
            attempted.append(cfg)
            return {"access_token": "tok-2"}

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", fake_refresh)

        async with AsyncExitStack() as stack:
            await MCPClientManager(stack)._renew_if_expired("jira", "https://x/mcp")

        assert attempted, "an unknown expiry must be refreshed, not trusted"

    async def test_a_refused_refresh_leaves_the_stored_tokens_alone(self, monkeypatch):
        """The old token is still what a re-authorisation will replace, and
        clearing it here would lose the refresh token a later attempt needs."""
        storage = await _prepare(expires_in=-10)

        async def refused(cfg):
            return None

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", refused)

        async with AsyncExitStack() as stack:
            renewed = await MCPClientManager(stack)._renew_if_expired("jira", "https://x/mcp")

        assert renewed is False
        assert (await storage.get_tokens()).access_token == "tok-1"
        assert (await storage.get_tokens()).refresh_token == "ref-1"

    async def test_no_refresh_token_means_no_attempt(self, monkeypatch):
        """A bearer PAT has no refresh flow and never had one."""
        storage = StoreTokenStorage("jira")
        await storage.set_tokens(_token())
        await storage.set_client_info(_client_info())

        attempted = []

        async def fake_refresh(cfg):
            attempted.append(cfg)

        import core.mcp_oauth as mcp_oauth
        monkeypatch.setattr(mcp_oauth, "refresh_access_token", fake_refresh)

        async with AsyncExitStack() as stack:
            renewed = await MCPClientManager(stack)._renew_if_expired("jira", "https://x/mcp")

        assert renewed is False
        assert attempted == []
