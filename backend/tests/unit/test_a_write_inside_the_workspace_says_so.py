"""A tool that declares it changes something and does not reach outside is filed as such (TAG1, D296, F736).

Once a person can untick "reaches outside" on a custom tool, a declared write that stays inside the workspace fell
through `decide()` to `undeclared`, and its approval card and refusal said "it does not declare what it does" about a
tool that had. The decision stays the `undeclared` policy's (no new key, never looser than before); the category and
the reason say what the tool declared.
"""
from __future__ import annotations

from core.tool_policy import CATEGORIES, REASONS, clean_policy, decide, explain
from core.tools import VirtualTool

STRICT = {"destructive": "deny", "open_world": "allow", "undeclared": "confirm"}
INSIDE_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


def test_a_write_that_stays_inside_says_it_changes_something():
    assert decide(VirtualTool("t", "", {}, annotations=INSIDE_WRITE), STRICT) == ("confirm", "changes")
    assert explain("changes") == REASONS["changes"] == "it changes something in this workspace"


def test_it_is_decided_by_the_undeclared_policy_and_adds_no_key():
    tool = VirtualTool("t", "", {}, annotations=INSIDE_WRITE)
    assert decide(tool, {**STRICT, "undeclared": "allow"})[0] == "allow"
    assert "changes" not in CATEGORIES
    assert clean_policy({"changes": "deny"}) == {}


def test_a_stranger_or_the_builder_saying_it_only_reads_is_still_not_known():
    tool = VirtualTool("t", "", {}, annotations={"readOnlyHint": True, "openWorldHint": False}, trusted=False)
    assert decide(tool, STRICT) == ("confirm", "undeclared")


def test_nothing_declared_is_still_undeclared():
    assert decide(VirtualTool("t", "", {}), STRICT) == ("confirm", "undeclared")
