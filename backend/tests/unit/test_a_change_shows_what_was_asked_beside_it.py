"""A change's review shows what the planner said it does beside the exact change, and the planner splits a range rather
than rewrite all of it (F1308, TRI1; CAP3's tier 1 prod-04; Naveen 11-03: "Build it as restated").

prod-04's person asked to close at 6pm on Fridays. The planner's op said "Change Friday closing time from 7pm to 6pm",
but its find/replace rewrote "Monday to Friday, 8am to 7pm" to close at 6pm every weekday, and the review showed only
the before and after, so the person approved it. Code can't tell which part of a range a person meant, so two answers:

* **the review:** under every ready op's line, the op's own `says` on a line of its own, "Described as: …", above its
  before and after, so a diff that does more than the words is seen before the yes (scrubbed like the rest of the
  card). The line itself stays code's alone (MCP2's note 3): the model's gloss is told apart from it, never in it;
* **the prompt:** rule 12 under # Changing says to split a range or a list so the rest reads as it did, with the
  example; the numbering of # Changing stays as it was.
"""
from __future__ import annotations

import re

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from test_a_knowledge_note_is_changed_in_place import _change, _edit, world  # noqa: F401 — fixtures
from test_the_change_route_decides_in_code import _check, fresh, role  # noqa: F401 — fixtures

SAYS = "Change Friday closing time from 5 to 4"


@pytest.mark.usefixtures("knowledge_store", "world")
class TestTheReview:
    async def test_prod_04s_op_shows_what_it_says_above_its_before_and_after(self, world):
        draft = _edit(world["hours"].id, find="Open 9 to 5, Monday to Friday.", replace="Open 9 to 4, Monday to Friday.")
        draft["ops"][0]["says"] = SAYS
        _, checked, _ = await _change(draft)
        review = checked["review"]
        assert f"\n  Described as: {SAYS}\n" in review, review
        assert review.index("Described as:") < review.index("before:"), "the words come before the diff"
        assert "[5]" in review and "[4]" in review, "the diff is still shown"

    async def test_an_op_without_says_reads_as_it_did(self, world):
        _, checked, _ = await _change(_edit(world["note"].id, find="within 30 days", replace="within 60 days"))
        assert "Described as" not in checked["review"], checked["review"]
        assert "before:" in checked["review"]


async def test_a_new_thing_shows_what_it_says_too(client, role):
    out, _, _ = await _check([{"op": "create", "target": {"kind": "skill", "name": "Mailchimp"},
                               "fields": {"what": "lists their audiences"}, "says": "Make a Mailchimp skill"}])
    assert out["review"] == '- Make the skill "Mailchimp" — lists their audiences\n  Described as: Make a Mailchimp skill'


def _changing_rules() -> list[str]:
    from core.native_builder.seed import bundled_definitions

    planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
    prompt = planner["system_prompt"]
    section = prompt.split("\n# Changing\n", 1)[1].split("\n# ", 1)[0]
    return re.findall(r"^(\d+)\. (.*)$", section, re.M)


class TestRuleTwelve:
    def test_a_range_is_split_so_the_rest_reads_as_it_did(self):
        rule = dict(_changing_rules())["12"]
        assert rule.startswith("A knowledge base's note or text file is changed with knowledge_edit_document"), rule
        assert "split it so the rest reads as it did" in rule
        assert "Monday to Thursday" in rule and "Friday" in rule

    def test_the_numbering_stands(self):
        numbers = [int(n) for n, _ in _changing_rules()]
        assert numbers == list(range(1, len(numbers) + 1)) and len(numbers) >= 21
