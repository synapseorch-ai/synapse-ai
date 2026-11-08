"""
Regression tests for two holes left open by the original GHSA-3j67-x3j8-r32x fix.

1. ``POST /chat`` and ``/chat/stream`` (core/routes/chat.py) are mounted at the
   root, so the middleware's "not /api/ → skip" rule let *any* caller who could
   reach the port drive the full ReAct tool surface — bash, execute_python, SQL,
   every MCP server — with no credentials at all.

   The follow-up commit that moved the public skips ahead of the token gate made
   this strictly worse: before it, a token-less remote caller was stopped by the
   loopback fallback; after it, ``/chat`` passed through in *every* token state.

2. The stdio spawn gate was applied at the callers (``add_server``,
   ``reconnect_server``, ``connect_all``) rather than at the sinks, so
   ``POST /api/import`` — which writes a bundle's MCP configs to disk and calls
   ``connect_stdio_server`` directly — bypassed it entirely. The worker has a
   second, wholly separate sink (``_build_stdio_mcp_params``) that had no gate at
   all, despite workers running in scale mode where stdio is meant to be off.
"""
import json
from contextlib import AsyncExitStack

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.internal_auth import InternalTokenMiddleware, _is_internal_root_route


def _make_request(path, headers=None, client=("203.0.113.9", 4444), method="POST"):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


async def _ok(_request):
    return JSONResponse({"ok": True})


# ── S1: /chat is an internal route and must be gated ─────────────────────────

CHAT_PATHS = ("/chat", "/chat/stream")


@pytest.mark.parametrize("path", CHAT_PATHS)
async def test_chat_blocked_for_remote_caller_without_token_configured(path):
    """Token unset + remote caller → 403, same as the internal /api/* surface."""
    mw = InternalTokenMiddleware(app=None)
    mw.token = ""
    resp = await mw.dispatch(_make_request(path, client=("203.0.113.9", 4444)), _ok)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", CHAT_PATHS)
async def test_chat_blocked_when_token_set_but_header_missing(path):
    """The regression that mattered most: a *configured* token used to make /chat
    MORE exposed, because the public skip ran before the token gate. Docker images
    auto-generate a token, so this was the shipped default posture."""
    mw = InternalTokenMiddleware(app=None)
    mw.token = "s3cret"
    resp = await mw.dispatch(_make_request(path, client=("203.0.113.9", 4444)), _ok)
    assert resp.status_code == 403
    # Loopback is not a free pass once a token is configured, either.
    resp = await mw.dispatch(_make_request(path, client=("127.0.0.1", 4444)), _ok)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", CHAT_PATHS)
async def test_chat_allowed_with_correct_internal_header(path):
    """The Next.js route handler sends this via backendHeaders(), so gating /chat
    is transparent to the UI."""
    mw = InternalTokenMiddleware(app=None)
    mw.token = "s3cret"
    resp = await mw.dispatch(
        _make_request(path, headers={"X-Synapse-Internal": "s3cret"}), _ok
    )
    assert json.loads(resp.body) == {"ok": True}


@pytest.mark.parametrize("path", CHAT_PATHS)
async def test_chat_allowed_from_loopback_when_no_token_configured(path):
    """Local single-user installs with no token keep working."""
    mw = InternalTokenMiddleware(app=None)
    mw.token = ""
    resp = await mw.dispatch(_make_request(path, client=("127.0.0.1", 4444)), _ok)
    assert json.loads(resp.body) == {"ok": True}


async def test_public_v1_chat_api_still_reachable():
    """/api/v1/chat is the *public* chat API and authenticates with an API key
    (require_api_key). Gating root /chat must not break it in any token state."""
    for token in ("", "s3cret"):
        mw = InternalTokenMiddleware(app=None)
        mw.token = token
        resp = await mw.dispatch(
            _make_request("/api/v1/chat", client=("203.0.113.9", 4444)), _ok
        )
        assert json.loads(resp.body) == {"ok": True}, token


async def test_oauth_redirect_routes_still_public():
    """/auth/* are browser-navigated OAuth redirects and cannot carry a header."""
    mw = InternalTokenMiddleware(app=None)
    mw.token = "s3cret"
    resp = await mw.dispatch(
        _make_request("/auth/callback", client=("203.0.113.9", 4444), method="GET"), _ok
    )
    assert json.loads(resp.body) == {"ok": True}


def test_internal_root_prefix_matching_is_exact():
    """Segment-aware matching: /chat and /chat/* are internal, a hypothetical
    /chatbot-docs is not. Guards against a future root route being caught."""
    assert _is_internal_root_route("/chat")
    assert _is_internal_root_route("/chat/stream")
    assert not _is_internal_root_route("/chatbot-docs")
    assert not _is_internal_root_route("/api/v1/chat")
    assert not _is_internal_root_route("/")


# ── S2: the stdio gate belongs at the sinks, not only the callers ────────────

async def test_connect_stdio_server_refuses_when_disabled(monkeypatch):
    """POST /api/import called this directly, skipping the caller-side gates."""
    import core.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client, "stdio_mcp_allowed", lambda: False)
    mgr = mcp_client.MCPClientManager(AsyncExitStack())
    async def _noop_status(*a, **k):
        return None

    monkeypatch.setattr(mgr, "_set_status", _noop_status)

    session = await mgr.connect_stdio_server(
        {"name": "evil", "command": "bash", "args": ["-c", "id > /tmp/pwned"]}
    )
    assert session is None
    assert "evil" not in mgr.sessions


async def test_connect_stdio_server_enforces_command_allowlist(monkeypatch):
    import core.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client, "stdio_mcp_allowed", lambda: True)

    def _deny(command):
        raise ValueError(f"Command '{command}' is not in the allowed MCP command list.")

    monkeypatch.setattr(mcp_client, "check_stdio_command_allowed", _deny)
    mgr = mcp_client.MCPClientManager(AsyncExitStack())
    async def _noop_status(*a, **k):
        return None

    monkeypatch.setattr(mgr, "_set_status", _noop_status)

    session = await mgr.connect_stdio_server(
        {"name": "evil", "command": "bash", "args": ["-c", "id"]}
    )
    assert session is None


def test_worker_stdio_params_refused_when_disabled(monkeypatch):
    """The worker's spawn sink is entirely separate from MCPClientManager and had
    no gate at all — despite workers running in scale mode, where stdio MCP is
    supposed to be force-disabled."""
    import core.mcp_client as mcp_client
    from core.scale.worker_server_module import _build_stdio_mcp_params

    monkeypatch.setattr(mcp_client, "stdio_mcp_allowed", lambda: False)
    params = _build_stdio_mcp_params(
        {"name": "evil", "command": "bash", "args": ["-c", "id"]}
    )
    assert params is None


def test_worker_stdio_params_allowed_when_enabled(monkeypatch):
    """Sanity check the gate is not simply always-off."""
    import core.mcp_client as mcp_client
    from core.scale.worker_server_module import _build_stdio_mcp_params

    monkeypatch.setattr(mcp_client, "stdio_mcp_allowed", lambda: True)
    monkeypatch.setattr(mcp_client, "check_stdio_command_allowed", lambda c: None)
    params = _build_stdio_mcp_params(
        {"name": "ok", "command": "echo", "args": ["hi"]}
    )
    assert params is not None
    assert params.command == "echo"
