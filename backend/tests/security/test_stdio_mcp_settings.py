"""
The two stdio-MCP hardening settings can be set, and the settings route refuses
what it would otherwise drop (S11, F202).

`allow_stdio_mcp` and `mcp_command_allowlist` existed only as defaults in
`core/config.py`: they were not fields of the `Settings` model, so
`POST /api/settings {"allow_stdio_mcp": false}` answered 200 and dropped the
field, and with that "off" saved a stdio `/usr/bin/touch` server still spawned
(measured by the OSS test pass). The backend's own startup warning, and
GHSA-3j67's guidance, told operators to set a knob nobody could set.

The route now takes both, and answers 422 naming any field it does not know —
a silently dropped field is how a security setting went missing, and it would
be again.
"""
import pytest

from core.mcp_client import check_stdio_command_allowed, stdio_mcp_allowed


@pytest.fixture(autouse=True)
def _restore_settings():
    """1.9.x keeps settings in one file that the suite's per-test reset does
    not touch, so a knob turned off here would reach every later test."""
    from core.config import load_settings
    from core.routes.settings import save_settings

    before = dict(load_settings())
    yield
    save_settings(before)


async def _get(client) -> dict:
    return (await client.get("/api/settings")).json()


class TestAllowStdioMcp:
    async def test_off_round_trips_and_refuses_a_stdio_server(self, client):
        resp = await client.post("/api/settings", json={"agent_name": "Synapse", "allow_stdio_mcp": False})

        assert resp.status_code == 200, resp.text
        assert (await _get(client))["allow_stdio_mcp"] is False
        assert stdio_mcp_allowed() is False

        resp = await client.post("/api/mcp/servers", json={
            "name": "touchy", "server_type": "stdio", "command": "/usr/bin/touch", "args": ["/tmp/x"],
        })
        assert resp.status_code == 403

    async def test_on_again(self, client):
        await client.post("/api/settings", json={"agent_name": "Synapse", "allow_stdio_mcp": False})
        await client.post("/api/settings", json={"agent_name": "Synapse", "allow_stdio_mcp": True})

        assert (await _get(client))["allow_stdio_mcp"] is True
        assert stdio_mcp_allowed() is True

    async def test_saving_another_screen_leaves_it_alone(self, client):
        """The settings page posts its own fields only; the merge keeps this."""
        await client.post("/api/settings", json={"agent_name": "Synapse", "allow_stdio_mcp": False})
        await client.post("/api/settings", json={"agent_name": "Synapse", "vault_enabled": True})

        assert (await _get(client))["allow_stdio_mcp"] is False


class TestCommandAllowlist:
    async def test_it_round_trips_and_is_enforced(self, client):
        resp = await client.post(
            "/api/settings", json={"agent_name": "Synapse", "mcp_command_allowlist": ["npx", "uvx"]}
        )

        assert resp.status_code == 200, resp.text
        assert (await _get(client))["mcp_command_allowlist"] == ["npx", "uvx"]
        check_stdio_command_allowed("npx")
        with pytest.raises(ValueError, match="not in the allowed MCP command list"):
            check_stdio_command_allowed("/usr/bin/touch")

    async def test_empty_allows_any_command(self, client):
        await client.post("/api/settings", json={"agent_name": "Synapse", "mcp_command_allowlist": ["npx"]})
        await client.post("/api/settings", json={"agent_name": "Synapse", "mcp_command_allowlist": []})

        check_stdio_command_allowed("/usr/bin/touch")


class TestTheRouteRefusesWhatItWouldDrop:
    async def test_an_unknown_field_is_a_422_that_names_it(self, client):
        resp = await client.post("/api/settings", json={"agent_name": "Synapse", "allow_stdio_mpc": False})

        assert resp.status_code == 422
        assert "allow_stdio_mpc" in resp.text
        assert (await _get(client))["allow_stdio_mcp"] is True

    async def test_a_patch_needs_no_agent_name(self, client):
        """Every caller sent `agent_name` only because the model required it."""
        name = (await _get(client))["agent_name"]
        resp = await client.post("/api/settings", json={"allow_stdio_mcp": False})

        assert resp.status_code == 200, resp.text
        assert (await _get(client))["allow_stdio_mcp"] is False
        assert (await _get(client))["agent_name"] == name
