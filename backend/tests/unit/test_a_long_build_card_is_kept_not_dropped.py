"""A build's or a change's card on Synapse AI's board is kept however long its review, and a card not kept is said
(F1750).

Each build or change is one card on Synapse AI's board: its stage, its review as the body, its typed plan or change as
`data`, which code reads back (a change's picks, "the second one", undo). A board keeps a body of at most 4,000
characters, and a longer review made the whole card go unwritten, `data` with it, with only a log line to say so. Now
a body over the limit is cut at a break to fit, never leaving a code fence or a bold open, and ends by saying where
the whole plan is; `data` is kept whole. A card that still cannot be kept is said on the review, never stopping the
build or the change.

Near misses read as before: a body that fits, one of exactly 4,000 characters, a turn's own note card.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.boards import assistant
from core.store import boards as store

LIMIT = store.BODY_MAX


def _long(n: int = 6500) -> str:
    paragraph = "The desk reads each new order, drafts the welcome email and plans the first delivery. "
    out, i = "## What it does\n", 0
    while len(out) < n:
        out += paragraph * 3 + f"(part {i})\n\n"
        i += 1
    return out


class TestTheBodyFits:
    def test_a_long_review_is_cut_at_a_break_and_says_where_the_rest_is(self):
        body = assistant.fitted_body(_long())
        assert len(body) <= LIMIT and body.endswith(assistant.CUT_LINE)
        kept = body[:-len(assistant.CUT_LINE)].rstrip()
        assert kept.endswith(")"), "cut where a paragraph ends, never mid-sentence"

    def test_a_fence_across_the_limit_is_closed(self):
        body = "## Code\n" + "Words before the code. " * 160 + "\n\n```python\n" + "print('a line of code')\n" * 200 + \
            "```\nAfter."
        assert body.index("```") < LIMIT < len(body)
        out = assistant.fitted_body(body)
        assert len(out) <= LIMIT and out.count("```") % 2 == 0 and out.endswith(assistant.CUT_LINE)

    def test_an_open_bold_is_never_left_open(self):
        body = "Plain words here. " * 215 + "**This bold part runs on " + "and on " * 200 + "to its end.**"
        out = assistant.fitted_body(body)
        assert len(out) <= LIMIT and out.count("**") % 2 == 0

    @pytest.mark.parametrize("body", ["A short review.", "x" * LIMIT])
    def test_a_body_that_fits_is_as_it_was(self, body):
        assert assistant.fitted_body(body) == body


class TestTheCardIsKept:
    async def test_a_long_builds_card_is_written_with_its_whole_plan(self):
        plan = {"title": "Team onboarding", "steps": [{"id": f"s{i}", "does": "x" * 50} for i in range(40)]}
        card = await assistant.put("builder_f1750_a", title="Team onboarding", stage="waiting", owner_id="u1",
                                   body=_long(), data={"plan": {"plan": plan}})
        assert len(card.body) <= LIMIT and card.body.endswith(assistant.CUT_LINE)
        assert (await assistant.card("builder_f1750_a")).data["plan"]["plan"] == plan

    async def test_a_long_changes_picks_are_read_back(self):
        picks = [{"op": "set_agent", "field": "agent", "wanted": "the desk",
                  "options": [{"id": "agent_a", "name": "Desk A"}, {"id": "agent_b", "name": "Desk B"}]}]
        await assistant.put("builder_f1750_b", title="A change", stage="waiting", owner_id="u1", body=_long(),
                            data={"change": {"picks": picks}})
        assert (await assistant.card("builder_f1750_b")).data["change"]["picks"] == picks

    async def test_a_turns_note_card_is_as_it_was(self):
        await assistant.note("builder_f1750_c", owner_id="u1", connects=["Gmail"])
        assert (await assistant.turn("builder_f1750_c"))["connects"] == ["Gmail"]


class TestACardNotKeptIsSaid:
    @pytest.fixture
    def refused(self, monkeypatch):
        async def put(*a, **k):
            raise store.BadValue("The board could not keep it.")

        monkeypatch.setattr(assistant, "put", put)

    async def test_on_a_builds_review(self, monkeypatch, refused):
        from core.native_builder import build_plan as B

        monkeypatch.setattr(B, "_run", lambda: ("builder_f1750_d", None))
        monkeypatch.setattr(B, "_caller", lambda: SimpleNamespace(user_id="u1"))
        checked = {"plan": {"title": "Team onboarding"}, "review": "## Plan", "say": "Here is the plan."}
        await B._card(checked)
        assert checked["say"] == f"Here is the plan. {B.NOT_KEPT}"

    async def test_on_a_changes_review(self, monkeypatch, refused):
        from core.native_builder import change_tools as C

        monkeypatch.setattr(C, "_run", lambda: ("builder_f1750_e", None))
        monkeypatch.setattr(C, "_caller", lambda: SimpleNamespace(user_id="u1"))
        checked = {"ops": [], "review": "## Change", "say": ""}
        await C._card(checked, stage="waiting")
        assert checked["say"] == C.CHANGE_NOT_KEPT
