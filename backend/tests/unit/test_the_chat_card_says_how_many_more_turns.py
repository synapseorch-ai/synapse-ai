"""The chat's turn-limit card says how many more turns Keep going gives (F1234; F1219's shots, 10-29).

Synapse AI's panel card read "Keep going gives it 2 more turns"; an agent chat's card did not, because the chat page
copied the pause's fields one by one and left `turn_limit` behind, and a parked pause did not keep it for a page
reopened while it waits. Now the loop's own `turn_limit` is the one source: the live card copies it, and the parked
pause keeps it for the reopened card.
"""
from __future__ import annotations

from core import turn_limit
from core.chat_approval import parked_payload, public_view

PAUSE = {"type": "human_input_required", "kind": "tool_approval", "tool_name": turn_limit.KIND,
         "category": turn_limit.KIND, "approval_id": "turn_limit:3", "tool_args": {},
         "prompt": turn_limit.asking_words(3), "turn_limit": {"turns": 3, "more": 3}}


def test_the_parked_pause_keeps_the_count():
    assert parked_payload(PAUSE, message="find it", progress={})["turn_limit"] == {"turns": 3, "more": 3}


def test_a_reopened_page_gets_it_back():
    view = public_view(parked_payload(PAUSE, message="find it", progress={}))
    assert view["turn_limit"] == {"turns": 3, "more": 3}


def test_a_tool_approval_has_none():
    tool = {**PAUSE, "tool_name": "danger", "category": "destructive", "approval_id": "a1"}
    tool.pop("turn_limit")
    assert "turn_limit" not in parked_payload(tool, message="x", progress={})
    assert "turn_limit" not in public_view(parked_payload(tool, message="x", progress={}))


def test_the_chat_page_copies_it_from_the_pause_it_draws():
    """The live card's fields are copied one by one; `turn_limit` is one of them, from the event (one source)."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "app" / "(app)" / "page.tsx").read_text()
    assert "turn_limit: data.turn_limit," in page and "turn_limit: parked.turn_limit," in page
