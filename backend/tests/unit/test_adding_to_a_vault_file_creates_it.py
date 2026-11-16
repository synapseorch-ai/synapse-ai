"""Adding to a Vault file that does not exist yet creates it on the first run, and two adds at once never lose a line
(F1626).

A workflow asked to "add a line to the Vault file expenses.md" failed on its first run, every time: neither host had a
way to add to a file. The planner patched a file it assumed was there (`vault_patch`: "File not found") or read it,
recomposed it and wrote it back (a read of a file that does not exist yet fails). Now `vault_write` takes `append: true`:
it adds the text at the end, one line break between, and creates the file and its folder when it is not there. On both
hosts the add reads the file's version and writes only over that version (`core.storage.PreconditionFailed`
otherwise), reading again once on a clash, so two runs adding at once both land. A plain write that meets another is
said ("read it again"), never retried over content the caller never saw. A text patch of a missing file, or of
nothing (`find: ""` would put the text at the top), is refused with the way to add to a file; merging JSON into a
missing file makes it.
"""
from __future__ import annotations

import json
import os

import pytest

from core.storage import LocalBlobStore, get_blob_store, set_blob_store


def _json(result) -> dict:
    return json.loads(result[0].text)


@pytest.fixture
def vault(tmp_path):
    """A self-hosted Vault on a temporary directory, as a plain install has."""
    before = get_blob_store()
    set_blob_store(LocalBlobStore(tmp_path))
    from core.vault_backend import get_vault

    root = get_vault().root
    root.mkdir(parents=True, exist_ok=True)  # a Vault the person has used: its folder is there
    yield root
    set_blob_store(before)


class TestTheSandboxAddsToAFile:
    def test_an_add_to_a_missing_file_creates_it(self, vault):
        from tools import sandbox

        out = _json(sandbox._handle_write({"path": "logs/expenses.md", "content": "820, Travel", "append": True}))
        assert "error" not in out, out
        assert (vault / "logs/expenses.md").read_text() == "820, Travel"

    def test_an_add_to_a_file_puts_it_at_the_end_one_line_break_between(self, vault):
        from tools import sandbox

        (vault / "clocks.md").write_text("Tokyo 09:00")
        _json(sandbox._handle_write({"path": "clocks.md", "content": "London 01:00", "append": True}))
        (vault / "ends.md").write_text("one\n")
        _json(sandbox._handle_write({"path": "ends.md", "content": "two", "append": True}))
        assert (vault / "clocks.md").read_text() == "Tokyo 09:00\nLondon 01:00"
        assert (vault / "ends.md").read_text() == "one\ntwo"

    def test_a_write_without_append_replaces_the_file_as_before(self, vault):
        from tools import sandbox

        (vault / "note.md").write_text("old")
        _json(sandbox._handle_write({"path": "note.md", "content": "new"}))
        assert (vault / "note.md").read_text() == "new"

    def test_two_adds_at_once_both_land(self, vault, monkeypatch):
        """Another run's add lands between this one's read and its write: this one reads again, never overwrites."""
        from core.vault_backend import LocalVault
        from tools import sandbox

        (vault / "log.md").write_text("first")
        real = LocalVault.write
        meddled = []

        def write(self, path, content, **kw):
            if not meddled:
                meddled.append(True)
                path.write_text(path.read_text() + "\nfrom the other run")
            return real(self, path, content, **kw)

        monkeypatch.setattr(LocalVault, "write", write)
        out = _json(sandbox._handle_write({"path": "log.md", "content": "from this run", "append": True}))
        assert "error" not in out, out
        assert (vault / "log.md").read_text() == "first\nfrom the other run\nfrom this run"


class TestTheLocalVersionIsTheContent:
    def test_same_size_same_time_other_content_is_stale(self, vault):
        """A one-character change inside the filesystem's time resolution (a coarse-time mount) is still a change."""
        from core.storage import PreconditionFailed
        from core.vault_backend import get_vault

        backend = get_vault()
        path = vault / "same.md"
        path.write_text("aaaa")
        seen = backend.version(path)
        stamp = os.stat(path)
        path.write_text("aaab")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with pytest.raises(PreconditionFailed):
            backend.write(path, "aaaa\nmore", if_match=seen)
        assert path.read_text() == "aaab"

    def test_create_only_refuses_a_file_that_is_there(self, vault):
        from core.storage import PreconditionFailed
        from core.vault_backend import get_vault

        (vault / "made.md").write_text("by the other run")
        with pytest.raises(PreconditionFailed):
            get_vault().write(vault / "made.md", "mine", if_none_match=True)


class TestThePatchOfAMissingFile:
    def test_merging_json_into_a_missing_file_makes_it(self, vault):
        from tools import sandbox

        out = _json(sandbox._handle_patch({"path": "state.json", "merge": json.dumps({"count": 1})}))
        assert "error" not in out, out
        assert json.loads((vault / "state.json").read_text()) == {"count": 1}

    def test_a_text_patch_of_a_missing_file_says_how_to_add_to_one(self, vault):
        from tools import sandbox

        out = _json(sandbox._handle_patch({"path": "expenses.md", "find": "x", "replace": "y"}))
        assert "error" in out and "append" in out["error"] and "vault_write" in out["error"], out

    def test_finding_nothing_is_refused_never_put_at_the_top(self, vault):
        from tools import sandbox

        (vault / "log.md").write_text("first")
        out = _json(sandbox._handle_patch({"path": "log.md", "find": "", "replace": "added"}))
        assert "error" in out and "append" in out["error"], out
        assert (vault / "log.md").read_text() == "first"


class _Store:
    """Cloud's engine reads, as the blob store answers them: text by key, and its version (the object's ETag)."""

    def __init__(self, files: dict[str, tuple[str, str]]):
        self.files = files

    def get(self, key):
        return self.files[key][0] if key in self.files else None

    def version(self, key):
        return self.files[key][1] if key in self.files else None

    def exists(self, key):
        return key in self.files

    def path_for(self, key):
        return None

    def list(self, prefix=""):
        return [k for k in self.files if k.startswith(prefix)]


@pytest.fixture
def cloud(monkeypatch):
    """Cloud's `vault` server with a writer set (F1289), its reads from a fake store, its writes recorded."""
    from core import vault_tools
    from core.vault import mention_key

    store = _Store({})
    monkeypatch.setattr(vault_tools, "_store", lambda: store)
    calls: list[dict] = []
    plan: list = []

    async def writer(path, content, *, if_match=None):
        calls.append({"path": path, "content": content, "if_match": if_match})
        outcome = plan.pop(0) if plan else None
        if callable(outcome):  # another run's write, landing as this one is made
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        key = mention_key(path)
        store.files[key] = (content, f'"v{len(calls)}"')
        return {"path": path, "size": len(content)}

    vault_tools.set_writer(writer)
    yield store, calls, plan
    vault_tools.set_writer(None)


def _cloud_said(result) -> dict:
    return json.loads(result[0].text)


class TestCloudsVaultAddsToAFile:
    async def test_an_add_to_a_missing_file_writes_it_with_no_version(self, cloud):
        from core import vault_tools

        store, calls, _ = cloud
        out = _cloud_said(await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "one", "append": True}))
        assert "error" not in out, out
        assert calls == [{"path": "logs/x.md", "content": "one", "if_match": None}]

    async def test_an_add_passes_the_version_it_read_verbatim(self, cloud):
        from core import vault_tools
        from core.vault import mention_key

        store, calls, _ = cloud
        store.files[mention_key("logs/x.md")] = ("zero", '"9b2cf535f27731c974343645a3985328"')
        await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "one", "append": True})
        assert calls == [{"path": "logs/x.md", "content": "zero\none", "if_match": '"9b2cf535f27731c974343645a3985328"'}]

    async def test_a_clash_reads_again_once_and_both_lines_land(self, cloud):
        from core import vault_tools
        from core.storage import PreconditionFailed
        from core.vault import mention_key

        store, calls, plan = cloud
        key = mention_key("logs/x.md")
        store.files[key] = ("zero", '"a"')

        def other_run_lands():
            store.files[key] = ("zero\nother", '"b"')
            return PreconditionFailed("changed after it was read")

        plan.append(other_run_lands)
        out = _cloud_said(await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "one", "append": True}))
        assert "error" not in out, out
        assert [c["if_match"] for c in calls] == ['"a"', '"b"']
        assert calls[-1]["content"] == "zero\nother\none"

    async def test_the_create_race_reads_again_too(self, cloud):
        """Two adds to a missing file: both read nothing; the second's create-only write is stale, so it reads again."""
        from core import vault_tools
        from core.storage import PreconditionFailed
        from core.vault import mention_key

        store, calls, plan = cloud
        key = mention_key("logs/new.md")

        def other_made_it():
            store.files[key] = ("theirs", '"t"')
            return PreconditionFailed("made by another run")

        plan.append(other_made_it)
        out = _cloud_said(await vault_tools.call("vault_write", {"path": "logs/new.md", "content": "mine", "append": True}))
        assert "error" not in out, out
        assert [c["if_match"] for c in calls] == [None, '"t"'] and calls[-1]["content"] == "theirs\nmine"

    async def test_a_second_clash_is_said_never_raised(self, cloud):
        from core import vault_tools
        from core.storage import PreconditionFailed

        _, calls, plan = cloud
        plan.extend([PreconditionFailed("stale"), PreconditionFailed("stale again")])
        out = _cloud_said(await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "one", "append": True}))
        assert "error" in out and "read it again" in out["error"], out
        assert len(calls) == 2

    async def test_a_plain_write_that_meets_another_is_said_and_not_retried(self, cloud):
        from core import vault_tools
        from core.storage import PreconditionFailed

        _, calls, plan = cloud
        plan.append(PreconditionFailed("stale"))
        out = _cloud_said(await vault_tools.call("vault_write", {"path": "logs/x.md", "content": "whole"}))
        assert "error" in out and "changed while I was writing it" in out["error"] and "read it again" in out["error"], out
        assert len(calls) == 1


class TestThePlannerKnowsHowToAdd:
    def test_the_plan_steps_prompt_says_to_add_with_append(self):
        from pathlib import Path

        import core.native_builder as nb

        text = (Path(nb.__file__).parent / "prompts" / "plan_steps.md").read_text()
        assert "append" in text and "vault_write" in text, "the planner is never told how to add to a Vault file"
