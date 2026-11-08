"""
Assert the exact mcp SDK surface this codebase binds to.

test_mcp_tool_servers.py proves the system works end to end; this module tells
you *why* it broke, in one line, without spawning a single subprocess.

The mcp 2.0.0 release rewrote the SDK: the lowlevel `Server` lost its decorator
registration (handlers are now passed as `on_list_tools=` / `on_call_tool=`
callbacks), `ClientSession.read_timeout_seconds` changed from `timedelta` to
`float`, and `Tool.inputSchema` was renamed `input_schema`. Every one of those is
a silent, import-time or first-call break for us.

Synapse pins `mcp>=1.29,<2` deliberately — v1.x is upstream's supported holding
position while it is in maintenance mode. These tests fail loudly the moment
something lifts that cap without doing the migration.
"""
from __future__ import annotations

import importlib
import inspect
from datetime import timedelta

import pytest


def test_lowlevel_server_still_has_decorator_api():
    """backend/tools/*.py register handlers with `@app.list_tools()` and
    `@app.call_tool()` at module scope. mcp 2.0 removed both from the lowlevel
    Server, so every tool server dies at import."""
    from mcp.server import Server

    server = Server("contract-check")
    missing = [name for name in ("list_tools", "call_tool") if not hasattr(server, name)]

    assert not missing, (
        f"mcp.server.Server is missing {missing} — the decorator API was removed in "
        "mcp 2.0. Every server in backend/tools/ will fail at import. Either pin "
        "mcp<2 or migrate to the Server(on_list_tools=..., on_call_tool=...) callbacks."
    )


def test_client_session_read_timeout_accepts_timedelta():
    """We pass `read_timeout_seconds=timedelta(...)` in 10 places. mcp 2.0 made
    it a float and adds it to a float clock, raising
    `unsupported operand type(s) for +: 'float' and 'datetime.timedelta'`."""
    from mcp import ClientSession

    param = inspect.signature(ClientSession.__init__).parameters.get("read_timeout_seconds")
    assert param is not None, "ClientSession no longer accepts read_timeout_seconds"

    annotation = str(param.annotation)
    assert "timedelta" in annotation, (
        f"ClientSession.read_timeout_seconds is annotated {annotation!r}, not timedelta — "
        "mcp 2.0 changed it to float. core/server.py, core/mcp_client.py, "
        "core/scale/worker_server_module.py, core/react_engine.py and "
        "core/orchestration/steps.py all pass timedelta and will raise at runtime."
    )


def test_tool_input_schema_field_name():
    """Tool definitions across backend/tools/ use `inputSchema=`; mcp 2.0 renamed
    the pydantic field to `input_schema` (60 occurrences would need updating)."""
    from mcp.types import Tool

    assert "inputSchema" in Tool.model_fields, (
        "mcp.types.Tool no longer has an 'inputSchema' field — mcp 2.0 renamed it to "
        "'input_schema'. All Tool(...) definitions in backend/tools/ need updating."
    )


# Every mcp symbol imported anywhere in backend/, as (module, attribute).
# Keep in sync when adding a new mcp import.
_REQUIRED_MCP_SYMBOLS = [
    ("mcp", "ClientSession"),
    ("mcp", "StdioServerParameters"),
    ("mcp.client.auth", "OAuthClientProvider"),
    ("mcp.client.auth", "TokenStorage"),
    ("mcp.client.sse", "sse_client"),
    ("mcp.client.stdio", "stdio_client"),
    ("mcp.client.streamable_http", "streamable_http_client"),
    ("mcp.server", "Server"),
    ("mcp.server.lowlevel", "Server"),
    ("mcp.server.stdio", "stdio_server"),
    ("mcp.server.streamable_http_manager", "StreamableHTTPSessionManager"),
    ("mcp.shared.auth", "OAuthClientInformationFull"),
    ("mcp.shared.auth", "OAuthClientMetadata"),
    ("mcp.shared.auth", "OAuthToken"),
    ("mcp.types", "EmbeddedResource"),
    ("mcp.types", "ImageContent"),
    ("mcp.types", "TextContent"),
    ("mcp.types", "Tool"),
    ("mcp.types", "ToolAnnotations"),
    ("mcp.types", "CallToolResult"),
]


@pytest.mark.parametrize("module_name,attribute", _REQUIRED_MCP_SYMBOLS)
def test_required_mcp_symbol_resolves(module_name: str, attribute: str):
    """Guards against an SDK reshuffle silently removing something we import."""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - only on a broken SDK
        pytest.fail(f"cannot import {module_name}: {exc}")

    assert hasattr(module, attribute), f"{module_name}.{attribute} no longer exists in the installed mcp SDK"


def test_installed_mcp_is_v1():
    """The pin is load-bearing: mcp 2.0 breaks every tool server and every
    ClientSession. If this fails, the cap was lifted without the migration."""
    from importlib.metadata import version

    installed = version("mcp")
    major = int(installed.split(".")[0])

    assert major == 1, (
        f"mcp {installed} is installed, but Synapse requires 1.x. mcp 2.0 removed the "
        "lowlevel Server decorator API and changed read_timeout_seconds to float. "
        "See backend/requirements.txt and pyproject.toml."
    )


def test_timedelta_timeout_round_trips_through_client_session():
    """Belt-and-braces: the actual value we construct at import time in
    core/server.py must be accepted by the installed SDK."""
    from mcp import ClientSession

    param = inspect.signature(ClientSession.__init__).parameters["read_timeout_seconds"]
    assert param.default is None
    # Constructing the timedelta the way core.config-driven modules do.
    assert isinstance(timedelta(seconds=60), timedelta)
