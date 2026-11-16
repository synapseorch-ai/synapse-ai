"""Synapse AI reads a workspace Vault file it is asked about, read-only, and says plainly how much it read (F1623).

Asked "Summarise the Vault file notes.md in three bullet points", Synapse AI said it could not read Vault files from
here. It answers from a pack code builds (`answer_pack`), and its answering steps hold no way to read a file, and no
pack ever held one: even an `@[notes.md]` in its chat was text. Now code finds the Vault files a message names (an
`@[path]`, a Vault path, or a name only one file in the Vault has, at the top first) and reads them with the engine's own
mention reader, in the workspace's scope and never writing, into the pack whatever the question's route: each file's
text up to 20,000 characters, and 40,000 for all of a message's files, past which the pack says how long it is and how
much is there; a binary file, a missing one or a name two files share is said, never guessed.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from core.storage import LocalBlobStore, get_blob_store, set_blob_store


@pytest.fixture
def vault(tmp_path):
    before = get_blob_store()
    store = LocalBlobStore(tmp_path)
    set_blob_store(store)
    from core.vault_backend import get_vault

    root = get_vault().root
    root.mkdir(parents=True, exist_ok=True)
    yield root, store
    set_blob_store(before)


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _said(message: str) -> str:
    from core.native_builder.answer_pack import vault_said

    return await vault_said(message)


class TestTheFileTheyNameIsRead:
    async def test_a_file_named_by_its_name_is_read(self, vault):
        root, _ = vault
        _put(root, "notes.md", "Alpha line\nBeta line")
        said = await _said("Summarise the Vault file notes.md in three bullet points and show them to me.")
        assert "notes.md" in said and "Beta line" in said, said

    async def test_a_mention_and_a_vault_path_are_read(self, vault):
        root, _ = vault
        _put(root, "reports/q1.csv", "region,sales\nnorth,12")
        assert "north,12" in await _said("What does @[reports/q1.csv] show?")
        assert "north,12" in await _said("What does reports/q1.csv show?")

    async def test_the_top_of_the_vault_is_looked_at_first(self, vault):
        root, _ = vault
        _put(root, "notes.md", "the top one")
        _put(root, "archive/notes.md", "an old one")
        said = await _said("What's in notes.md?")
        assert "the top one" in said and "an old one" not in said, said


class TestWhatCannotBeReadIsSaid:
    async def test_a_missing_file_is_said(self, vault):
        said = await _said("Summarise plans.md for me.")
        assert "plans.md" in said and "isn't in the Vault" in said, said

    async def test_a_name_two_files_share_is_asked_about_never_guessed(self, vault):
        root, _ = vault
        _put(root, "a/notes.md", "first copy")
        _put(root, "b/notes.md", "second copy")
        said = await _said("What's in notes.md?")
        assert "a/notes.md" in said and "b/notes.md" in said and "which" in said.lower(), said
        assert "first copy" not in said and "second copy" not in said, said

    async def test_a_binary_file_is_said_not_read(self, vault):
        root, _ = vault
        (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
        said = await _said("Describe photo.png.")
        assert "photo.png" in said and "can't be read in chat" in said, said


class TestHowMuchIsReadIsSaid:
    async def test_past_twenty_thousand_characters_the_rest_is_said_not_shown(self, vault):
        root, _ = vault
        _put(root, "long.md", "§" * 25_000)
        said = await _said("Summarise long.md.")
        assert said.count("§") == 20_000, said.count("§")
        assert "25,000 characters" in said and "first 20,000" in said, said[:400]

    async def test_a_message_holds_forty_thousand_characters_of_files_at_most(self, vault):
        root, _ = vault
        for name, mark in (("one.md", "§"), ("two.md", "¤"), ("three.md", "¶")):
            _put(root, name, mark * 15_000)
        said = await _said("Compare one.md, two.md and three.md.")
        assert (said.count("§"), said.count("¤"), said.count("¶")) == (15_000, 15_000, 10_000)
        assert "three.md" in said and "first 10,000" in said, said[-400:]


class TestItNeverWrites:
    async def test_reading_writes_nothing(self, vault, monkeypatch):
        root, store = vault
        _put(root, "notes.md", "read only")

        def refuse(*a, **k):
            raise AssertionError("the chat wrote to the Vault")

        for name in ("put", "put_bytes", "put_stream", "delete"):
            monkeypatch.setattr(store, name, refuse)
        assert "read only" in await _said("What's in notes.md?")

    async def test_a_message_naming_no_file_adds_nothing(self, vault):
        assert await _said("How do I schedule a workflow to run every morning?") == ""


class TestThePackCarriesIt:
    def test_the_pack_has_a_place_for_it_whatever_the_route(self):
        from core.native_builder.answer_pack import build_pack
        from core.product_tools.roles import Role

        assert "vault" in inspect.signature(build_pack).parameters, "the answer's pack has no place for a Vault file"
        for needs in ("none", "help", "workspace"):
            pack = build_pack(routed={"needs": needs}, workspace_name="W", profile="", asker_name="", asker_role=Role.OWNER,
                              last="", capabilities="", help_hits=[], named=[], vault="VAULT SECTION")
            assert "VAULT SECTION" in pack, needs

    def test_the_talk_step_may_answer_from_a_file_the_pack_holds(self):
        import core.native_builder as nb

        steps = json.loads((Path(nb.__file__).parent / "orchestration.json").read_text())["steps"]
        talk = next(s for s in steps if s["id"] == "step_answer_talk")["prompt_template"]
        assert "Vault file" in talk, "the talk step says never describe what the workspace holds, a Vault file too"
