"""A message's `@[path]` mentions are found in one pass, however the message is written (F1481, CodeQL py/polynomial-redos).

`@\\[([^\\]\\n]+)\\]` was tried from every `@[`, and each try scanned to the end of the line: a message of `@[` and no
`]` took time by the square of its length, on the server's own loop. Chat, /api/v1 and /api/v2 read mentions, and
take messages of 100,000 characters (17.6 s on cloud's worker). `core.vault.mention_spans` finds the same mentions,
up to 1,024 characters each, in one pass.
"""
from __future__ import annotations

import time

import pytest

from core.storage import get_blob_store
from core.vault import MENTION_MAX, MentionReader, mention_spans

FAST = 0.2


@pytest.mark.parametrize("text", [
    "@[" * 50_000,                        # CodeQL's case: every `@[` a new start, no `]`
    "@[a" * 33_334,                       # mixed, as D3ASM measured it
    ("@[" * 50 + "\n") * 2_000,           # line after line of them
    "@[" + "x" * 99_998,                  # one start, a very long name, no `]`
    ("@[" + "y" * 1_100 + "]") * 91,      # names over the cap, each closed
])
def test_a_long_message_is_read_at_once(text):
    reader = MentionReader(per_mention=10, total=10)
    started = time.perf_counter()
    masked, raws = reader.mask(text)
    assert time.perf_counter() - started < FAST
    assert len(text) >= 99_000 and masked == text and raws == []


@pytest.mark.parametrize("text, found", [
    ("read @[notes.md] now", ["@[notes.md]"]),
    ("@[exports/user@example.csv] x", ["@[exports/user@example.csv]"]),
    ("@[a@[b]", ["@[a@[b]"]),
    ("@[] @[a]", ["@[a]"]),
    ("@[a\nb] @[c]", ["@[c]"]),
    ("x @[y", []),
    ("@[a] @[b] @[a]", ["@[a]", "@[b]", "@[a]"]),
    ("@@[a]", ["@[a]"]),
    ("@[q3 plan (draft).md]", ["@[q3 plan (draft).md]"]),
])
def test_it_finds_the_mentions_it_found_before(text, found):
    assert [text[s:e] for s, e in mention_spans(text)] == found


def test_a_name_is_read_up_to_the_cap():
    at_cap = "@[" + "q" * MENTION_MAX + "]"
    assert [at_cap[s:e] for s, e in mention_spans(at_cap)] == [at_cap]
    assert list(mention_spans("@[" + "q" * (MENTION_MAX + 1) + "]")) == []


def test_mask_puts_each_mention_back_where_it_was():
    masked, raws = MentionReader().mask("a @[x.md] b @[y.md] c")
    assert raws == ["@[x.md]", "@[y.md]"]
    assert masked.startswith("a ") and masked.endswith(" c") and "@[" not in masked


async def test_a_real_mention_still_resolves():
    get_blob_store().put("vault/notes/user@example.md", "hello from the vault")
    out, mentions = await MentionReader().expand("read @[notes/user@example.md] now")
    assert "hello from the vault" in out
    assert [m.outcome for m in mentions] == ["inlined"]
