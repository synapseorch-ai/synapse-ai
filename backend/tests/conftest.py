"""
Root test harness for the Synapse backend suite.

Critical ordering (mirrors backend/tests/unit/test_cache.py): SYNAPSE_DATA_DIR
and the fake-LLM delay profile are set on the environment **before** any
``core.*`` module is imported, because many modules bind DATA_DIR and construct
JsonStore(...) at import time.

What this provides
------------------
- Full data-dir isolation (temp dir, real user data never touched).
- ``fake_llm`` (autouse): swaps the real LLM at all three binding sites so no
  test can ever hit a network or need an API key. Tests that want to control
  the response just take the ``fake_llm`` parameter and call ``.script([...])``.
- ``test_app`` / ``client``: the real FastAPI app driven by httpx ASGITransport
  **without** running the heavy lifespan (no MCP/Redis/Postgres startup).
- Seed fixtures: agents, orchestrations, and a real API key (Bearer header).
- ``fake_redis`` + ``scale_app``: an in-memory Redis wired onto app.state so the
  V2 (distributed) endpoints can be exercised with no real infrastructure.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

# ── 1. Sandbox env BEFORE importing core.* ───────────────────────────────────
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="synapse_test_")
os.environ["SYNAPSE_DATA_DIR"] = _TMP_DATA_DIR
# Fake-LLM delay defaults to 0 (instant) when these are unset — see FakeLLM.
# The stress suite sets them to the 5-90s profile. We deliberately do NOT
# setdefault them here, so the stress conftest's fallbacks can take effect.

# ── 2. Make backend/ and tests/ importable ───────────────────────────────────
_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_BACKEND_DIR = _TESTS_DIR.parent
for _p in (str(_BACKEND_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402,F401

from _fakes.fake_llm import FakeLLM  # noqa: E402
from _fakes import seed as _seed  # noqa: E402
from _fakes import fake_redis_stream as _frs  # noqa: E402


# ── per-test data isolation (autouse) ────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_data():
    """Reset the file-backed stores before each test.

    All tests share a single sandbox DATA_DIR, so seeded agents / orchestrations
    / API keys would otherwise leak across tests. Clearing them (and their
    JsonStore caches, which ``save`` updates in place) gives each test a blank
    slate while keeping the fast single-process setup.
    """
    import core.routes.agents as agents_mod
    import core.routes.orchestrations as orch_mod
    agents_mod.save_user_agents([])
    agents_mod.active_agent_id = None
    orch_mod.save_orchestrations([])
    for reset in (
        lambda: __import__("core.api_keys", fromlist=["_save_keys"])._save_keys([]),
        lambda: __import__("core.routes.tools", fromlist=["save_custom_tools"]).save_custom_tools([]),
        lambda: __import__("core.routes.repos", fromlist=["save_repos"]).save_repos([]),
        lambda: __import__("core.routes.db_configs", fromlist=["save_db_configs"]).save_db_configs([]),
    ):
        try:
            reset()
        except Exception:
            pass
    yield


# ── fake LLM (autouse) ───────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Replace ``generate_response`` at every binding site.

    - ``core.llm_providers.generate_response`` — the definition; picked up by
      late importers (orchestration/steps.py, compaction.py, summarizer.py).
    - ``core.react_engine.llm_generate_response`` — bound at module load, so it
      must be patched directly.
    - ``core.routes.agents.llm_generate_response`` — same.
    """
    fake = FakeLLM()
    import core.llm_providers as _llm
    monkeypatch.setattr(_llm, "generate_response", fake, raising=False)
    import core.react_engine as _re
    monkeypatch.setattr(_re, "llm_generate_response", fake, raising=False)
    import core.routes.agents as _agents
    monkeypatch.setattr(_agents, "llm_generate_response", fake, raising=False)
    return fake


# ── app + client ─────────────────────────────────────────────────────────────
@pytest.fixture
def test_app(monkeypatch):
    """The real FastAPI app, with a non-empty agent_sessions so /chat and the
    ReAct loop don't short-circuit with 'No agents connected'. Lifespan is NOT
    run (httpx ASGITransport does not emit lifespan events)."""
    import core.server as server
    monkeypatch.setattr(server, "agent_sessions", {"_test": object()}, raising=False)
    if not hasattr(server, "memory_store"):
        monkeypatch.setattr(server, "memory_store", None, raising=False)
    # The orchestration handlers read the server module off app.state (set during
    # the real lifespan, which we don't run).
    server.app.state.server_module = server
    # Standalone (no scale) by default.
    server.app.state.redis = None
    server.app.state.arq_redis = None
    server.app.state.pg_session_factory = None
    return server.app


@pytest_asyncio.fixture
async def client(test_app):
    """Async httpx client bound to the app via ASGITransport (no live server)."""
    import httpx
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── seed fixtures ────────────────────────────────────────────────────────────
@pytest.fixture
def seed_agent():
    """Seed one agent (default) or accept overrides via the returned factory."""
    def _make(**overrides):
        agent = _seed.make_agent(**overrides)
        _seed.seed_agents([agent])
        return agent
    return _make


@pytest.fixture
def seed_orchestration():
    def _make(**overrides):
        orch = _seed.make_orchestration(**overrides)
        _seed.seed_orchestrations([orch])
        return orch
    return _make


@pytest.fixture
def api_key():
    """A real API key + ready-to-use Authorization header."""
    raw, record = _seed.seed_api_key("test-suite")
    return {"raw": raw, "record": record, "headers": {"Authorization": f"Bearer {raw}"}}


# ── scale / V2 fixtures ──────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def fake_redis():
    r = _frs.new_fake_redis()
    try:
        yield r
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


@pytest.fixture
def scale_app(test_app, fake_redis):
    """Wire an in-memory Redis + mock ARQ + dummy PG factory onto app.state so
    the V2 endpoints pass ``_require_scale_mode`` without real infrastructure.
    Individual tests still patch _create_run_row / rate-limit helpers as needed.
    """
    from unittest.mock import AsyncMock
    test_app.state.redis = fake_redis
    test_app.state.arq_redis = AsyncMock()
    test_app.state.pg_session_factory = object()  # sentinel; PG helpers are mocked per-test
    return test_app
