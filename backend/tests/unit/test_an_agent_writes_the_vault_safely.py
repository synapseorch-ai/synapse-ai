"""An agent writes a text file into the Vault, safely, where its host offers the write (F1289; Naveen 11-02, 79).

Cloud's fleet serves only the Vault's reads (`core.vault_tools`): the sandbox server that holds `vault_write` is not
there (T8), so a chart (F1288) could never be saved. Now `core.vault_tools` offers `vault_write(path, content)`, by the
sandbox's own name, wherever a host installs a writer (`set_writer`); self-hosted, the sandbox's stays the one. Before
any writer, the engine refuses:

* a path that is not a Vault path (`..`, a leading `/`, a backslash, control characters);
* the engine's own folders (tool outputs, the browser's, compacted conversations);
* anything but plain text and the tables the Vault draws: .json .md .txt .csv .tsv; a NUL; more than 5 MB;
* a knowledge base's folder, on or off: its documents change through `edit_knowledge_document`, with its checks and
  its undo, never past them (79: no knowledge poisoning by a write).

`engine_writer` writes through the engine's blob store, asks the host's room first (`step_refs`' write hook, the plan's
Vault cap in cloud), and tells whoever follows the Vault. The blob store keeps no metadata, so it creates or writes
over: on a self-hosted install the Vault is the person's own (cloud's writer overwrites only what it wrote).
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import step_refs, vault_tools
from core.storage import set_blob_store
from core.storage.base import LocalBlobStore


@pytest.fixture
def vault(tmp_path):
    store = LocalBlobStore(tmp_path)
    set_blob_store(store)
    vault_tools.set_writer(vault_tools.engine_writer)
    yield store
    vault_tools.set_writer(None)
    step_refs.set_write_hook(None)
    set_blob_store(None)


@pytest.fixture
def told(monkeypatch):
    changes: list = []

    async def notify(path, op, **kw):
        changes.append((path, op, kw.get("source")))

    import core.vault_events as events

    monkeypatch.setattr(events, "notify_vault_change", notify)
    return changes


async def _write(path: str, content: str) -> dict:
    out = await vault_tools.call("vault_write", {"path": path, "content": content})
    return json.loads(out[0].text)


def _names() -> set[str]:
    return {t.name for t in vault_tools.tools()}


class TestOfferedWhereAHostWrites:
    def test_listed_only_with_a_writer(self, vault):
        assert "vault_write" in _names()
        vault_tools.set_writer(None)
        assert "vault_write" not in _names(), "self-hosted, the sandbox's vault_write is the one"
        assert {"list_files", "read_file", "search_files", "read_json"} <= _names()

    def test_it_says_it_writes(self):
        vault_tools.set_writer(vault_tools.engine_writer)
        try:
            [tool] = [t for t in vault_tools.tools() if t.name == "vault_write"]
        finally:
            vault_tools.set_writer(None)
        assert tool.annotations.readOnlyHint is False, "a write: the person is asked first (F964)"
        assert set(tool.inputSchema["required"]) == {"path", "content"}


class TestTheEnginesWrite:
    async def test_a_chart_csv_is_written_counted_and_told(self, vault, told):
        grew: list = []
        step_refs.set_write_hook(grew.append)
        said = await _write("reports/sales-by-region.csv", "region,total\nNorth,41200\n")
        assert "error" not in said, said
        assert vault.get("vault/reports/sales-by-region.csv") == "region,total\nNorth,41200\n"
        assert grew == [len("region,total\nNorth,41200\n")], "the host's room is asked first"
        assert told == [("reports/sales-by-region.csv", "write", "tool")]

    async def test_each_run_writes_its_chart_over_the_last(self, vault, told):
        await _write("reports/sales.csv", "region,total\nNorth,1\n")
        await _write("reports/sales.csv", "region,total\nNorth,2\n")
        assert vault.get("vault/reports/sales.csv") == "region,total\nNorth,2\n"

    async def test_no_room_is_no_write(self, vault, told):
        def full(growth):
            raise RuntimeError("The Vault is full on this plan.")

        step_refs.set_write_hook(full)
        said = await _write("reports/sales.csv", "region,total\n")
        assert "full" in said["error"] and vault.get("vault/reports/sales.csv") is None and told == []


class TestWhatIsRefused:
    @pytest.mark.parametrize("path, says", [
        ("../escape.csv", "Vault path"), ("/abs.csv", "Vault path"), ("a\\b.csv", "Vault path"),
        ("tool_outputs/x.csv", "engine"), ("playwright/run.md", "engine"), ("compaction_archives/c.md", "engine"),
        ("reports/run.py", ".csv"), ("reports/page.html", ".csv"), ("reports/noext", ".csv"),
    ])
    async def test_a_path_or_type_it_never_writes(self, vault, told, path, says):
        said = await _write(path, "x\n")
        assert says in said["error"], said
        assert told == []

    async def test_not_text_or_too_big(self, vault, told):
        assert "NUL" in (await _write("reports/a.txt", "a\x00b"))["error"]
        assert "5 MB" in (await _write("reports/big.txt", "x" * (5 * 1024 * 1024 + 1)))["error"]
        assert vault.get("vault/reports/a.txt") is None and told == []

    async def test_no_writer_no_write(self, tmp_path):
        set_blob_store(LocalBlobStore(tmp_path))
        try:
            vault_tools.set_writer(None)
            said = await _write("reports/a.csv", "a\n")
        finally:
            set_blob_store(None)
        assert "error" in said


class TestAKnowledgeBasesFolder:
    async def test_is_changed_through_its_own_tool_never_written(self, vault, told, knowledge_store, monkeypatch):
        from _fakes.knowledge_env import enable_knowledge, use_saved_settings
        from core.knowledge import bases, seams
        from core.knowledge.api_models import KnowledgeBaseCreate

        seams.reset_all()
        use_saved_settings(monkeypatch)
        await enable_knowledge()
        await bases.create_base(KnowledgeBaseCreate(name="Support handbook", description="x",
                                                    folder="knowledge/support"))
        said = await _write("knowledge/support/refunds.md", "Refunds: any time.\n")
        assert "Support handbook" in said["error"] and "edit_knowledge_document" in said["error"], said
        assert vault.get("vault/knowledge/support/refunds.md") is None and told == []
        # Beside it, outside every base, is written.
        assert "error" not in await _write("knowledge/notes.md", "A note.\n")
        seams.reset_all()

