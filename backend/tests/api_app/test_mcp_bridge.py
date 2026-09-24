"""`synapse mcp`: a stdio client reaches the backend's MCP endpoint through the relay (D125).

The relay runs as its own process, as a stdio client would start it, against the
app served on a real socket.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.usefixtures("mcp_app")

# The endpoint fixture lives with the HTTP tests.
from test_mcp_server import mcp_app  # noqa: E402,F401


def _env(url: str, key: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYNAPSE_")}
    env["PYTHONPATH"] = str(REPO)  # the relay needs `synapse`, never `backend`
    env["SYNAPSE_API_URL"] = url
    if key:
        env["SYNAPSE_API_KEY"] = key
    return env


async def _relay(env: dict) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "synapse.mcp_bridge", env=env, cwd=str(REPO),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await asyncio.wait_for(proc.communicate(b""), 30)
    return proc.returncode, err.decode()


def test_the_relay_and_the_endpoint_share_one_path():
    from core.mcp_server.endpoint import MCP_PATH
    from synapse.mcp_bridge import MCP_PATH as RELAY_PATH
    assert RELAY_PATH == MCP_PATH


async def test_a_stdio_client_lists_and_calls_through_the_relay(live_server, api_key, seed_orchestration):
    await seed_orchestration(id="orch_hello", name="Say hello")
    params = StdioServerParameters(command=sys.executable, args=["-m", "synapse.mcp_bridge"],
                                   env=_env(live_server.url, api_key["raw"]), cwd=str(REPO))
    async with stdio_client(params) as (read, write), \
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
        init = await session.initialize()
        assert "Synapse" in (init.instructions or "")
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"list_orchestrations", "run_orchestration", "run_say_hello"} <= names
        assert not {n for n in names if n.startswith("delete_")}
        listed = await session.call_tool("list_orchestrations", {})
    assert not listed.isError
    assert "orch_hello" in listed.content[0].text


async def test_a_refused_key_is_one_line_and_exit_1(live_server):
    code, err = await _relay(_env(live_server.url, "sk-syn-" + "0" * 64))
    assert code == 1
    assert "API key was refused" in err
    assert err.count("\n") == 1, err


async def test_an_unreachable_server_is_one_line_and_exit_1():
    code, err = await _relay(_env("http://127.0.0.1:9", "sk-syn-x"))
    assert code == 1
    assert "could not reach http://127.0.0.1:9" in err
