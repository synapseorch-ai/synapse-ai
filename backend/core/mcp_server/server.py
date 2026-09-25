"""Synapse as an MCP server: the tool list and the dispatch (D125).

Transport-free. `endpoint.py` serves it over streamable HTTP inside the backend;
`synapse mcp` (`synapse/mcp_bridge.py`) relays stdio clients to that endpoint.
Nothing here runs outside the backend process, because only it holds the tool
sessions and the store a builder tool or a run needs.

Two hooks make it reusable by an embedder (cloud's hosted endpoint):

* `authorize(request) -> principal | None` — who is calling, from the Starlette
  request (its headers; in cloud its app's state too). Checked at the HTTP edge
  and again on every call: the SDK runs handlers on its own task group, so
  nothing the edge learned reaches them.
* `bind(principal)` — an async context the call runs inside: the settings the
  request reads, and in a multi-tenant host the tenant.
* `server_module()` — an async context yielding what a builder tool is handed as
  `server_module` (its tool sessions). This process's `core.server` by default;
  a fleet's per-tenant module, acquired and given back, in cloud.

Where a run tool's run executes is `runs.set_run_backend`'s.
"""
from __future__ import annotations

import contextlib
import json
from typing import Any, AsyncIterator, Awaitable, Callable

from mcp import types
from mcp.server.lowlevel import Server

from core.log import get_logger
from core.mcp_server import catalog, runs, workflows

_log = get_logger(__name__)

Authorize = Callable[[Any], Awaitable[Any | None]]
Bind = Callable[[Any], contextlib.AbstractAsyncContextManager]
ServerModule = Callable[[], contextlib.AbstractAsyncContextManager]

#: The most text one tool result carries. `get_orchestration` and a run's state
#: have no bound of their own, and a client model reads every character.
RESULT_CHARS = 50_000

INSTRUCTIONS = (
    "Synapse builds and runs AI workflows (orchestrations) made of agents, tools and human "
    "checkpoints. Look before you build: list_agents, list_orchestrations and list_tool_servers "
    "say what exists. Each workflow is also a tool of its own (run_<name>). A run that asks a "
    "question comes back `paused`; answer it with answer_run. Deleting is done in the Synapse "
    "app, not here. Runs spend the workspace's model credits."
)


def synapse_version() -> str | None:
    """The installed Synapse's version, as the CLI reads it; the SDK's own otherwise."""
    try:
        from importlib.metadata import version
        return version("synapse-orch-ai")
    except Exception:
        pass
    try:
        from pathlib import Path
        for line in (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text().splitlines():
            if line.startswith("version") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def bearer_from(headers: Any) -> str | None:
    value = (headers.get("authorization") if headers is not None else None) or ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def authorize_api_key(request: Any) -> dict | None:
    """The open-source authorizer: a Synapse API key (`sk-syn-…`) as the request's bearer."""
    bearer = bearer_from(getattr(request, "headers", None))
    if not bearer:
        return None
    from core.api_keys import validate_api_key
    return await validate_api_key(bearer)


@contextlib.asynccontextmanager
async def bind_settings(_principal: Any) -> AsyncIterator[None]:
    """The open-source binding: the request's settings, as `bind_settings` middleware does.

    The endpoint sits outside the app's middleware, and the SDK's handlers run on
    their own task group, so nothing the HTTP request bound reaches them.
    """
    from core import settings_runtime

    token = await settings_runtime.bind()
    try:
        yield
    finally:
        settings_runtime.reset(token)


@contextlib.asynccontextmanager
async def this_process() -> AsyncIterator[Any]:
    """The open-source server module: this backend's own tool sessions."""
    import core.server as _server
    yield _server


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + f"\n… [cut: {len(text) - RESULT_CHARS} more characters]"
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)


def _json_result(value: Any) -> types.CallToolResult:
    return _text_result(json.dumps(value, default=str, ensure_ascii=False))


def _error(message: str) -> types.CallToolResult:
    return _text_result(json.dumps({"error": message}, ensure_ascii=False), is_error=True)


def _fixed_tools() -> list[types.Tool]:
    return catalog.builder_tools() + list(runs.RUN_TOOLS)


def _reserved() -> set[str]:
    return set(catalog.SERVED_BUILDER_TOOLS) | set(runs.RUN_TOOL_NAMES)


async def _call(name: str, args: dict, server_module: ServerModule = this_process) -> types.CallToolResult:
    if name in catalog.SERVED_BUILDER_TOOLS:
        from core.builder_tools import execute_builder_tool_with_events

        async with server_module() as module:
            output, _events = await execute_builder_tool_with_events(name, args, module)
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = None
        return _text_result(output, is_error=isinstance(parsed, dict) and "error" in parsed)

    if name in runs.RUN_TOOL_NAMES:
        return _json_result(await runs.call_run_tool(name, args))

    if name.startswith(workflows.PREFIX):
        orch = (await workflows.workflow_map(_reserved())).get(name)
        if orch is not None:
            message, inputs = workflows.split_args(orch, args)
            return _json_result(await runs.start_run(
                orch["id"], message, inputs, runs._wait(args, runs.DEFAULT_WAIT_SECONDS),
            ))

    # Deletes and everything else not on the allowlist land here: never the
    # builder's dispatch, whatever the name.
    return _error(f"Unknown tool: {name}")


def build_server(authorize: Authorize = authorize_api_key, bind: Bind = bind_settings,
                 name: str = "synapse", server_module: ServerModule = this_process) -> Server:
    server: Server = Server(name, version=synapse_version(), instructions=INSTRUCTIONS)

    async def _principal() -> Any | None:
        try:
            request = server.request_context.request
        except LookupError:
            return None
        if request is None:
            return None
        return await authorize(request)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        principal = await _principal()
        if principal is None:
            raise PermissionError("A valid Synapse API key is required.")
        async with bind(principal):
            fixed = _fixed_tools()
            named = await workflows.workflow_map(_reserved())
            return fixed + [workflows.as_tool(n, o) for n, o in named.items()]

    @server.call_tool()
    async def call_tool(tool_name: str, arguments: dict) -> types.CallToolResult:
        principal = await _principal()
        if principal is None:
            return _error("A valid Synapse API key is required.")
        async with bind(principal):
            try:
                return await _call(tool_name, arguments or {}, server_module)
            except runs.ToolRefusal as exc:
                return _error(str(exc))
            except KeyError as exc:
                return _error(f"Missing argument: {exc.args[0]}")
            except Exception as exc:  # a tool's failure is the caller's error result, not a 500
                _log.warning(f"MCP tool {tool_name} failed: {exc!r}")
                return _error(str(exc) or type(exc).__name__)

    return server
