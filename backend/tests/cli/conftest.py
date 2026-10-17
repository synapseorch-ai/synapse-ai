"""The CLI's test fixtures: a config of each test's own, the CLI against the live app, MCP's endpoint (D294).

Every test points `SYNAPSE_CLI_CONFIG` into its own tmp_path, so none can read
or write the ~/.synapse/cli.json of the person running the suite.
"""
from __future__ import annotations

import asyncio

import pytest

from cli_harness import main


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    from synapse.client import target

    monkeypatch.setenv("SYNAPSE_CLI_CONFIG", str(tmp_path / "cli.json"))
    monkeypatch.setenv("SYNAPSE_CHAT_HISTORY", str(tmp_path / "chat_history"))
    for name in ("SYNAPSE_API_URL", "SYNAPSE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    target._install_settings.cache_clear()
    yield
    target._install_settings.cache_clear()


@pytest.fixture
def synapse(live_server, api_key, monkeypatch, capsys):
    """`await synapse("agents", "list")` → (exit code, stdout, stderr), against the live app."""
    async def run(*argv):
        return await asyncio.to_thread(
            main, monkeypatch, capsys, *argv, "--url", live_server.url, "--api-key", api_key["raw"])
    return run


@pytest.fixture
async def mcp_endpoint(test_app):
    """The app's MCP endpoint running, as the lifespan starts it (A's `mcp_app`, D125)."""
    from core.mcp_server.endpoint import default_endpoint

    endpoint = default_endpoint()
    started, stop = asyncio.Event(), asyncio.Event()

    async def _serve():
        async with endpoint.serve():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_serve())
    await asyncio.wait_for(started.wait(), 5)
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


@pytest.fixture
def schedule_manager(test_app):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr._server_module = test_app.state.server_module
    test_app.state.schedule_manager = mgr
    return mgr


@pytest.fixture
def clean_install(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setenv("SYNAPSE_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.delenv("SYNAPSE_DATA_DIR", raising=False)
    monkeypatch.delenv("SYNAPSE_BACKEND_PORT", raising=False)
    return tmp_path
