"""A custom tool's behaviour tags: read-only, destructive, safe to repeat, reaches outside (TAG1, D296, F733).

The four are MCP's tool annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint). A new HTTP tool
gets them from its method, the person sees and changes them, and the policy, a Tool step's retries and the builder's
checks read them. Naveen's calls (2026-10-17): every HTTP tool reaches outside unless the person unticks it (n8n at
localhost still sends the email); a tool saved before F488 stays "not set"; a Python tool starts not set.
"""
from __future__ import annotations

import pytest

from core.tool_tags import (
    HINT_OF, METHOD_TAGS, TAGS, clean_hints, defaults_for, hints_of, may_repeat, tags_of, tighten,
)


@pytest.mark.parametrize("method,read_only,destructive,idempotent", [
    ("GET", True, False, True), ("head", True, False, True), ("OPTIONS", True, False, True), (None, True, False, True),
    ("PUT", False, False, True),
    ("POST", False, False, False), ("PATCH", False, False, False),
    ("DELETE", False, True, True),
])
def test_the_method_gives_the_defaults(method, read_only, destructive, idempotent):
    tags = defaults_for(method)
    assert tags == {"read_only": read_only, "destructive": destructive, "idempotent": idempotent, "open_world": True}


def test_an_unknown_method_is_taken_as_a_write():
    assert defaults_for("PROPFIND") == METHOD_TAGS["POST"]


def test_the_tags_are_mcps_hints():
    assert TAGS == ("read_only", "destructive", "idempotent", "open_world")
    assert HINT_OF == {"read_only": "readOnlyHint", "destructive": "destructiveHint",
                       "idempotent": "idempotentHint", "open_world": "openWorldHint"}


def test_a_write_stores_all_four_and_a_read_needs_no_destructive_hint():
    assert hints_of(defaults_for("POST")) == {"readOnlyHint": False, "destructiveHint": False,
                                              "idempotentHint": False, "openWorldHint": True}
    # "destructiveHint is meaningful only when readOnlyHint is false" (MCP); F488's saved GETs carry these three.
    assert hints_of(defaults_for("GET")) == {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}


@pytest.mark.parametrize("method", ["GET", "PUT", "POST", "PATCH", "DELETE"])
def test_hints_and_tags_go_round(method):
    assert tags_of(hints_of(defaults_for(method))) == defaults_for(method)


def test_reading_follows_the_engines_rule():
    from core.tools import VirtualTool, tool_hints

    for ann in ({"readOnlyHint": False}, {"readOnlyHint": False, "destructiveHint": False}, {"readOnlyHint": True},
                {"readOnlyHint": True, "destructiveHint": True}, {}, {"openWorldHint": True, "idempotentHint": True}):
        hints = tool_hints(VirtualTool("t", "", {}, annotations=ann))
        assert tags_of(ann) == {t: getattr(hints, t) for t in TAGS}, ann
    # A write that does not say otherwise is taken as destructive, as MCP's own default is.
    assert tags_of({"readOnlyHint": False})["destructive"] is True


def test_nothing_declared_is_not_set():
    assert tags_of(None) is None and tags_of("junk") is None


def test_the_builder_can_only_make_a_tool_stricter():
    get, post = defaults_for("GET"), defaults_for("POST")
    # It can say a GET deletes something (a GET that cancels a booking); it cannot say a POST only reads.
    assert tighten(get, {"destructive": True})["destructive"] is True
    assert tighten(get, {"destructive": True})["read_only"] is False
    assert tighten(post, {"read_only": True}) == post
    assert tighten(post, {"idempotent": True}) == post
    assert tighten(get, {"open_world": False})["open_world"] is True
    assert tighten(get, {"idempotent": False})["idempotent"] is False
    assert tighten(get, {}) == get and tighten(get, None) == get


def test_unknown_hints_are_dropped_and_values_are_booleans():
    assert clean_hints({"readOnlyHint": True, "title": "x", "openWorldHint": 0}) == {"readOnlyHint": True,
                                                                                    "openWorldHint": False}
    assert clean_hints("junk") is None and clean_hints(None) is None


@pytest.mark.parametrize("tool,repeat", [
    ({"method": "POST", "annotations": hints_of(defaults_for("POST"))}, False),
    ({"method": "PUT", "annotations": hints_of(defaults_for("PUT"))}, True),
    ({"method": "GET", "annotations": hints_of(defaults_for("GET"))}, True),
    # The person says this POST is safe to repeat (an upsert by key).
    ({"method": "POST", "annotations": {**hints_of(defaults_for("POST")), "idempotentHint": True}}, True),
    # A tool saved before F488: its method decides, as HTTP does.
    ({"method": "POST"}, False), ({"method": "DELETE"}, True), ({}, True),
    # A Python tool nobody has tagged keeps today's retry.
    ({"tool_type": "python", "code": "x"}, True),
    ({"tool_type": "python", "code": "x", "annotations": {"readOnlyHint": False, "idempotentHint": False}}, False),
    # Reading changes nothing, so it can be asked again whatever it says about repeating.
    ({"tool_type": "python", "annotations": {"readOnlyHint": True}}, True),
])
def test_what_may_be_repeated(tool, repeat):
    assert may_repeat(tool) is repeat


def test_a_listed_tool_keeps_the_hints_it_set():
    from mcp.types import Tool, ToolAnnotations

    from core.tool_tags import listed_hints

    told = Tool(name="deploy", inputSchema={"type": "object"},
                annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False, title="Deploy"))
    assert listed_hints(told) == {"destructiveHint": True, "openWorldHint": False}
    assert listed_hints(Tool(name="x", inputSchema={"type": "object"})) is None
    assert listed_hints({"name": "x", "annotations": {"readOnlyHint": 1, "title": "t"}}) == {"readOnlyHint": True}
    assert listed_hints({"name": "x"}) is None
