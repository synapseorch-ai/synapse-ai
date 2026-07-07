"""
Route-surface smoke tests — "make sure nothing breaks".

Structural: assert the full app route inventory is registered (so an accidental
router-removal or bad decorator is caught) and that every expected router prefix
is mounted. Behavioural: hit a curated set of infra-free read-only routes and
assert they never 500.
"""
import pytest
from fastapi.routing import APIRoute


def _routes(app):
    return [(r.path, tuple(sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})))
            for r in app.routes if isinstance(r, APIRoute)]


# Critical routes that must always exist (method-agnostic path check).
CRITICAL_PATHS = [
    # agent chat
    "/chat", "/chat/stream",
    # orchestration (internal)
    "/api/orchestrations/{orch_id}", "/api/orchestrations/{orch_id}/run",
    "/api/orchestrations/runs/{run_id}/human-input",
    # agents / settings / usage / sessions / tools / vault
    "/api/agents", "/api/agents/{agent_id}", "/api/agents/active",
    "/api/settings", "/api/usage/summary", "/api/sessions",
    # v1 external
    "/api/v1/chat", "/api/v1/chat/stream",
    "/api/v1/orchestrations/{orch_id}/run",
    "/api/v1/orchestrations/runs/{run_id}/resume",
    # v2 external
    "/api/v2/chat", "/api/v2/chat/{session_id}/stream",
    "/api/v2/orchestrations/{orch_id}/run",
    "/api/v2/orchestrations/runs/{run_id}/stream",
]

EXPECTED_PREFIXES = ["/api/v1/", "/api/v2/", "/api/agents", "/api/orchestrations",
                     "/api/settings", "/api/usage", "/api/sessions", "/api/tools",
                     "/api/vault", "/api/logs", "/api/schedules", "/api/repos"]


def test_critical_routes_registered(test_app):
    paths = {p for p, _ in _routes(test_app)}
    missing = [p for p in CRITICAL_PATHS if p not in paths]
    assert not missing, f"Missing critical routes: {missing}"


def test_all_expected_prefixes_mounted(test_app):
    paths = [p for p, _ in _routes(test_app)]
    for prefix in EXPECTED_PREFIXES:
        assert any(p.startswith(prefix) for p in paths), f"No routes under {prefix}"


def test_route_surface_is_substantial(test_app):
    # Guards against a mass route-loss regression (e.g. a router failing to import).
    assert len(_routes(test_app)) >= 80


def test_no_duplicate_method_path_pairs(test_app):
    seen, dups = set(), []
    for path, methods in _routes(test_app):
        for m in methods:
            if (m, path) in seen:
                dups.append((m, path))
            seen.add((m, path))
    assert not dups, f"Duplicate route registrations: {dups}"


@pytest.mark.parametrize("path", [
    "/api/agent-types",
    "/api/usage/summary",
    "/api/usage/pricing",
    "/openapi.json",
])
async def test_safe_get_routes_do_not_500(client, path):
    resp = await client.get(path)
    assert resp.status_code < 500, f"{path} -> {resp.status_code}"
