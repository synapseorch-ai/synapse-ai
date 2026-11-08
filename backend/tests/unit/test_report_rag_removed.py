"""Report RAG is gone: nothing a session holds adds a "search the embedded report" block to the prompt.

It was dead code — `last_report_context` was read by the volatile tail and
written by nothing, `embed_report_for_session` had no caller, and the tool the
injected block told the model to call, `search_embedded_report`, was served by
no tool server. Knowledge (`core/knowledge/`) replaces it. Closes backlog 10
item 12's last third (B1a, D158).
"""
from __future__ import annotations

import time

from core.memory import MemoryStore
from core.tools import build_volatile_tail


def test_a_session_holding_report_context_adds_nothing_to_the_tail():
    state = {"last_report_context": {"timestamp": time.time(), "row_count": 40, "type": "emails"}}

    tail = build_volatile_tail("s1", lambda _sid: state, turns_remaining=3, max_turns=5)

    assert "RAG" not in tail
    assert "search_embedded_report" not in tail
    assert tail == build_volatile_tail("s1", lambda _sid: {}, turns_remaining=3, max_turns=5)


def test_memory_store_has_no_report_methods():
    for name in ("embed_report_for_session", "search_session_embeddings", "search_embedded_report",
                 "clear_session_embeddings", "generate_report_summary"):
        assert not hasattr(MemoryStore, name), name


def test_the_response_cache_keeps_what_it_uses():
    assert callable(getattr(MemoryStore, "get_embedding", None))
    assert callable(getattr(MemoryStore, "clear_memory", None))
