"""
Regression tests for GHSA-3j67-x3j8-r32x — unauthenticated MCP stdio RCE.

Two independent layers of the fix are covered:

1. ``InternalTokenMiddleware`` no longer opens ``/api/*`` to *remote* callers
   when the internal token is unset — it falls back to loopback-only instead of
   blanket-permissive.
2. stdio MCP server registration is gated by ``allow_stdio_mcp`` / scale mode, so
   the arbitrary-command sink cannot be reached even when auth is weak. The gate
   runs *before* the manager, so no subprocess is ever spawned.
"""
import json
import os

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import core.config as config
from core.internal_auth import InternalTokenMiddleware, _is_loopback


# ── settings isolation ───────────────────────────────────────────────────────
# The suite shares one DATA_DIR, and the root autouse fixture does not reset
# settings.json — so restore it ourselves to avoid leaking allow_stdio_mcp=False
# (etc.) into other tests.
@pytest.fixture(autouse=True)
def _restore_settings():
    original = None
    if os.path.exists(config.SETTINGS_FILE):
        with open(config.SETTINGS_FILE) as f:
            original = f.read()
    yield
    if original is not None:
        with open(config.SETTINGS_FILE, "w") as f:
            f.write(original)
    elif os.path.exists(config.SETTINGS_FILE):
        os.remove(config.SETTINGS_FILE)


def _write_settings(**overrides):
    with open(config.SETTINGS_FILE, "w") as f:
        json.dump(overrides, f)


def _make_request(path="/api/mcp/servers", headers=None, client=("203.0.113.9", 4444)):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
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


# ── middleware: loopback-only fallback when no token is configured ────────────

def test_is_loopback_helper():
    assert _is_loopback(_make_request(client=("127.0.0.1", 1)))
    assert _is_loopback(_make_request(client=("::1", 1)))
    assert not _is_loopback(_make_request(client=("203.0.113.9", 1)))
    assert not _is_loopback(_make_request(client=None))


async def test_no_token_blocks_remote_api_access():
    mw = InternalTokenMiddleware(app=None)
    mw.token = ""  # simulate SYNAPSE_INTERNAL_TOKEN unset
    resp = await mw.dispatch(_make_request(client=("203.0.113.9", 4444)), _ok)
    assert resp.status_code == 403


async def test_no_token_allows_loopback():
    mw = InternalTokenMiddleware(app=None)
    mw.token = ""
    resp = await mw.dispatch(_make_request(client=("127.0.0.1", 4444)), _ok)
    assert json.loads(resp.body) == {"ok": True}


async def test_token_set_requires_header_even_from_loopback():
    mw = InternalTokenMiddleware(app=None)
    mw.token = "s3cret"
    # A set token is required for every internal /api/* caller, loopback included.
    resp = await mw.dispatch(_make_request(client=("127.0.0.1", 4444)), _ok)
    assert resp.status_code == 403
    # Correct header → allowed.
    resp = await mw.dispatch(_make_request(headers={"X-Synapse-Internal": "s3cret"}), _ok)
    assert json.loads(resp.body) == {"ok": True}


async def test_external_versioned_api_bypasses_internal_token():
    """The open /api/v1|v2 API (API-key auth) must stay reachable regardless of
    the internal-token state — including remote callers and a token-less backend.
    Guards against the loopback fallback ordering ahead of the v<N> skip."""
    for token in ("", "s3cret"):
        mw = InternalTokenMiddleware(app=None)
        mw.token = token
        for path in ("/api/v1/chat", "/api/v2/orchestrations"):
            resp = await mw.dispatch(
                _make_request(path=path, client=("203.0.113.9", 4444)), _ok
            )
            assert json.loads(resp.body) == {"ok": True}, (token, path)


# ── route: stdio registration gate (the RCE sink) ─────────────────────────────

async def test_stdio_registration_blocked_when_disabled(client, monkeypatch):
    """The advisory's exact request is refused with 403 and no server is added."""
    import core.server as server

    called = {"add": 0}

    class _FakeManager:
        async def add_server(self, **kwargs):
            called["add"] += 1
            return {"config": {}, "connected": False, "status": "disconnected"}

    monkeypatch.setattr(server, "mcp_manager", _FakeManager(), raising=False)
    _write_settings(allow_stdio_mcp=False)

    resp = await client.post(
        "/api/mcp/servers",
        json={
            "name": "vuln001-poc",
            "server_type": "stdio",
            "command": "python3",
            "args": ["-c", "open('/tmp/rce_proof.txt','w').write('x')"],
        },
    )
    assert resp.status_code == 403
    assert called["add"] == 0  # sink never reached — no subprocess spawned


async def test_stdio_registration_blocked_in_scale_mode(client, monkeypatch):
    import core.server as server

    monkeypatch.setattr(server, "mcp_manager", object(), raising=False)
    _write_settings(scale_mode_enabled=True, allow_stdio_mcp=True)

    resp = await client.post(
        "/api/mcp/servers",
        json={"name": "evil", "server_type": "stdio", "command": "python3", "args": []},
    )
    assert resp.status_code == 403


async def test_remote_registration_not_blocked_by_stdio_gate(client, monkeypatch):
    """Remote MCP servers stay usable even when stdio is disabled."""
    import core.server as server

    seen = {}

    class _FakeManager:
        async def add_server(self, **kwargs):
            seen.update(kwargs)
            return {"config": {"name": kwargs["name"]}, "connected": False, "status": "disconnected"}

    monkeypatch.setattr(server, "mcp_manager", _FakeManager(), raising=False)
    _write_settings(allow_stdio_mcp=False)

    resp = await client.post(
        "/api/mcp/servers",
        json={"name": "remote1", "server_type": "remote", "url": "https://example.com/mcp"},
    )
    assert resp.status_code != 403
    assert seen.get("server_type") == "remote"
