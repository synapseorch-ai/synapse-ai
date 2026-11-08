"""
Google OAuth credentials in the store.

Two properties matter. The refresh token must not reach the settings dict,
which is loaded on every turn and handed to a dozen call sites. And the
account email must stay reachable from synchronous code, because
`build_system_prompt` is sync, is on the per-turn path, and the only thing it
has ever wanted from these credentials is the address to tell the model to use.
"""
import json

import pytest

from core.scale.context import set_resource_provider
from core.tenancy import tenant_scope
from services import google as google_svc


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


@pytest.fixture(autouse=True)
def settings_from_the_store():
    """`load_settings()` only reaches the store once lifespan installs the
    provider; these tests need the same wiring the running server has."""
    from core import settings_runtime
    from core.config import set_settings_provider

    settings_runtime.reset_state()
    settings_runtime.install_provider()
    yield
    settings_runtime.reset_state()
    set_settings_provider(None)


_CLIENT = {"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "shh"}}
_TOKEN = {"token": "ya29.x", "refresh_token": "1//refresh", "email": "user@example.com"}


async def test_client_config_and_token_round_trip():
    assert await google_svc.load_client_config() is None
    assert await google_svc.load_token() is None

    await google_svc.save_client_config(_CLIENT)
    await google_svc.save_token(_TOKEN)

    assert (await google_svc.load_client_config())["installed"]["client_secret"] == "shh"
    assert (await google_svc.load_token())["refresh_token"] == "1//refresh"


async def test_the_email_is_a_setting_and_the_refresh_token_is_not():
    from core.config import load_settings

    await google_svc.save_token(_TOKEN)

    settings = load_settings()
    assert settings.get(google_svc.EMAIL_SETTING) == "user@example.com"
    assert "1//refresh" not in json.dumps(settings)


async def test_the_sync_prompt_builder_sees_the_email():
    """core/tools.py reads this off the snapshot, without awaiting anything."""
    from core.config import load_settings

    await google_svc.save_token(_TOKEN)

    assert load_settings().get(google_svc.EMAIL_SETTING) == "user@example.com"


async def test_an_email_only_in_the_id_token_is_still_found():
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps({"email": "jwt@example.com"}).encode()
    ).decode().rstrip("=")
    await google_svc.save_token({"token": "ya29.x", "id_token": f"hdr.{payload}.sig"})

    from core.config import load_settings
    assert load_settings().get(google_svc.EMAIL_SETTING) == "jwt@example.com"


async def test_credentials_are_per_tenant(multi_tenant):
    with tenant_scope("acme"):
        await google_svc.save_client_config(_CLIENT)
        await google_svc.save_token(_TOKEN)

    with tenant_scope("globex"):
        assert await google_svc.load_client_config() is None
        assert await google_svc.load_token() is None

    with tenant_scope("acme"):
        assert (await google_svc.load_token())["refresh_token"] == "1//refresh"


async def test_the_mcp_directory_is_materialised_from_the_rows():
    """workspace-mcp reads files, so the rows are written to scratch before it
    spawns — process-local, rebuildable, and nothing in it is not in the store."""
    assert await google_svc.materialise_mcp_dir() is None   # nothing configured yet

    await google_svc.save_client_config(_CLIENT)
    await google_svc.save_token(_TOKEN)

    directory = await google_svc.materialise_mcp_dir()
    assert directory is not None
    assert json.loads((directory / "client_secret.json").read_text())["installed"]["client_secret"] == "shh"
    assert json.loads((directory / "token.json").read_text())["refresh_token"] == "1//refresh"
    assert (directory / "user@example.com.json").exists()


async def test_a_status_check_does_not_refresh_or_write():
    """GET /api/config polls this. It used to perform a network token exchange
    and rewrite three files every time the integrations tab was opened."""
    await google_svc.save_client_config(_CLIENT)
    await google_svc.save_token({"token": "ya29.x", "refresh_token": "1//refresh"})

    assert await google_svc.get_google_credentials(refresh=False) is None
    # The stored token is untouched by the check.
    assert (await google_svc.load_token())["token"] == "ya29.x"
