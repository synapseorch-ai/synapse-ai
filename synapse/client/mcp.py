"""The MCP door: what a key can do that has no v1 route (D294).

Building agents and workflows, the deletes, Boards, what waits on a person, a
workspace's activity and usage, a scheduled run's approval: a key reaches these
only at `{origin}/api/v1/mcp`, through the product tools' one registry (MCP2,
D292), which enforces the same role, scope, confirmation and audit for the CLI
as for Claude. Naveen's call (2026-10-17): the CLI calls that door rather than
gain routes of its own, so D124 (no workflow CRUD on the key routes) stands.

The endpoint is stateless and answers JSON, so one POST per call is the whole
protocol: no session, no `initialize`, no MCP SDK — `httpx` only, like the rest
of the client.
"""
from __future__ import annotations

import itertools
import json
from typing import Any

from synapse.client import errors
from synapse.client.errors import CliError
from synapse.client.http import ApiClient

#: The endpoint's path under the API base. Mirrors `core.mcp_server.endpoint.MCP_PATH`
#: (`/api/v1/mcp`); `synapse.mcp_bridge.MCP_PATH` is the same, and a test holds the three together.
MCP_SUFFIX = "/mcp"
_HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
#: How long a tool call may take: a run tool waits up to 120 s, a chat turn up to 600 s.
_READ_TIMEOUT = 660.0


class ToolError(CliError):
    """The tool answered with an error: refused, missing, or failed."""


def _refusal_code(message: str) -> int:
    """The exit code a tool's refusal leaves with: the registry says why in words, not statuses."""
    lowered = message.lower()
    if lowered.startswith("unknown tool") or " not found" in lowered or "no such" in lowered \
            or "does not exist" in lowered:
        return errors.NOT_FOUND
    if "not granted" in lowered or "needs the" in lowered and "role" in lowered:
        return errors.REFUSED
    return errors.ERROR


def _decode(result: dict) -> Any:
    """A tool result's value: its structured content, else its text read as JSON when it is JSON."""
    if result.get("structuredContent") is not None:
        value = result["structuredContent"]
    else:
        texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
        text = "\n".join(texts)
        try:
            value = json.loads(text)
        except ValueError:
            value = text
    if result.get("isError"):
        message = value.get("error") if isinstance(value, dict) else None
        message = str(message or value or "The tool failed.")
        raise ToolError(message, _refusal_code(message))
    return value


class Mcp:
    """`tools/list` and `tools/call` at the target's MCP endpoint, with its key."""

    _ids = itertools.count(1)

    def __init__(self, api: ApiClient):
        self.api = api

    @property
    def url(self) -> str:
        return self.api.target.api_base + MCP_SUFFIX

    def _rpc(self, method: str, params: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        resp = self._post(body)
        if resp.get("error"):
            err = resp["error"]
            raise CliError(f"{err.get('message') or 'The MCP call failed.'} (MCP {err.get('code')})")
        return resp.get("result") or {}

    def _post(self, body: dict) -> dict:
        import httpx

        resp = self.api.send("POST", "", url=self.url, json=body,
                             extra_headers=_HEADERS, timeout=httpx.Timeout(30.0, read=_READ_TIMEOUT))
        if "text/event-stream" in resp.headers.get("content-type", ""):
            # A server that streams its answer: the last `data:` line is the response.
            data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
            return json.loads(data[-1]) if data else {}
        return resp.json()

    def tools(self) -> list[dict]:
        """Every tool this key is offered, with its description and input schema."""
        return self._rpc("tools/list", {}).get("tools") or []

    def call(self, name: str, arguments: dict | None = None) -> Any:
        """One tool call's value; an error result raises `ToolError`."""
        args = {k: v for k, v in (arguments or {}).items() if v is not None}
        return _decode(self._rpc("tools/call", {"name": name, "arguments": args}))


def needs_confirmation(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") == "needs_confirmation" and bool(value.get("confirm_token"))
