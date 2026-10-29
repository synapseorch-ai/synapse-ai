"""Synapse as an MCP server: the tool list and the dispatch (D125).

Transport-free. `endpoint.py` serves it over streamable HTTP inside the backend;
`synapse mcp` (`synapse/mcp_bridge.py`) relays stdio clients to that endpoint.
Nothing here runs outside the backend process, because only it holds the tool
sessions and the store a builder tool or a run needs.

Four hooks make it reusable by an embedder (cloud's hosted endpoint):

* `authorize(request) -> principal | None` — who is calling, from the Starlette
  request (its headers; in cloud its app's state too). Checked at the HTTP edge
  and again on every call: the SDK runs handlers on its own task group, so
  nothing the edge learned reaches them.
* `bind(principal)` — an async context the call runs inside: the settings the
  request reads, and in a multi-tenant host the tenant.
* `server_module()` — an async context yielding what a builder tool is handed as
  `server_module` (its tool sessions). This process's `core.server` by default;
  a fleet's per-tenant module, acquired and given back, in cloud.
* `permits(principal, scope) -> bool` — whether the caller was granted a scope
  (`catalog.SCOPES`). A tool it was not granted is not listed, and calling it is
  refused by name. Everything is granted by default; cloud's OAuth tokens carry
  scopes (D127).
* `caller_of(principal) -> Caller` — who the principal is to the product tools
  (`core.product_tools`, MCP2, D292): their role, their scopes, the person. By
  default it is read off the principal (`caller_from`): the self-hosted product's
  key is its one person, the owner; cloud's principal carries its role.

Every tool but a workflow's own is a product tool: listed by
`registry.for_caller`, called through `registry.invoke`, which checks the role,
the scope and, for an irreversible act, a confirmation token, and tells the
host's audit.

Where a run tool's run executes is `runs.set_run_backend`'s, and where a knowledge
search runs `knowledge.set_knowledge_searcher`'s.
"""
from __future__ import annotations

import base64
import contextlib
import json
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from mcp import types
from mcp.server.lowlevel import Server

from core.log import get_logger
from core.mcp_server import catalog, runs, workflows
from core.product_tools import areas, registry
from core.product_tools.build import using_module
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role

_log = get_logger(__name__)

Authorize = Callable[[Any], Awaitable[Any | None]]
Bind = Callable[[Any], contextlib.AbstractAsyncContextManager]
ServerModule = Callable[[], contextlib.AbstractAsyncContextManager]
Permits = Callable[[Any, str], bool]
CallerOf = Callable[[Any], Caller]


def _icon(theme: str) -> types.Icon:
    # `theme` is the spec's (2025-11-25); the SDK's Icon has no field for it yet and passes it through as an extra.
    data = (Path(__file__).parent / "icons" / f"mark-{theme}-48.png").read_bytes()
    return types.Icon(src="data:image/png;base64," + base64.b64encode(data).decode(), mimeType="image/png",
                      sizes=["48x48"], theme=theme)


#: Our mark, as `initialize` gives it (F1215): an AI tool that draws a server's icon draws this one, by theme. The
#: dark theme's comes first, a white tile that shows on any background, for a tool that reads no theme. See icons/.
ICONS = [_icon("dark"), _icon("light")]

#: The most text one tool result carries. `get_orchestration` and a run's state
#: have no bound of their own, and a client model reads every character.
RESULT_CHARS = 50_000

INSTRUCTIONS = (
    "Synapse builds and runs AI workflows (orchestrations) made of agents, tools and human "
    "checkpoints. Look before you build: list_agents, list_orchestrations and list_tool_servers "
    "say what exists. run_orchestration runs any workflow by id or name; the ones a workspace "
    "offers to MCP clients are also tools of their own (run_<name>). A run that asks a question "
    "comes back `paused`; answer it with answer_run, and list_waiting shows everything that waits "
    "on a person. A delete answers first with what it would remove and a confirm_token; it is done "
    "only when called again with that token, so ask the person in between. Runs spend the "
    "workspace's model credits."
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


def permit_all(_principal: Any, _scope: str) -> bool:
    """A key, and every self-hosted caller, is granted every scope."""
    return True


def caller_from(principal: Any) -> Caller:
    """The caller a principal is, read off it: the self-hosted key's record (a dict) is the install's one person,
    the owner; an embedder's principal object says its own `role`, `scopes` and person (`user_id`)."""
    if isinstance(principal, dict):
        return Caller(role=Role.OWNER, via="mcp", credential_id=str(principal.get("id") or "") or None,
                      principal=principal)
    role = getattr(principal, "role", Role.OWNER)
    role = getattr(role, "value", role)
    user_id = getattr(principal, "user_id", None)
    credential = getattr(principal, "credential_id", None)
    return Caller(role=Role.parse(role), via="mcp", user_id=str(user_id) if user_id else None,
                  credential_id=str(credential) if credential else None,
                  scopes=getattr(principal, "scopes", None), principal=principal)


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


def as_mcp_tool(tool: registry.ProductTool) -> types.Tool:
    return types.Tool(name=tool.name, description=tool.description,
                      inputSchema=tool.schema(confirm_token=tool.needs_token("irreversible")),
                      annotations=types.ToolAnnotations(**tool.annotations()))


#: The caller `_fixed_tools()` lists for when none is given: everything a self-hosted key may call.
_EVERYTHING = Caller(role=Role.OWNER, via="mcp")


def _fixed_tools(caller: Caller = _EVERYTHING) -> list[types.Tool]:
    """The product tools this caller is offered, as MCP tools: listed ones only (a feature switched off is not)."""
    areas.ensure_loaded()
    return [as_mcp_tool(t) for t in registry.for_caller(caller)
            if t.offered is None or t.offered()]


def _reserved() -> set[str]:
    areas.ensure_loaded()
    return {t.name for t in registry.all_tools()}


def _result(value: Any) -> types.CallToolResult:
    return _text_result(value) if isinstance(value, str) else _json_result(value)


async def _call(name: str, args: dict, server_module: ServerModule = this_process,
                principal: Any = None, caller: Caller | None = None) -> types.CallToolResult:
    caller = caller or caller_from(principal if principal is not None else {})
    areas.ensure_loaded()
    tool = registry.get(name)
    if tool is not None and tool.area == "build":
        async with server_module() as module:
            with using_module(module):
                return _result(await registry.invoke(name, caller, args))
    if tool is not None:
        return _result(await registry.invoke(name, caller, args))

    if name.startswith(workflows.PREFIX):
        orch = (await workflows.workflow_map(_reserved())).get(name)
        if orch is not None:
            _may_run_a_workflow(caller)
            message, inputs = workflows.split_args(orch, args)
            return _json_result(await runs.start_run(
                orch["id"], message, inputs, runs._wait(args, runs.DEFAULT_WAIT_SECONDS),
            ))

    # Deletes and everything else not on the allowlist land here: never the
    # builder's dispatch, whatever the name.
    return _error(f"Unknown tool: {name}")


def _may_run_a_workflow(caller: Caller) -> None:
    """A workflow's own tool starts a run, as `run_orchestration` does: the run scope and the builder role."""
    if not caller.granted("run"):
        raise ToolRefusal("This connection was not granted 'run', which a workflow's tool needs. "
                          "Reconnect and allow 'run' to use it.")
    if not caller.role.can_act_as(Role.BUILDER):
        raise ToolRefusal(f"Running a workflow needs the builder role; you are a {caller.role.label} in this "
                          "workspace.")


def _offers_workflows(caller: Caller) -> bool:
    return caller.granted("run") and caller.role.can_act_as(Role.BUILDER)


def build_server(authorize: Authorize = authorize_api_key, bind: Bind = bind_settings,
                 name: str = "synapse", server_module: ServerModule = this_process,
                 permits: Permits = permit_all, caller_of: CallerOf = caller_from) -> Server:
    server: Server = Server(name, version=synapse_version(), instructions=INSTRUCTIONS, icons=ICONS)

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
        caller = caller_of(principal)
        async with bind(principal):
            tools = _fixed_tools(caller)
            if _offers_workflows(caller):
                named = await workflows.workflow_map(_reserved())
                tools += [workflows.as_tool(n, o) for n, o in named.items()]
            return [t for t in tools if permits(principal, catalog.scope_of(t.name))]

    @server.call_tool()
    async def call_tool(tool_name: str, arguments: dict) -> types.CallToolResult:
        principal = await _principal()
        if principal is None:
            return _error("A valid Synapse API key is required.")
        scope = catalog.scope_of(tool_name)
        if scope is not None and not permits(principal, scope):
            return _error(
                f"This connection was not granted '{scope}', which {tool_name} needs. "
                f"Reconnect and allow '{scope}' to use it."
            )
        caller = caller_of(principal)
        async with bind(principal):
            try:
                return await _call(tool_name, arguments or {}, server_module, principal, caller)
            except ToolRefusal as exc:
                return _error(str(exc))
            except KeyError as exc:
                return _error(f"Missing argument: {exc.args[0]}")
            except Exception as exc:  # a tool's failure is the caller's error result, not a 500
                _log.warning(f"MCP tool {tool_name} failed: {exc!r}")
                return _error(str(exc) or type(exc).__name__)

    return server
