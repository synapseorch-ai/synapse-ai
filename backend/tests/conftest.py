"""
Root test harness for the Synapse backend suite.

No module binds a directory at import any more, so there is no ordering
constraint here to respect: isolation is per test, in ``_isolate_store`` below,
which points the database, the blob store, scratch and the state dir at that
test's ``tmp_path``. This file used to open by setting ``SYNAPSE_DATA_DIR``
before the first ``core.*`` import, because several modules computed paths from
it at module scope. That is what the store replaced.

What this provides
------------------
- Full isolation from a developer's real install, per test.
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

import pathlib
import sys

# Fake-LLM delay defaults to 0 (instant) when its env vars are unset — see
# FakeLLM. The stress suite sets them to the 5-90s profile. We deliberately do
# NOT setdefault them here, so the stress conftest's fallbacks can take effect.

# ── Make backend/, tests/ and the repo root importable ───────────────────────
# The repo root is what makes `synapse.cli` importable. Without it the CLI tests
# skip themselves, and a skip that nobody notices is how `synapse api-keys`
# stayed broken for a release.
_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_BACKEND_DIR = _TESTS_DIR.parent
_REPO_ROOT = _BACKEND_DIR.parent
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR), str(_TESTS_DIR)):
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
    """Reset what is still file-backed, and the process-global active agent.

    The four resource collections no longer need clearing: they live in the
    store, and `_isolate_store` below gives every test a fresh empty database.
    What is left here is the file-backed remainder, plus `active_agent_id`,
    which is a module global rather than stored state.
    """
    import core.routes.agents as agents_mod
    agents_mod.active_agent_id = None
    yield


# ── run-event journal isolation (autouse) ────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_run_journals(tmp_path, monkeypatch):
    """Clear the shared journal registry between tests.

    The journal's directory is scratch, which `_isolate_store` already points
    at tmp_path — so what is left to isolate is the registry itself: shared
    instances cache an open file handle and a sequence number, and neither may
    survive into the next test.
    """
    import core.orchestration.journal as journal_mod
    journal_mod._journals.clear()
    yield
    for run_id in list(journal_mod._journals):
        journal_mod.close_journal(run_id)


# ── run checkpoint isolation (autouse) ───────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_run_checkpoints(tmp_path, monkeypatch):
    """Give every test its own empty store for run state.

    Run checkpoints used to be JSON files under backend/logs/orchestration_runs
    and were isolated by pointing RUNS_DIR at a tmp_path. They are rows in the
    store now, so isolation means a fresh database per test — otherwise the
    first test to run the engine leaves rows that the next test's
    ``list_runs()`` picks up.

    A file in tmp_path rather than ``:memory:``, deliberately. An in-memory
    database has to be held open by a single pooled connection for its whole
    life, and aiosqlite services that connection from a worker thread bound to
    the test's event loop. Once the loop closes the thread dies raising
    "Event loop is closed" — reported by pytest against whichever *later*
    test happened to be running. With a file, each session opens and closes its
    own connection, so no thread outlives the test that created it.
    """
    import core.orchestration.state as state_mod
    import core.storage.base as blob_mod
    import core.store.engine as store_mod

    monkeypatch.setenv("SYNAPSE_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(store_mod, "_engine", None)
    monkeypatch.setattr(store_mod, "_session_factory", None)
    monkeypatch.setattr(state_mod, "_backend", None)

    # Blobs — the vault and the response/tool caches — need the same treatment.
    # Without it a cached answer written by one test is a *hit* in the next,
    # and worse, the default blob directory is inside the repo, so the suite
    # would slowly fill a developer's working tree with cached LLM responses.
    monkeypatch.setenv("SYNAPSE_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setattr(blob_mod, "_store", None)
    # A developer's own bucket would otherwise become every test's blob store
    # wherever a process installs the configured one (F57).
    monkeypatch.delenv("S3_BUCKET", raising=False)

    # Scratch too — logs and run-event journals are appended there before being
    # published, and the default sits inside the repo.
    monkeypatch.setenv("SYNAPSE_SCRATCH_DIR", str(tmp_path / "scratch"))

    # And the state dir, which is ChromaDB's index. Its default is also inside
    # the repo, so without this a test that touches memory leaves a vector
    # store in the developer's working tree — the same way the suite used to
    # slowly fill backend/data.
    monkeypatch.setenv("SYNAPSE_STATE_DIR", str(tmp_path / "state"))

    # And the install's home, which a packaged install's launcher sets and every
    # default above falls back to (core/paths.py). A shell that exported it for
    # a real install would otherwise lend that install's .env to the suite.
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))

    # The store's read-through cache is a module global, so it outlives the
    # database that tmp_path just replaced.
    import core.store.cache as cache_mod
    cache_mod.invalidate_all()

    # And so does the settings snapshot, which is cached per tenant for 30
    # seconds. Without this, a test that writes a setting is still being
    # answered to the *next* tests in the process — against their own empty
    # databases — for as long as the TTL lasts. That is ordering- and
    # speed-dependent, which is the worst way to find out.
    import core.settings_runtime as settings_mod
    settings_mod.invalidate()
    settings_mod._current.set(None)


# ── notification store isolation (autouse) ───────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_notifications(tmp_path, monkeypatch):
    """Reset the module-level notification hub's in-memory rings.

    Notifications are a blob now, and `_isolate_store` gives each test its own
    blob directory — but the hub is a module singleton constructed at import,
    so its in-memory rings (one per tenant, F337) outlive that.
    """
    import core.notifications as notif_mod
    notif_mod.hub._rings.clear()
    notif_mod.hub._subs.clear()
    notif_mod.hub._orch_names.clear()
    notif_mod.hub._terminal_notified.clear()


# ── settings provider isolation (autouse) ────────────────────────────────────
@pytest.fixture(autouse=True)
def _restore_settings_provider():
    """Put back the settings provider each test found (F816, BF11).

    `core/server.py` installs it at import, once per process. A test that set it to None
    on the way out (test_shared_fleet's fleet did) left every later test that relied on that
    import on bare defaults while its writes went to the store: the semantic cache's tests
    then read no Embedding Model, 6 of 13 failing after test_mcp_boot_retry + test_shared_fleet.
    F91 met the same class and `test_app` reinstalls; this covers every test, not one fixture."""
    from core.config import get_settings_provider, set_settings_provider

    before = get_settings_provider()
    yield
    set_settings_provider(before)


# ── model CLI status isolation (autouse) ─────────────────────────────────────
@pytest.fixture(autouse=True)
def _fresh_claims():
    """Every test spends confirm tokens and approved reviews in a store of its own (F778): a call one test's approval
    made is not already made in the next."""
    from core.product_tools import confirm

    confirm.set_claim_store(None)
    yield
    confirm.set_claim_store(None)


@pytest.fixture(autouse=True)
def _no_model_list():
    """No test reads this machine's providers for the models a workspace can pick (`core.model_choices`, SAI2-P2):
    the list is a host's here, and a test that is about it sets one. With none, nothing is refused for its model."""
    from core import model_choices

    async def nothing():
        return {}

    model_choices.set_model_choices(nothing)
    yield
    model_choices.set_model_choices(None)


@pytest.fixture(autouse=True)
def _isolate_cli_status():
    """Forget what the model CLIs said (CLIM1): their status is cached for ten
    minutes per binary, and a test's faked `which` must not answer for the next."""
    import core.providers.cli_status as cli_status
    cli_status.clear_cache()
    yield
    cli_status.clear_cache()


# ── Knowledge seam isolation (autouse) ───────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_knowledge_seams():
    """Every Knowledge seam, the Vault observer and the lifecycle observers back
    to their defaults around each test, so one test's fake never serves the next."""
    from core import vault_events
    from core.knowledge import costs, events, rerankers, seams, web
    from core.knowledge import fetch as knowledge_fetch

    seams.reset_all()
    rerankers.reset_breakers()
    costs.set_platform_pricer(None)
    web.set_crawl_runner(None)
    web.set_vault_room(None)
    knowledge_fetch.set_proxy_required(False)
    knowledge_fetch.set_pacer(None)
    vault_events.set_vault_observer(None)
    events.knowledge_observers.clear()
    yield
    seams.reset_all()
    rerankers.reset_breakers()
    costs.set_platform_pricer(None)
    web.set_crawl_runner(None)
    web.set_vault_room(None)
    knowledge_fetch.set_proxy_required(False)
    knowledge_fetch.set_pacer(None)
    vault_events.set_vault_observer(None)
    events.knowledge_observers.clear()


@pytest.fixture(autouse=True)
def _isolate_process_seams():
    """A worker's vector store (F393), a host's run images (F395), its run gate
    (F419) and its spend reader (CAP1) back to their defaults after each test:
    building a worker module sets the first."""
    from core.cache import response_cache
    from core.orchestration import run_cap, run_gate, run_images

    yield
    response_cache.set_memory_store(None)
    run_images.set_run_images(None)
    run_gate.set_run_gate(None)
    run_cap.set_spend_reader(None)


# ── chat turn isolation (autouse) ────────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _isolate_chat_turns(_isolate_run_checkpoints):
    """No chat turn outlives its test.

    A turn runs in its own task now (F49), so one a test left running would
    still be writing to a store the next test's fixtures have replaced — or to a
    loop that has closed. Torn down before the store fixture it depends on.
    """
    import asyncio
    import contextlib

    from core import chat_turns

    chat_turns._TURNS.clear()
    yield
    leftovers = [t.task for t in chat_turns._TURNS.values()
                 if t.task is not None and not t.task.done()]
    for task in leftovers:
        task.cancel()
    for task in leftovers:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)
    chat_turns._TURNS.clear()


@pytest_asyncio.fixture(autouse=True)
async def _isolate_helpers(_isolate_chat_turns):
    """No helper's result is still on its way back when its test ends (SUB1a).

    A background helper is a chat turn (`_isolate_chat_turns` ends it); its result
    goes back in a task of its own, which could start a follow-up turn on a store
    the next test has replaced. Torn down before the chat turns.
    """
    import asyncio
    import contextlib

    from core import chat_turns, helpers, team

    def clear():
        for registry in (helpers._LIVE, helpers._OPEN, helpers._CHANGED, helpers._BACKGROUND, helpers._LOCKS,
                         helpers._QUEUED, helpers._COURIERS, chat_turns._FREE, team._LIVE):
            registry.clear()
        helpers._DELIVERED.clear()
        helpers.uninstall()  # a host one test installed (SUB1b) never reaches the next

    clear()
    yield
    leftovers = [t for t in helpers._DELIVERING if not t.done()]
    for task in leftovers:
        task.cancel()
    for task in leftovers:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)
    helpers._DELIVERING.clear()
    clear()


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
    # The lifespan we skip installs the settings provider (server.py does it at
    # import too, for this very reason) — but that fires once per process, so a
    # test that discarded the provider left every later settings read on bare
    # defaults while the writes went to the store (F91). Installing is a
    # function-pointer assignment: no I/O, no loop, no store.
    from core import settings_runtime
    settings_runtime.install_provider()
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


@pytest_asyncio.fixture
async def live_server(test_app, tmp_path):
    """The same app behind a real uvicorn server on this test's loop.

    For what ``client`` cannot show: a client that hangs up mid-stream (F49).
    Set up through ``client`` or the store; stream through ``live_server.client()``.
    """
    import os

    from _fakes.live_server import serve

    assert str(tmp_path) in os.environ.get("SYNAPSE_DB_URL", ""), (
        "the live server must run against this test's store, never backend/var"
    )
    async with serve(test_app) as live:
        yield live


@pytest.fixture
def gated_llm(monkeypatch):
    """A model call that parks until the test lets it go.

    Patched at the same three sites as ``fake_llm``. ``started`` is set when a
    call arrives, ``release`` lets every waiting call answer ``answer``; the
    gate is opened at teardown so nothing is left parked on a closed loop.
    """
    import asyncio
    import types

    gate = types.SimpleNamespace(
        started=asyncio.Event(), release=asyncio.Event(),
        answer="The answer, after the gate.", calls=0,
    )

    async def _gated(*args, **kwargs):
        gate.calls += 1
        gate.started.set()
        await gate.release.wait()
        return gate.answer

    import core.llm_providers as _llm
    monkeypatch.setattr(_llm, "generate_response", _gated, raising=False)
    import core.react_engine as _re
    monkeypatch.setattr(_re, "llm_generate_response", _gated, raising=False)
    import core.routes.agents as _agents
    monkeypatch.setattr(_agents, "llm_generate_response", _gated, raising=False)
    yield gate
    gate.release.set()


# ── seed fixtures ────────────────────────────────────────────────────────────
@pytest.fixture
def seed_agent():
    """Seed one agent (default) or accept overrides via the returned factory.

    The factory is async because the store is: `agent = await seed_agent(...)`.
    Every test taking this fixture is already an `async def`.
    """
    async def _make(**overrides):
        agent = _seed.make_agent(**overrides)
        await _seed.seed_agents([agent])
        return agent
    return _make


@pytest.fixture
def seed_orchestration():
    async def _make(**overrides):
        orch = _seed.make_orchestration(**overrides)
        await _seed.seed_orchestrations([orch])
        return orch
    return _make


@pytest_asyncio.fixture
async def api_key():
    """A real API key + ready-to-use Authorization header."""
    raw, record = await _seed.seed_api_key("test-suite")
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
