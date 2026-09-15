"""`@[path]` in a prompt — one async reader, read from the store, capped, never stale.

What it replaced was a synchronous `expand_vault_mentions` that read a
materialised working copy (`S3Vault.ensure_local`, which serves the copy for as
long as it exists — an edit on the vault page was never seen by a warm replica),
had no cap, and kept the `@[path]` token in its output so a second pass inlined
the file twice.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.storage import get_blob_store, set_blob_store
from core.storage.base import LocalBlobStore, tenant_key
from core.vault import Mention, MentionReader, mention_key


def _put(rel: str, text: str) -> None:
    get_blob_store().put(f"vault/{rel}", text)


def _write_bytes(rel: str, body: bytes) -> None:
    path = get_blob_store().path_for(f"vault/{rel}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


class _Counting(LocalBlobStore):
    """A local store that records what the reader asks of it, and from where."""

    def __init__(self, root):
        super().__init__(root)
        self.gets = 0
        self.exists_calls = 0
        self.threads: set[int] = set()
        self.fail_get: Exception | None = None

    def get(self, key):
        self.gets += 1
        self.threads.add(threading.get_ident())
        if self.fail_get is not None:
            raise self.fail_get
        return super().get(key)

    def exists(self, key):
        self.exists_calls += 1
        return super().exists(key)


class _Pathless:
    """A store with no filesystem, like S3 — so the vault materialises a working copy."""

    def __init__(self):
        self.blobs: dict[str, str] = {}

    def put(self, key, content):
        self.blobs[tenant_key(key)] = content

    def get(self, key):
        return self.blobs.get(tenant_key(key))

    def exists(self, key):
        return tenant_key(key) in self.blobs

    def delete(self, key):
        self.blobs.pop(tenant_key(key), None)

    def list(self, prefix=""):
        scoped = tenant_key(prefix)
        cut = tenant_key("")
        return [k[len(cut):] for k in self.blobs if k.startswith(scoped)]

    def path_for(self, key):
        return None


@pytest.fixture
def counting(tmp_path):
    store = _Counting(tmp_path / "blobs")
    set_blob_store(store)
    yield store
    set_blob_store(None)


class TestTheKeyGuard:
    @pytest.mark.parametrize(
        "rel",
        ["../x.md", "/etc/passwd", "a\\b.md", "", "   ", "a//b.md", "./a.md", "a/./b.md",
         "a/../b.md", "notes[1].md", "bad\x00.md", "bad​.md", ".md"],
        ids=["dotdot", "absolute", "backslash", "empty", "blank", "double-slash", "dot-segment",
             "inner-dot", "inner-dotdot", "bracket", "control", "zero-width", "private-use"],
    )
    def test_refuses_what_is_not_a_vault_path(self, rel):
        assert mention_key(rel) is None

    def test_places_the_path_under_the_vault(self):
        assert mention_key("notes/q3 plan (draft).md") == "vault/notes/q3 plan (draft).md"

    def test_normalises_to_nfc(self):
        assert mention_key("café.md") == "vault/café.md"


class TestInlining:
    async def test_inlines_a_small_file(self):
        _put("notes.md", "hello from the vault")
        out, mentions = await MentionReader().expand("read @[notes.md] now")
        assert "hello from the vault" in out
        assert "@[notes.md]" not in out
        assert out.startswith("read [vault: notes.md — 20 chars")
        assert [m.outcome for m in mentions] == ["inlined"]
        assert mentions[0].chars == 20

    async def test_text_without_a_mention_is_returned_as_is(self):
        out, mentions = await MentionReader().expand("plain message")
        assert (out, mentions) == ("plain message", [])

    async def test_a_second_pass_cannot_expand_again(self):
        _put("notes.md", "once")
        once, _ = await MentionReader().expand("@[notes.md]")
        twice, mentions = await MentionReader().expand(once)
        assert twice == once and mentions == []

    async def test_the_fence_outgrows_the_backticks_in_the_file(self):
        _put("code.md", "```py\nprint(1)\n```")
        out, _ = await MentionReader().expand("@[code.md]")
        assert "\n````md\n```py\nprint(1)\n```\n````" in out

    async def test_the_same_file_twice_is_read_once_and_shown_once(self, counting):
        _put("notes.md", "hello")
        out, mentions = await MentionReader().expand("@[notes.md] and again @[notes.md]")
        assert counting.gets == 1
        assert out.count("hello") == 1
        assert "[vault: notes.md — inlined above]" in out
        assert [(m.outcome, m.reason) for m in mentions] == [("inlined", ""), ("inlined", "above")]

    async def test_a_file_holding_a_template_is_shown_as_written(self):
        """The two-phase form: mask, let the caller render, then fill.

        A file that contains `{{ global.KEY }}` must reach the model as those
        words, not as the secret — the reference in the prompt itself renders.
        """
        from core import templating

        _put("secrets.md", "key: {{ global.KEY }}")
        reader = MentionReader()
        masked, raws = reader.mask("token {{ global.KEY }} then @[secrets.md]")
        rendered = templating.render(masked, {"global": {"KEY": "s3cret"}})
        out, mentions = await reader.fill(rendered, raws)
        assert out.startswith("token s3cret then [vault: secrets.md")
        assert "key: {{ global.KEY }}" in out
        assert out.count("s3cret") == 1
        assert [m.outcome for m in mentions] == ["inlined"]

    def test_a_placeholder_survives_the_legacy_state_pass(self):
        import re

        masked, raws = MentionReader(per_mention=1, total=1).mask("{state.a} @[x.md] {{ state.b }}")
        assert raws == ["@[x.md]"]
        assert "{" not in masked.replace("{state.a}", "").replace("{{ state.b }}", "")
        assert re.sub(r"\{state\.(\w+)\}", "A", masked).startswith("A ")


class TestTheCap:
    async def test_over_the_per_mention_cap_is_a_reference(self):
        _put("big.json", "x" * 50)
        out, mentions = await MentionReader(per_mention=10, total=100).expand("@[big.json]")
        assert "x" * 50 not in out
        assert "[vault: big.json — 50 chars, over the 10-char inline limit; " in out
        assert 'read it with read_file / read_json / search_files (path "big.json")' in out
        assert (mentions[0].outcome, mentions[0].chars) == ("referenced", 50)

    async def test_the_prompts_budget_is_shared_in_order(self):
        _put("a.md", "a" * 80)
        _put("b.md", "b" * 80)
        out, mentions = await MentionReader(per_mention=100, total=120).expand("@[a.md] @[b.md]")
        assert "a" * 80 in out and "b" * 80 not in out
        assert [m.outcome for m in mentions] == ["inlined", "referenced"]
        assert "the prompt's 120-char inline budget is used up" in out

    async def test_the_budget_is_per_prompt_and_the_cache_per_reader(self, counting):
        """An orchestration run keeps one reader across its steps."""
        _put("a.md", "a" * 80)
        _put("b.md", "b" * 80)
        reader = MentionReader(per_mention=100, total=120)
        first, _ = await reader.expand("@[a.md]")
        second, mentions = await reader.expand("@[b.md] then @[a.md]")
        assert "a" * 80 in first
        assert "b" * 80 in second and "a" * 80 not in second
        assert [m.outcome for m in mentions] == ["inlined", "referenced"]
        assert counting.gets == 2

    async def test_a_store_error_is_asked_again(self, counting):
        _put("notes.md", "hello")
        counting.fail_get = RuntimeError("blip")
        reader = MentionReader()
        first, _ = await reader.expand("@[notes.md]")
        counting.fail_get = None
        second, _ = await reader.expand("@[notes.md]")
        assert first == "@[notes.md]" and "hello" in second

    def test_the_cap_is_the_tenants_vault_threshold(self, monkeypatch):
        import core.config as config

        monkeypatch.setattr(config, "load_settings", lambda: {"vault_threshold": 7})
        reader = MentionReader()
        assert (reader.per_mention, reader.total) == (7, 14)

    def test_the_default_cap_is_the_shipped_threshold(self):
        reader = MentionReader()
        assert (reader.per_mention, reader.total) == (100_000, 200_000)


class TestWhatCannotBeInlined:
    async def test_a_binary_is_referenced_without_being_read(self, counting):
        _write_bytes("report.pdf", b"%PDF-1.4 " + b"\x00" * 64)
        out, mentions = await MentionReader().expand("see @[report.pdf]")
        assert (counting.gets, counting.exists_calls) == (0, 1)
        assert "[vault: report.pdf — a PDF; binary, download-only, not readable by tools" in out
        assert mentions[0].outcome == "referenced"

    async def test_a_missing_binary_is_missing(self):
        out, mentions = await MentionReader().expand("see @[nope.pdf]")
        assert out == "see @[nope.pdf]"
        assert mentions[0].outcome == "missing"

    async def test_undecodable_bytes_are_referenced(self):
        _write_bytes("blob.dat", b"\xff\xfe\x00junk")
        out, mentions = await MentionReader().expand("@[blob.dat]")
        assert "[vault: blob.dat — not a text file; not readable by tools" in out
        assert mentions[0].outcome == "referenced"

    async def test_a_missing_file_stays_literal(self):
        out, mentions = await MentionReader().expand("see @[nope.md] please")
        assert out == "see @[nope.md] please"
        assert (mentions[0].outcome, mentions[0].reason) == ("missing", "no such vault file")
        assert mentions[0].warning() == "@[nope.md] — no such vault file; left as typed"

    async def test_a_path_that_is_not_a_vault_path_stays_literal(self):
        msg = "read @[../../../../etc/passwd]"
        out, mentions = await MentionReader().expand(msg)
        assert out == msg
        assert mentions[0].outcome == "invalid"
        assert mentions[0].warning() == "@[../../../../etc/passwd] — not a vault path; left as typed"

    async def test_a_store_error_stays_literal_and_says_why(self, counting):
        _put("notes.md", "hello")
        counting.fail_get = RuntimeError("the object store said no")
        out, mentions = await MentionReader().expand("@[notes.md]")
        assert out == "@[notes.md]"
        assert mentions[0].outcome == "unreadable"
        assert "the object store said no" in mentions[0].warning()

    async def test_an_nfd_mention_finds_the_nfc_file(self):
        _put("café.md", "au lait")
        out, _ = await MentionReader().expand("@[café.md]")
        assert "au lait" in out

    async def test_an_inlined_file_carries_no_warning(self):
        _put("notes.md", "hello")
        _, mentions = await MentionReader().expand("@[notes.md]")
        assert mentions[0].warning() is None


class TestNeverStale:
    async def test_an_edit_is_seen_by_the_next_reader(self):
        _put("notes.md", "v1")
        first, _ = await MentionReader().expand("@[notes.md]")
        _put("notes.md", "v2")
        second, _ = await MentionReader().expand("@[notes.md]")
        assert "v1" in first and "v2" in second and "v1" not in second

    async def test_a_materialised_working_copy_is_never_read(self):
        """The old reader served `ensure_local`'s copy for as long as it existed."""
        from core.vault_backend import get_vault

        store = _Pathless()
        set_blob_store(store)
        try:
            store.put("vault/notes.md", "v1")
            vault = get_vault()
            assert vault.materialises
            copy = vault.ensure_local(vault.root / "notes.md")
            assert Path(copy).read_text(encoding="utf-8") == "v1"
            store.put("vault/notes.md", "v2")
            out, _ = await MentionReader().expand("@[notes.md]")
            assert "v2" in out and "v1" not in out
        finally:
            set_blob_store(None)

    async def test_the_read_leaves_the_event_loop(self, counting):
        _put("notes.md", "hello")
        await MentionReader().expand("@[notes.md]")
        assert counting.threads and threading.get_ident() not in counting.threads


class TestWhatTheLogSees:
    def test_a_mention_summarises_without_its_content(self):
        m = Mention(raw="@[a.md]", rel="a.md", key="vault/a.md", outcome="inlined", chars=3, text="abc")
        assert m.summary() == {"path": "a.md", "chars": 3, "outcome": "inlined", "reason": ""}
        assert m.line() == "@[a.md] — 3 chars inlined"

    def test_the_lines_for_the_other_outcomes(self):
        above = Mention(raw="@[a.md]", rel="a.md", key="vault/a.md", outcome="inlined", chars=3, reason="above")
        ref = Mention(raw="@[b.pdf]", rel="b.pdf", key="vault/b.pdf", outcome="referenced", reason="a PDF; binary")
        gone = Mention(raw="@[c.md]", rel="c.md", key="vault/c.md", outcome="missing", reason="no such vault file")
        assert above.line() == "@[a.md] — inlined above"
        assert ref.line() == "@[b.pdf] — referenced: a PDF; binary"
        assert gone.line() == "@[c.md] — missing: no such vault file"
