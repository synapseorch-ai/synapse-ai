"""A tool server shared by every tenant is given only the environment it needs (F1112).

The shared servers (`time`, `collect_data`, the two parsers, the scraper) are one
process each for every tenant a worker serves, and none of them opens the store.
So they are given an allowlist of this process's environment, never a copy of
all of it.
"""

from __future__ import annotations

import pytest

from core.scale.worker_server_module import (
    _BACKEND_ROOT,
    _TOOLS_DIR,
    SHARED_TOOL_ENV_NAMES,
    SHARED_TOOL_ENV_PREFIXES,
    _get_native_mcp_servers,
)

#: Shaped like what a worker holds and a shared server must not.
WITHHELD = {
    "DATABASE_URL": "postgresql://app:not-a-real-password@db.internal/app",
    "SYNAPSE_DB_URL": "postgresql://app:not-a-real-password@db.internal/app",
    "REDIS_URL": "rediss://cache.internal:6379/0",
    "MODEL_GATEWAY_SIGNING_KEY": "not-a-real-key",
    "SMTP_PASSWORD": "not-a-real-password",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/not-real",
    "ANY_OTHER_SETTING": "x",
}

#: What a shared server does need, and must still be given.
GIVEN = {
    "HTTPS_PROXY": "http://egress-proxy.internal:4750",
    "NO_PROXY": "localhost,127.0.0.1",
    "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
    "EGRESS_ALLOW_HOSTS": "",
    "LC_ALL": "C.UTF-8",
}


@pytest.fixture
def worker_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {**WITHHELD, **GIVEN}.items():
        monkeypatch.setenv(name, value)


def _shared() -> dict:
    servers = _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="shared")
    assert servers, "no shared tool servers to check"
    return servers


@pytest.mark.usefixtures("worker_environment")
def test_a_shared_server_is_given_only_allowlisted_names() -> None:
    for name, params in _shared().items():
        stray = {
            key
            for key in params.env
            if key not in SHARED_TOOL_ENV_NAMES and not key.startswith(SHARED_TOOL_ENV_PREFIXES)
        }
        assert not stray, f"{name} is given {sorted(stray)}"
        assert not set(WITHHELD) & set(params.env), f"{name} is given a withheld name"


@pytest.mark.usefixtures("worker_environment")
def test_what_a_shared_server_needs_is_still_given() -> None:
    for name, params in _shared().items():
        for key, value in GIVEN.items():
            assert params.env.get(key) == value, f"{name} lost {key}"
        assert params.env["PYTHONPATH"].split(":")[0] == str(_BACKEND_ROOT), name


@pytest.mark.usefixtures("worker_environment")
def test_a_deployment_can_add_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPSE_TOOL_ENV_EXTRA", "SCRAPER_USER_AGENT, ")
    monkeypatch.setenv("SCRAPER_USER_AGENT", "synapse")

    for params in _shared().values():
        assert params.env.get("SCRAPER_USER_AGENT") == "synapse"
        assert "ANY_OTHER_SETTING" not in params.env


@pytest.mark.usefixtures("worker_environment")
def test_a_tenant_scoped_server_still_inherits() -> None:
    """Narrowing these is its own change: they open the store, so they keep its URL."""
    for name, params in _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="tenant").items():
        assert params.env.get("SYNAPSE_DB_URL") == WITHHELD["SYNAPSE_DB_URL"], name
