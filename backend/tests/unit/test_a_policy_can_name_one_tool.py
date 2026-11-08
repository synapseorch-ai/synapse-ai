"""A tool policy can name one tool, and that decision wins over the tool's category (BWA1, D274).

The AI builder's approval guard needs an agent that asks before `create_booking` while its searches run freely; a
category (`open_world: confirm`) would make every web search ask too. `tool:<name>` keys sit beside the three
categories, survive a write (`clean_policy`) and a resolve, and a visitor's `confirm` is still a `deny` for them.
"""
from __future__ import annotations

from core.tool_policy import REASONS, clean_policy, decide, resolve_policy
from core.tools import VirtualTool, hinted, custom_virtual_tool

BOOK = custom_virtual_tool(hinted({"name": "create_booking", "description": "d", "method": "POST"}))
SEARCH = custom_virtual_tool(hinted({"name": "search_flights", "description": "d", "method": "POST"}))


def test_the_named_tool_asks_and_the_others_follow_their_category():
    policy = resolve_policy({"tool_policy": {"tool:create_booking": "confirm"}}, {})
    assert decide(BOOK, policy) == ("confirm", "tool")
    assert decide(SEARCH, policy) == ("allow", "open_world")
    assert REASONS["tool"]


def test_a_named_tool_can_also_be_let_through_a_stricter_category():
    policy = resolve_policy({"tool_policy": {"open_world": "confirm", "tool:search_flights": "allow"}}, {})
    assert decide(SEARCH, policy) == ("allow", "tool")
    assert decide(BOOK, policy) == ("confirm", "open_world")


def test_junk_is_still_ignored_and_a_write_keeps_the_named_tools():
    assert resolve_policy({"tool_policy": {"tool:": "confirm", "tool:x": "maybe", "other": "deny"}}, {}) == {
        "destructive": "allow", "open_world": "allow", "undeclared": "allow"}
    assert clean_policy({"tool:b": "confirm", "open_world": "deny", "tool:a": "allow", "tool:c": "nope"}) == {
        "open_world": "deny", "tool:a": "allow", "tool:b": "confirm"}


def test_a_visitors_confirm_is_a_deny_for_a_named_tool_too():
    policy = resolve_policy({"tool_policy": {"tool:create_booking": "confirm"}}, {})
    visitor = {c: ("deny" if d == "confirm" else d) for c, d in policy.items()}  # the engine's rewrite, verbatim
    assert decide(BOOK, visitor) == ("deny", "tool")


def test_an_undeclared_tool_by_name_is_matched_by_name():
    tool = VirtualTool("legacy_post", "d", {"type": "object"})
    assert decide(tool, resolve_policy({"tool_policy": {"tool:legacy_post": "deny"}}, {})) == ("deny", "tool")
