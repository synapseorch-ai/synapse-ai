"""
A v1.x install spread across several folders comes across whole.

v1.x wrote to two kinds of place: its data folder (`SYNAPSE_DATA_DIR`, or
`~/.synapse/data` for pip and npm) and, whatever that said, `backend/data`
beside the code for chats, `schedules.json` and part of the vault. The importer
used to take the first folder holding any JSON, so a pip install with one
schedule imported that schedule and stranded its agents and settings.

Also here: the files beside the JSON (vault, datasets, WhatsApp sessions), and
the "store is empty" guard, which counted the builder the product seeds on
every boot and so refused every import after the first start.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

import core.store.importer as importer
from core.store import session
from core.store.models import AgentDB, ChatSessionDB, ScheduleDB, SettingDB


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _chat(root: Path, session_id: str, agent: str, text: str) -> None:
    _write(root / "chat_sessions" / f"{agent}_{session_id}.json", {
        "session_id": session_id, "agent_id": agent,
        "turns": [{"user": text, "assistant": "ok"}],
        "last_updated": "2026-01-02T03:04:05Z",
    })


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """No candidate anywhere real: home and the code-relative folder are tmp."""
    home = tmp_path / "home"
    code_data = tmp_path / "site-packages" / "backend" / "data"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(importer, "_code_data_dir", lambda: code_data)
    monkeypatch.delenv("SYNAPSE_DATA_DIR", raising=False)
    return {"home_data": home / ".synapse" / "data", "code_data": code_data,
            "tmp": tmp_path}


async def test_a_pip_install_split_across_two_folders_comes_across_whole(layout):
    """Settings and agents in ~/.synapse/data; schedules and chats beside the code."""
    home_data, code_data = layout["home_data"], layout["code_data"]
    _write(home_data / "settings.json", {"model": "gpt-4o"})
    _write(home_data / "user_agents.json", [{"id": "agent_mine", "name": "Mine"}])
    _write(code_data / "schedules.json", [{
        "id": "sched_1", "name": "Nightly", "enabled": True, "target_type": "agent",
        "target_id": "agent_mine", "prompt": "go", "schedule_type": "interval",
        "interval_value": 5, "interval_unit": "minutes",
    }])
    _chat(code_data, "sess_1", "agent_mine", "hello")

    counts = await importer.import_legacy_data_if_present()

    assert counts["user_agents"] == 1
    assert counts["settings"] == 1
    assert counts["schedules"] == 1
    assert counts["chat_sessions"] == 1
    async with session() as s:
        assert (await s.execute(select(AgentDB.id))).scalars().all() == ["agent_mine"]
        assert (await s.execute(select(ScheduleDB.id))).scalars().all() == ["sched_1"]
        assert (await s.execute(select(SettingDB.key))).scalars().all() == ["model"]
        assert len((await s.execute(select(ChatSessionDB))).scalars().all()) == 1
    # Both folders are retired, so a second boot imports nothing.
    assert not home_data.exists() and not code_data.exists()
    assert await importer.import_legacy_data_if_present() is None


async def test_a_folder_of_only_chats_is_a_candidate(layout):
    _chat(layout["code_data"], "sess_1", "agent_a", "hi")
    assert importer._legacy_data_dirs() == [layout["code_data"]]


async def test_the_first_named_folder_wins_on_a_clash(layout):
    """`SYNAPSE_DATA_DIR` is what the install ran with; a stale copy loses."""
    real, stale = layout["tmp"] / "real", layout["home_data"]
    _write(real / "user_agents.json", [{"id": "agent_x", "name": "Current"}])
    _write(stale / "user_agents.json", [{"id": "agent_x", "name": "Stale"}])
    _chat(real, "s", "agent_x", "current")
    _chat(stale, "s", "agent_x", "stale")

    reports = await importer.import_sources([real, stale])

    assert [r["path"] for r in reports] == [str(real), str(stale)]
    async with session() as s:
        agent = (await s.execute(select(AgentDB))).scalar_one()
        chat = (await s.execute(select(ChatSessionDB))).scalar_one()
    assert agent.name == "Current"
    assert chat.messages[0]["content"] == "current"


async def test_one_failed_folder_does_not_cost_the_others(layout, monkeypatch):
    good, bad = layout["tmp"] / "good", layout["tmp"] / "bad"
    _write(good / "user_agents.json", [{"id": "agent_ok", "name": "Ok"}])
    _write(bad / "user_agents.json", [{"id": "agent_no", "name": "No"}])
    real_import = importer.import_data_dir

    async def flaky(root, tenant_id=importer.DEFAULT_TENANT):
        if Path(root) == bad:
            raise RuntimeError("disk said no")
        return await real_import(root, tenant_id)

    monkeypatch.setattr(importer, "import_data_dir", flaky)

    reports = await importer.import_sources([good, bad])

    assert reports[1] == {"path": str(bad), "error": "RuntimeError: disk said no"}
    assert bad.is_dir(), "a failed folder is left where it was"
    assert not good.exists()
    async with session() as s:
        assert (await s.execute(select(AgentDB.id))).scalars().all() == ["agent_ok"]


async def test_the_seeded_builder_and_default_agent_leave_the_store_empty():
    from core.default_agent import ensure_default_agent
    from core.native_builder.seed import seed_native_builder

    await seed_native_builder()
    assert await ensure_default_agent() is True
    assert await importer.store_is_empty() is True

    async with session() as s:
        s.add(AgentDB(id="agent_users_own", tenant_id="default", name="Mine",
                      definition={"id": "agent_users_own"}))
        await s.commit()
    assert await importer.store_is_empty() is False


async def test_an_import_after_the_builder_seeded_still_runs(layout):
    """The forgotten-mount case: the first v2 boot saw no legacy folder."""
    from core.native_builder.seed import seed_native_builder

    await seed_native_builder()
    _write(layout["home_data"] / "user_agents.json", [{"id": "agent_late", "name": "Late"}])

    counts = await importer.import_legacy_data_if_present()

    assert counts is not None and counts["user_agents"] == 1


def test_vault_datasets_and_whatsapp_files_land_under_their_blob_keys(tmp_path):
    from core.storage import get_blob_store
    from core.store.legacy_files import import_legacy_files

    root = tmp_path / "data"
    binary = bytes(range(256))
    (root / "vault" / "tool_outputs").mkdir(parents=True)
    (root / "vault" / "tool_outputs" / "page.md").write_text("# saved", encoding="utf-8")
    (root / "vault" / "chart.png").write_bytes(binary)
    (root / "datasets").mkdir()
    (root / "datasets" / "qa.jsonl").write_text('{"q": 1}\n', encoding="utf-8")
    (root / "datasets" / "notes.txt").write_text("not a dataset", encoding="utf-8")
    profile = root / "whatsapp_sessions" / "chn_1" / "Default"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_bytes(b"\x00cookie")
    if hasattr(os, "symlink"):
        try:
            os.symlink("host-123", root / "whatsapp_sessions" / "chn_1" / "SingletonLock")
        except OSError:
            pass

    counts = import_legacy_files(root)

    assert counts == {"vault": 2, "datasets": 1, "whatsapp_sessions": 1, "skipped": 0}
    store = get_blob_store()
    assert store.get("vault/tool_outputs/page.md") == "# saved"
    assert store.path_for("vault/chart.png").read_bytes() == binary
    assert store.get("datasets/qa.jsonl") == '{"q": 1}\n'
    assert not store.exists("datasets/notes.txt")
    assert store.path_for("runtime/whatsapp_sessions/chn_1/Default/Cookies").read_bytes() \
        == b"\x00cookie"
    assert not store.path_for("runtime/whatsapp_sessions/chn_1/SingletonLock").exists()


def test_a_file_the_store_already_has_is_not_overwritten(tmp_path):
    from core.storage import get_blob_store
    from core.store.legacy_files import import_legacy_files

    get_blob_store().put("vault/notes.md", "edited since the upgrade")
    (tmp_path / "data" / "vault").mkdir(parents=True)
    (tmp_path / "data" / "vault" / "notes.md").write_text("old", encoding="utf-8")

    assert import_legacy_files(tmp_path / "data")["vault"] == 0
    assert get_blob_store().get("vault/notes.md") == "edited since the upgrade"


async def test_the_container_volume_keeps_its_database_and_retires_the_trees(layout, monkeypatch):
    """/data holds the legacy JSON, synapse.db and blobs/ at once."""
    volume = layout["tmp"] / "volume"
    _write(volume / "user_agents.json", [{"id": "agent_c", "name": "C"}])
    (volume / "vault").mkdir()
    (volume / "vault" / "a.md").write_text("a", encoding="utf-8")
    (volume / "synapse.db").write_text("", encoding="utf-8")
    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(volume))

    counts = await importer.import_legacy_data_if_present()

    assert counts["user_agents"] == 1 and counts["vault"] == 1
    assert (volume / "synapse.db").is_file()
    assert (volume / "migrated" / "user_agents.json").is_file()
    assert (volume / "migrated" / "vault" / "a.md").is_file()
    assert not (volume / "vault").exists()


def test_migrate_from_and_logs_print_one_json_report(tmp_path, monkeypatch, capsys):
    """What the installer reads: stdout is the JSON and nothing else."""
    import sys

    cli = pytest.importorskip("synapse.cli")
    data, logs = tmp_path / "old" / "data", tmp_path / "old" / "logs"
    _write(data / "user_agents.json", [{"id": "agent_j", "name": "J"}])
    (logs / "orchestration_logs").mkdir(parents=True)
    (logs / "orchestration_logs" / "run_1.log").write_text("line\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["synapse", "migrate", "--from", str(data),
                                      "--logs", str(logs), "--json"])

    cli.main()

    report = json.loads(capsys.readouterr().out)
    assert report["database"] == "ok"
    assert report["sources"][0]["counts"]["user_agents"] == 1
    assert report["logs"][0]["counts"]["orchestration_logs"] == 1
    assert report["totals"]["user_agents"] == 1
    assert not data.exists() and (tmp_path / "old" / "data.migrated").is_dir()
    assert (tmp_path / "old" / "logs.migrated").is_dir()


def test_migrate_json_exits_non_zero_when_a_folder_fails(tmp_path, monkeypatch, capsys):
    import sys

    cli = pytest.importorskip("synapse.cli")
    bad = tmp_path / "bad"
    _write(bad / "user_agents.json", [{"id": "agent_b", "name": "B"}])

    async def boom(root, tenant_id="default"):
        raise RuntimeError("nope")

    monkeypatch.setattr(importer, "import_data_dir", boom)
    monkeypatch.setattr(sys, "argv", ["synapse", "migrate", "--from", str(bad), "--json"])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["sources"][0]["error"] == "RuntimeError: nope"
    assert bad.is_dir()
