"""An add never writes over a file another write made or changed after the add read it (F1626's follow-up).

F1626's add wrote only over the version it read, but two windows were left. The engine's own writer took a call with
no version (the add read no file) as "no condition", looked for itself, and wrote over a file another add had made in
between, so that add's line was lost. And both adds read the file's text first and its version after, so a write
landing between the two gave a version that matched a newer file than the text read. Now an explicit `if_match=None`
means create-only (a call with no keyword is a plain write), and an add takes its version around its read (the
sandbox from the very bytes it read), so a write in either window makes the add stale and it reads again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.storage import LocalBlobStore, PreconditionFailed, get_blob_store, set_blob_store


def _json(result) -> dict:
    return json.loads(result[0].text)


@pytest.fixture
def store(tmp_path):
    before = get_blob_store()
    s = LocalBlobStore(tmp_path)
    set_blob_store(s)
    yield s
    set_blob_store(before)


@pytest.fixture
def engine_writes(store):
    """The vault server writing through the engine's own writer, as a host whose Vault is the blob store does."""
    from core import vault_tools

    vault_tools.set_writer(vault_tools.engine_writer)
    yield store
    vault_tools.set_writer(None)


class TestTheVaultServersAdd:
    async def test_a_file_made_after_the_add_read_nothing_keeps_its_line(self, engine_writes, monkeypatch):
        from core import vault_tools

        store = engine_writes
        real = vault_tools._get_text
        once = []

        def read(rel):
            if not once:
                once.append(True)
                said = real(rel)
                store.put("vault/logs/x.md", "theirs")  # another add makes the file right after this one read nothing
                return said
            return real(rel)

        monkeypatch.setattr(vault_tools, "_get_text", read)
        out = _json(await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "mine", "append": True}))
        assert "error" not in out, out
        assert store.get("vault/logs/x.md") == "theirs\nmine"

    async def test_a_write_after_the_add_read_the_text_keeps_its_line(self, engine_writes, monkeypatch):
        from core import vault_tools

        store = engine_writes
        store.put("vault/logs/x.md", "first")
        real = vault_tools._get_text
        once = []

        def read(rel):
            said = real(rel)
            if not once:
                once.append(True)
                store.put("vault/logs/x.md", "first\nother")  # another add lands right after this one read
            return said

        monkeypatch.setattr(vault_tools, "_get_text", read)
        out = _json(await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "mine", "append": True}))
        assert "error" not in out, out
        assert store.get("vault/logs/x.md") == "first\nother\nmine"


class TestTheEngineWritersContract:
    """No keyword: a plain write. `if_match=None`: create-only. A version: only over that version. A host's own
    writer keeps the same contract."""

    async def test_create_only_refuses_a_file_that_is_there(self, store):
        from core.vault_tools import engine_writer

        store.put("vault/notes.md", "there")
        with pytest.raises(PreconditionFailed):
            await engine_writer("notes.md", "mine", if_match=None)
        assert store.get("vault/notes.md") == "there"

    async def test_a_plain_write_writes_over_as_before(self, store):
        from core.vault_tools import engine_writer

        store.put("vault/notes.md", "old")
        await engine_writer("notes.md", "new")
        assert store.get("vault/notes.md") == "new"

    async def test_create_only_makes_a_missing_file(self, store):
        from core.vault_tools import engine_writer

        await engine_writer("fresh.md", "made", if_match=None)
        assert store.get("vault/fresh.md") == "made"


class TestTheSandboxsAdd:
    def test_a_write_right_after_the_add_read_keeps_its_line(self, store, monkeypatch):
        from core.vault_backend import get_vault
        from tools import sandbox

        root = get_vault().root
        root.mkdir(parents=True, exist_ok=True)
        target = root / "log.md"
        target.write_text("first", encoding="utf-8")
        once = []
        real_text, real_bytes = Path.read_text, Path.read_bytes

        def other_lands(path):
            if not once and Path(path) == target:
                once.append(True)
                fd = os.open(target, os.O_WRONLY | os.O_APPEND)
                os.write(fd, b"\nother")
                os.close(fd)

        def read_text(self, *a, **k):
            said = real_text(self, *a, **k)
            other_lands(self)
            return said

        def read_bytes(self, *a, **k):
            said = real_bytes(self, *a, **k)
            other_lands(self)
            return said

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        out = _json(sandbox._handle_write({"path": "log.md", "content": "mine", "append": True}))
        assert "error" not in out, out
        assert real_text(target) == "first\nother\nmine"
