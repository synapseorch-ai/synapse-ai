"""A custom HTTP tool declares what it does, from its method, when it is created or saved (F488, BWA1, D274).

BWA1's Nova Pro baseline: a workspace whose policy was `open_world: confirm` let a booking API's POST tool run with
no approval, because every custom tool was `undeclared` and the policy for that was allow. Naveen's call: the hints
reach new and edited tools only, so no schedule or widget changes behaviour on upgrade; the AI builder's tools always
get them, marked as the builder's (believed when they restrict, as a third-party server's are).
"""
from __future__ import annotations

import pytest

from core.tool_policy import decide
from core.tools import BUILDER_HINTS, custom_virtual_tool, hinted, method_hints

OPEN_WORLD_CONFIRM = {"destructive": "confirm", "open_world": "confirm", "undeclared": "allow"}


@pytest.mark.parametrize("method,read_only,destructive", [
    ("GET", True, False), ("get", True, False), (None, True, False),
    ("POST", False, False), ("PUT", False, False), ("PATCH", False, False), ("DELETE", False, True),
])
def test_the_method_says_what_it_does(method, read_only, destructive):
    hints = method_hints(method)
    assert hints["readOnlyHint"] is read_only and hints.get("destructiveHint", False) is destructive
    assert hints["openWorldHint"] is True, "a custom HTTP tool reaches outside, as the built-in web tools say"


def test_a_saved_tool_gets_its_hints_and_keeps_the_persons():
    assert hinted({"name": "book", "method": "POST"})["annotations"] == method_hints("POST")
    mine = {"readOnlyHint": True, "openWorldHint": False}
    assert hinted({"name": "lookup", "method": "POST", "annotations": mine})["annotations"] == mine
    assert "annotations" not in hinted({"name": "calc", "tool_type": "python", "code": "x"})
    assert hinted({"name": "b", "method": "GET"}, by_builder=True)["annotations_by"] == BUILDER_HINTS


@pytest.mark.parametrize("method,decision", [("POST", "confirm"), ("DELETE", "confirm"), ("GET", "confirm")])
def test_the_policy_now_sees_what_a_saved_tool_does(method, decision):
    tool = custom_virtual_tool(hinted({"name": "t", "description": "d", "method": method}))
    assert decide(tool, OPEN_WORLD_CONFIRM)[0] == decision


def test_a_tool_nobody_has_saved_since_behaves_as_it_did():
    tool = custom_virtual_tool({"name": "old_post", "description": "d", "method": "POST"})
    assert tool.annotations is None
    assert decide(tool, OPEN_WORLD_CONFIRM) == ("allow", "undeclared")


def test_the_builders_read_only_claim_earns_nothing_but_its_writes_are_believed():
    from core.tools import tool_hints

    get = custom_virtual_tool(hinted({"name": "g", "description": "d", "method": "GET"}, by_builder=True))
    assert get.trusted is False and not tool_hints(get, trusted=False).trusted
    delete = custom_virtual_tool(hinted({"name": "x", "description": "d", "method": "DELETE"}, by_builder=True))
    assert decide(delete, OPEN_WORLD_CONFIRM) == ("confirm", "destructive")


async def test_the_routes_save_hints_and_a_persons_save_owns_the_builders():
    from core.routes.tools import _upsert_custom_tools, load_custom_tools

    await _upsert_custom_tools([{"name": "book_room", "description": "d", "method": "POST", "url": "https://x.example",
                                 "annotations_by": BUILDER_HINTS, "annotations": method_hints("POST")}])
    (saved,) = [t for t in await load_custom_tools() if t["name"] == "book_room"]
    assert saved["annotations"] == method_hints("POST") and "annotations_by" not in saved


async def test_the_builders_http_tool_is_saved_with_its_hints_marked_as_its_own():
    import json

    from core.builder_tools import execute_builder_tool
    from core.routes.tools import load_custom_tools

    class _Server:
        agent_sessions: dict = {}

    got = json.loads(await execute_builder_tool("create_http_tool", {
        "name": "cancel_booking", "description": "Cancel a booking.", "method": "DELETE",
        "url": "https://api.example.com/bookings/{id}"}, _Server()))
    assert got.get("status") == "created", got
    (saved,) = [t for t in await load_custom_tools() if t["name"] == "cancel_booking"]
    assert saved["annotations"] == method_hints("DELETE") and saved["annotations_by"] == BUILDER_HINTS
