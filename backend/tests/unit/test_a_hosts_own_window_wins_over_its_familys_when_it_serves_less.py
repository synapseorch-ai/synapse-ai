"""A host's own window for a model wins over its family's whenever the host serves less (F1649).

The window table matches a model's id, and each id its provider prefix leaves, by the longest prefix, so a model that a
host serves is read by its family's row. Groq's `groq.qwen/qwen3.8-27b` read Qwen 3.8's 983,616 tokens (Alibaba Model
Studio's window) while Groq serves 131,072 (Groq's model page), so a step's context budget planned for about seven
times what Groq takes. Now a host's own row for a family (`HOST_WINDOWS`, each with the page it was read from) is read
for that host's ids, and it wins whenever it is less than the family's. Where a host serves more, the family's smaller
window stands: a budget never plans past what is known to fit.

Near misses: Alibaba's own API keeps its long window; a host with no row of its own reads the family's; a host's row
never raises the family's; an id the table does not know is still not known.
"""
from __future__ import annotations

import pytest

from core.providers import windows as W


def test_groq_s_qwen_3_8_reads_what_groq_serves():
    assert W.window_tokens("groq.qwen/qwen3.8-27b") == 131_072


@pytest.mark.parametrize("model, tokens", [
    ("qwen.qwen3.8-max", 983_616),             # Alibaba's own API: its long window
    ("qwen.qwen3.8-flash", 983_616),
    ("openrouter.qwen/qwen3.8-27b", 983_616),  # a host with no row of its own: the family's
    ("together.Qwen/Qwen3.7-Plus", 983_616),   # another host for the family, no row: the family's
    ("cerebras.qwen-3.8-27b", None),           # an id the table does not know
])
def test_its_near_misses(model, tokens):
    assert W.window_tokens(model) == tokens


def test_a_hosts_row_never_raises_the_familys(monkeypatch):
    monkeypatch.setattr(W, "HOST_WINDOWS", (("groq", "qwen3.8", 2_000_000, "https://example.com/groq"),))
    assert W.window_tokens("groq.qwen/qwen3.8-27b") == 983_616


def test_a_hosts_row_for_a_family_the_table_does_not_know_is_read(monkeypatch):
    monkeypatch.setattr(W, "HOST_WINDOWS", (("groq", "acme-", 64_000, "https://example.com/groq"),))
    assert W.window_tokens("groq.acme-7b") == 64_000
    assert W.window_tokens("acme-7b") is None, "only the host's ids read its row"


def test_every_host_row_names_the_page_it_was_read_from():
    assert W.HOST_WINDOWS
    assert all(len(row) == 4 and row[3].startswith("https://") for row in W.HOST_WINDOWS), W.HOST_WINDOWS
