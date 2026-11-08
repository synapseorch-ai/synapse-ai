"""
The one-time import of a pre-database install.

Existing installs keep their work as JSON files in a data folder. Those files
are what is being removed, so the upgrade has to bring them across without
asking anyone to run a command — an upgrade step that can be missed becomes a
support thread for every self-hoster who misses it.

The properties that matter are all about not losing or duplicating work:
nothing runs when the store already has content, the consumed files are retired
so a second boot does not re-import, and a failure leaves the originals in
place.
"""
import hashlib
import json
from pathlib import Path

import pytest

#: A key an install issued before the upgrade. Assembled rather than inlined so
#: the pre-commit secret scanner does not have to be loosened for a fixture.
LEGACY_API_KEY = "-".join(["sk", "syn", "354a7", "fixture", "key"])
from sqlalchemy import select

from core.store import session
from core.store.importer import (
    import_data_dir,
    import_legacy_data_if_present,
    store_is_empty,
)
from core.store.models import (
    DEFAULT_TENANT,
    AgentDB,
    MCPServerDB,
    OrchestrationDB,
    ScheduleDB,
    SettingDB,
    ToolDB,
)


@pytest.fixture
def data_dir(tmp_path):
    """A DATA_DIR shaped like a real v1.9 install."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "orchestrations.json").write_text(json.dumps([
        {"id": "orch_abc1234", "name": "Nightly report", "description": "runs at 2am",
         "entry_step_id": "s1", "steps": [{"id": "s1", "type": "print"}]},
    ]), encoding="utf-8")
    (root / "user_agents.json").write_text(json.dumps([
        {"id": "agent_1700000000", "name": "Researcher", "model": "gpt-4"},
    ]), encoding="utf-8")
    (root / "custom_tools.json").write_text(json.dumps([
        {"id": "tool_x", "name": "lookup", "code": "return 1"},
    ]), encoding="utf-8")
    (root / "mcp_servers.json").write_text(json.dumps([
        {"name": "github", "label": "GitHub", "url": "https://mcp.example"},
    ]), encoding="utf-8")
    (root / "schedules.json").write_text(json.dumps([
        {"id": "sched_9f1", "name": "Nightly", "enabled": True,
         "target_type": "agent", "target_id": "agent_1700000000", "prompt": "go",
         "schedule_type": "interval", "interval_value": 5, "interval_unit": "minutes",
         "created_at": "2026-01-02T03:04:05Z", "next_run_at": "2026-01-02T03:09:05Z",
         "last_run_at": None},
    ]), encoding="utf-8")
    sessions = root / "chat_sessions"
    sessions.mkdir()
    # Two agents under one session id — two files today, and two rows after.
    for agent in ("agent_a", "agent_b"):
        (sessions / f"{agent}_sess_1.json").write_text(json.dumps({
            "session_id": "sess_1", "agent_id": agent,
            "turns": [{"user": f"hi {agent}", "assistant": "hello",
                       "tools": ["search"], "timestamp": "2026-01-02T03:04:05.000000Z"}],
            "last_response": "hello", "last_updated": "2026-01-02T03:04:05.000000Z",
            "cli_session_ids": {"anthropic_cli": f"cli-{agent}"},
        }), encoding="utf-8")
    (root / "usage_logs.json").write_text(json.dumps([
        {"timestamp": "2026-01-02T03:04:05.123Z", "model": "claude-x", "provider": "anthropic",
         "session_id": "s1", "agent_id": "a1", "source": "chat", "run_id": None,
         "tool_name": None, "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
         "context_chars": 900, "estimated_cost": 0.001, "latency_seconds": 0.5,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "estimated_savings": 0.0,
         "response_cache_hit": False},
        {"timestamp": "2026-01-02T03:05:00.000Z", "event_type": "compaction",
         "source": "compaction", "stage": "trim", "session_id": "s1", "agent_id": "a1",
         "run_id": None, "chars_before": 1000, "chars_after": 400, "chars_saved": 600,
         "reduction_pct": 60, "archive_path": None, "model": "", "input_tokens": 0,
         "output_tokens": 0, "total_tokens": 0, "estimated_cost": 0.0, "latency_seconds": 0.0},
    ]), encoding="utf-8")
    (root / "model_pricing.json").write_text(json.dumps({
        "claude-x": {"provider": "anthropic", "input_per_1m": 7.5, "output_per_1m": 30},
    }), encoding="utf-8")
    mcp_tokens = root / "mcp_tokens"
    mcp_tokens.mkdir()
    (mcp_tokens / "github.json").write_text(json.dumps({
        "access_token": "gho_x", "token_type": "Bearer", "refresh_token": "r1",
    }), encoding="utf-8")
    (mcp_tokens / "github_client.json").write_text(json.dumps({
        "client_id": "cid", "redirect_uris": ["http://localhost:8765/api/mcp/oauth/callback"],
    }), encoding="utf-8")
    (root / "credentials.json").write_text(json.dumps({
        "installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "shh"},
    }), encoding="utf-8")
    (root / "token.json").write_text(json.dumps({
        "token": "ya29.x", "refresh_token": "1//refresh", "email": "user@example.com",
    }), encoding="utf-8")
    (root / "settings.json").write_text(json.dumps({
        "model": "claude-x", "vault_threshold": 5000,
    }), encoding="utf-8")
    # The five peripheral collections. Every one of them holds something whose
    # loss is silent: an API key stops authenticating, a repo disappears from
    # the bash allowlist, a messaging channel stops answering.
    (root / "repos.json").write_text(json.dumps([
        {"id": "repo_1", "name": "Synapse", "path": "/src/synapse"},
    ]), encoding="utf-8")
    (root / "db_configs.json").write_text(json.dumps([
        {"id": "db_1", "name": "Warehouse", "db_type": "postgres",
         "connection_string": "postgresql://u:p@localhost:5432/w"},
    ]), encoding="utf-8")
    (root / "messaging_channels.json").write_text(json.dumps([
        {"id": "chn_1", "name": "TARS", "platform": "telegram", "enabled": True,
         "credentials": {"bot_token": "token"}},
    ]), encoding="utf-8")
    (root / "personal_details.json").write_text(json.dumps({
        "first_name": "Ada", "email": "ada@example.com",
    }), encoding="utf-8")
    # A real hash of a known key, so the test can assert the thing that matters:
    # the key its owner already holds still authenticates after the upgrade.
    (root / "api_keys.json").write_text(json.dumps([
        {"id": "key_1", "name": "internal tools",
         "key_hash": hashlib.sha256(LEGACY_API_KEY.encode()).hexdigest(),
         "key_prefix": "sk-syn-354a7", "is_active": True,
         "created_at": "2026-05-06T01:40:52Z", "last_used_at": "2026-05-06T02:50:05Z"},
    ]), encoding="utf-8")
    return root


async def test_everything_comes_across(data_dir):
    counts = await import_data_dir(data_dir)

    assert counts == {
        "orchestrations": 1, "user_agents": 1, "custom_tools": 1, "mcp_servers": 1,
        "schedules": 1, "chat_sessions": 2, "usage_logs": 2, "model_pricing": 1,
        "google": 2, "mcp_tokens": 1, "settings": 2,
        "repos": 1, "db_configs": 1, "messaging_channels": 1,
        "personal_details": 1, "api_keys": 1,
    }

    async with session() as s:
        orch = (await s.execute(select(OrchestrationDB))).scalar_one()
        agent = (await s.execute(select(AgentDB))).scalar_one()
        tool = (await s.execute(select(ToolDB))).scalar_one()
        mcp = (await s.execute(select(MCPServerDB))).scalar_one()
        sched = (await s.execute(select(ScheduleDB))).scalar_one()
        settings = {r.key: json.loads(r.value)
                    for r in (await s.execute(select(SettingDB))).scalars()}

    assert orch.name == "Nightly report"
    assert orch.definition["steps"][0]["id"] == "s1"   # the whole definition, not a summary
    assert agent.definition["model"] == "gpt-4"
    assert tool.name == "lookup"
    assert mcp.definition["url"] == "https://mcp.example"
    assert settings == {"model": "claude-x", "vault_threshold": 5000}

    # Schedule state is promoted to columns the tick can query, and the
    # original created_at survives — restamping it with the migration time
    # would lose the ordering the JSON file kept by position.
    assert sched.enabled is True
    assert sched.definition["interval_value"] == 5
    assert sched.next_run_at.replace(tzinfo=None).isoformat() == "2026-01-02T03:09:05"
    assert sched.created_at.replace(tzinfo=None).isoformat() == "2026-01-02T03:04:05"

    # Usage history comes across, and a compaction event's own fields land in
    # `details` rather than being dropped for having no column.
    from core.store.models import ModelPricingDB, UsageLogDB

    async with session() as s:
        usage = (await s.execute(
            select(UsageLogDB).order_by(UsageLogDB.timestamp)
        )).scalars().all()
        priced = (await s.execute(select(ModelPricingDB))).scalar_one()

    assert [u.event_type for u in usage] == [None, "compaction"]
    assert usage[0].estimated_cost == 0.001
    assert usage[1].details["chars_saved"] == 600
    # An edited rate card survives; it is not overwritten by the shipped one.
    assert priced.entry["input_per_1m"] == 7.5


async def test_google_credentials_land_as_documents_not_settings(data_dir):
    """The refresh token must not end up in the widely-read settings dict."""
    from core.store.settings import load_settings_for_tenant
    from services import google as google_svc

    await import_data_dir(data_dir)

    assert (await google_svc.load_client_config())["installed"]["client_secret"] == "shh"
    assert (await google_svc.load_token())["refresh_token"] == "1//refresh"

    settings = await load_settings_for_tenant()
    assert "refresh_token" not in json.dumps(settings)


async def test_mcp_tokens_come_across_as_one_document_per_server(data_dir):
    """Two files per server on disk; one row keyed by the server name after."""
    from core.mcp_client import StoreTokenStorage

    await import_data_dir(data_dir)

    storage = StoreTokenStorage("github")
    assert (await storage.get_tokens()).access_token == "gho_x"
    assert (await storage.get_client_info()).client_id == "cid"


async def test_chat_history_comes_across_per_agent(data_dir):
    """Two agents shared one session id on disk, and must still afterwards."""
    from core.session import _get_conversation_history, get_cli_session_id

    await import_data_dir(data_dir)

    a = await _get_conversation_history("sess_1", "agent_a")
    b = await _get_conversation_history("sess_1", "agent_b")

    assert [t["user"] for t in a] == ["hi agent_a"]
    assert [t["user"] for t in b] == ["hi agent_b"]
    assert a[0]["tools"] == ["search"]
    assert await get_cli_session_id("sess_1", "agent_a", "anthropic_cli") == "cli-agent_a"


async def test_imported_rows_land_on_the_single_tenant(data_dir):
    await import_data_dir(data_dir)
    async with session() as s:
        for model in (OrchestrationDB, AgentDB, ToolDB, MCPServerDB, ScheduleDB):
            row = (await s.execute(select(model))).scalar_one()
            assert row.tenant_id == DEFAULT_TENANT


async def test_orchestrations_are_active(data_dir):
    """NULL is_active is not true.

    The engine counts `WHERE is_active`, so an imported orchestration would
    exist, list fine, and be invisible on the screens that count it — the
    workspace reporting itself empty while holding the user's work.
    """
    await import_data_dir(data_dir)
    async with session() as s:
        assert (await s.execute(select(OrchestrationDB))).scalar_one().is_active is True


async def test_the_peripheral_collections_read_back_through_their_own_loaders(data_dir):
    """Imported through the store, read back through the code that serves them.

    These five were the last collections to move, and the importer was never
    extended for them — so an upgrade silently dropped every API key, linked
    repo, database connection, messaging channel and the personal details
    document. Each loss is invisible until something fails much later: a key
    stops authenticating with no log line explaining it, a bot goes quiet.

    Asserting through `collections.load` rather than against rows is the point:
    the importer keys these itself, and keying them differently from the live
    CRUD path would store them somewhere nothing reads.
    """
    from core.store import collections

    await import_data_dir(data_dir)

    assert [r["path"] for r in await collections.load("repos")] == ["/src/synapse"]
    assert [c["name"] for c in await collections.load("db_configs")] == ["Warehouse"]

    channels = await collections.load("messaging_channels")
    assert channels[0]["credentials"]["bot_token"] == "token"

    assert (await collections.load_one("personal_details"))["email"] == "ada@example.com"


async def test_an_imported_api_key_still_authenticates(data_dir):
    """The key a user issued before the upgrade keeps working.

    Only the hash was ever stored, so this imports losslessly — and it has to,
    because the user cannot be handed their key again to re-enter it.
    """
    from core.api_keys import list_api_keys, validate_api_key

    await import_data_dir(data_dir)

    keys = await list_api_keys()
    assert [k["name"] for k in keys] == ["internal tools"]
    assert keys[0]["key_prefix"] == "sk-syn-354a7"

    # The assertion the whole thing is for: the raw key its owner still has in
    # a script somewhere authenticates against the imported row.
    record = await validate_api_key(LEGACY_API_KEY)
    assert record is not None, "an API key stopped working across the upgrade"
    assert record["name"] == "internal tools"


# ── running it at boot ───────────────────────────────────────────────────────
#
# `import_legacy_data_if_present()` takes no argument: it finds the folder
# itself, because after this refactor nothing else in the engine knows where
# that folder is. `SYNAPSE_DATA_DIR` is its first candidate, read for detection
# only, which is what these tests drive.


@pytest.fixture
def legacy_install(data_dir, monkeypatch):
    """A v1.9 install the boot-time importer will discover on its own."""
    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(data_dir))
    return data_dir


async def test_first_boot_imports_and_renames(legacy_install):
    counts = await import_legacy_data_if_present()

    assert counts is not None
    assert not legacy_install.exists()
    assert (legacy_install.parent / "data.migrated").is_dir()
    # The originals survive the move — nothing is deleted.
    assert (legacy_install.parent / "data.migrated" / "orchestrations.json").is_file()


async def test_second_boot_does_nothing(legacy_install):
    await import_legacy_data_if_present()
    again = await import_legacy_data_if_present()
    assert again is None


async def test_a_folder_holding_live_state_is_not_renamed(legacy_install):
    """The container case: one volume holds the legacy JSON *and* the database.

    Renaming the folder would move `synapse.db` with it — and the import has
    just written into that file, so the rename would destroy exactly what it
    produced. Only the consumed files move.
    """
    (legacy_install / "synapse.db").write_text("", encoding="utf-8")

    counts = await import_legacy_data_if_present()

    assert counts is not None
    assert legacy_install.is_dir()
    assert (legacy_install / "synapse.db").is_file()
    assert (legacy_install / "migrated" / "orchestrations.json").is_file()
    assert not (legacy_install / "orchestrations.json").exists()
    # And that is enough to stop a second import even before store_is_empty().
    assert await import_legacy_data_if_present() is None


async def test_a_populated_store_is_never_overwritten(legacy_install):
    """The guard that protects work done since the upgrade.

    If the rename failed on the first boot, or the folder was restored from a
    backup, a second import must not clobber what is already there.
    """
    async with session() as s:
        s.add(OrchestrationDB(id="orch_abc1234", tenant_id=DEFAULT_TENANT,
                              name="Edited since upgrade", definition={"v": 2}))
        await s.commit()

    assert await import_legacy_data_if_present() is None
    assert legacy_install.exists()    # left in place for a human to look at

    async with session() as s:
        row = (await s.execute(select(OrchestrationDB))).scalar_one()
    assert row.name == "Edited since upgrade"


async def test_nothing_happens_without_a_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(tmp_path / "absent"))
    assert await import_legacy_data_if_present() is None

    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(tmp_path))   # exists, but empty
    assert await import_legacy_data_if_present() is None


async def test_the_npm_layout_is_a_candidate(data_dir, monkeypatch):
    """`~/.synapse/data` is where the npm launcher put it.

    It only ever worked because that launcher passed SYNAPSE_DATA_DIR into the
    backend's environment. That is gone, so without this candidate an npm
    install's upgrade would silently import nothing.
    """
    monkeypatch.delenv("SYNAPSE_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: data_dir.parent))
    (data_dir.parent / ".synapse").mkdir()
    data_dir.rename(data_dir.parent / ".synapse" / "data")

    assert await import_legacy_data_if_present() is not None


async def test_a_corrupt_file_does_not_stop_the_rest(data_dir):
    """A half-written JSON file must not cost the user everything else."""
    (data_dir / "user_agents.json").write_text("{not json", encoding="utf-8")

    counts = await import_data_dir(data_dir)

    assert counts["user_agents"] == 0
    assert counts["orchestrations"] == 1
    assert counts["mcp_servers"] == 1


async def test_settings_alone_do_not_count_as_a_populated_store(tmp_path):
    """A fresh install writes settings before it has any content.

    Treating that as "already populated" would skip the import and strand the
    user's workflows.
    """
    async with session() as s:
        s.add(SettingDB(tenant_id=DEFAULT_TENANT, key="model", value='"x"'))
        await s.commit()

    assert await store_is_empty() is True


# ── A v1.9 workflow the v2 engine refuses (F1235) ──────────────────────────────
# v1.9's builder saved a step's fields as the model wrote them — a human step's
# form fields as `{"fields": [...]}` (HOT2's prod step) — and v2's engine will
# not run that step. The import is the one time every upgrader's documents pass
# through code, so each is read into the engine's shape there, once.

V19_HUMAN = {"id": "keys", "name": "Generate API Keys", "type": "human", "human_prompt": "Your API keys?",
             "human_fields": {"fields": [{"name": "api_key", "type": "str"}]}, "output_key": "keys",
             "next_step_id": "done"}
V19_END = {"id": "done", "name": "Done", "type": "end"}


async def _imported(data_dir, doc: dict) -> dict:
    (data_dir / "orchestrations.json").write_text(json.dumps([doc]), encoding="utf-8")
    await import_data_dir(data_dir)
    async with session() as s:
        return (await s.execute(select(OrchestrationDB))).scalar_one().definition


async def test_a_v1_9_step_in_another_shape_comes_across_in_the_engines(data_dir):
    from core.models_orchestration import Orchestration

    doc = {"id": "orch_h", "name": "Weekly post", "entry_step_id": "keys", "steps": [V19_HUMAN, V19_END]}
    with pytest.raises(ValueError):
        Orchestration.model_validate(doc)

    definition = await _imported(data_dir, doc)

    Orchestration.model_validate(definition)
    assert definition["steps"][0]["human_fields"] == [{"name": "api_key", "type": "str"}]
    assert {k: v for k, v in definition.items() if k != "steps"} == {k: v for k, v in doc.items() if k != "steps"}


async def test_a_workflow_already_in_the_engines_shape_comes_across_as_it_was(data_dir):
    human = {**V19_HUMAN, "human_fields": [{"name": "api_key", "type": "str"}]}
    doc = {"id": "orch_h", "name": "Weekly post", "entry_step_id": "keys", "steps": [human, V19_END]}
    assert await _imported(data_dir, doc) == doc


async def test_a_workflow_whose_steps_are_not_a_list_comes_across_as_it_was(data_dir):
    doc = {"id": "orch_h", "name": "Weekly post", "steps": {"s1": {"type": "print"}}}
    assert await _imported(data_dir, doc) == doc
