"""`synapse mcp`: Synapse's MCP server for clients that speak stdio (D125).

A relay, nothing more. The server itself runs inside the backend (streamable HTTP
at `MCP_PATH`), because only the backend holds the tool sessions and the store a
builder tool or a run needs. This process forwards `tools/list` and `tools/call`
there and the answers back, so a stdio client (Claude Desktop, an editor's MCP
config) reaches the same tools a streamable-HTTP client does, local or remote.

It needs only `mcp` and `httpx`, never the backend's code. The CLI resolves the
origin and the key (flag, env, or the local install's own key) and calls `run()`;
this module never reads either from the environment itself.
"""
from __future__ import annotations

import sys

#: The endpoint's path on the origin. Mirrors `core.mcp_server.endpoint.MCP_PATH`
#: (a test holds the two together); this module cannot import the backend.
MCP_PATH = "/api/v1/mcp"

# A run tool waits up to 120 s and a chat turn up to 600 s before it answers.
_READ_TIMEOUT_SECONDS = 660.0


def _say(line: str) -> None:
    print(f"synapse mcp: {line}", file=sys.stderr, flush=True)


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status inside an (anyio-grouped) client error, if any."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    for inner in getattr(exc, "exceptions", ()) or ():
        status = _http_status(inner)
        if status is not None:
            return status
    cause = exc.__cause__ or exc.__context__
    return _http_status(cause) if cause is not None and cause is not exc else None


def _is_unreachable(exc: BaseException) -> bool:
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)):
        return True
    return any(_is_unreachable(e) for e in getattr(exc, "exceptions", ()) or ())


async def _relay(url: str, api_key: str | None) -> None:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(30.0, read=_READ_TIMEOUT_SECONDS)
    endpoint = url.rstrip("/") + MCP_PATH

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http, \
            streamable_http_client(endpoint, http_client=http) as (read, write, _), \
            ClientSession(read, write) as upstream:
        init = await upstream.initialize()

        server: Server = Server("synapse", instructions=init.instructions)

        @server.list_tools()
        async def list_tools():
            return (await upstream.list_tools()).tools

        # The backend validates; validating here too would only differ from it.
        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict):
            return await upstream.call_tool(name, arguments or {})

        async with stdio_server() as (stdin, stdout):
            await server.run(stdin, stdout, server.create_initialization_options())


def run(url: str, api_key: str | None) -> int:
    """Serve stdio until the client disconnects. Returns the process exit code.

    `url` is the origin (`http://127.0.0.1:8765`, `https://api.synapseorch.com`);
    the endpoint's path is appended here.
    """
    import anyio

    try:
        anyio.run(_relay, url, api_key)
        return 0
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:  # noqa: BLE001 — every failure is one line on stderr
        status = _http_status(exc)
        if status == 401:
            _say(f"the API key was refused by {url}. Create one with `synapse api-keys generate`.")
        elif status == 404:
            _say(f"{url} has no MCP endpoint at {MCP_PATH}. Is it a Synapse server new enough to serve MCP?")
        elif status is not None:
            _say(f"{url}{MCP_PATH} answered HTTP {status}.")
        elif _is_unreachable(exc):
            _say(f"could not reach {url}. Is Synapse running (`synapse start`)?")
        else:
            _say(f"stopped: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":  # a development entry; the CLI calls run() with what it resolved
    import os

    raise SystemExit(run(os.environ.get("SYNAPSE_API_URL", "http://127.0.0.1:8765"),
                         os.environ.get("SYNAPSE_API_KEY")))
