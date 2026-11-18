"""A website widget they asked for, which a build doesn't make, is said as not made, never claimed (F1668).

Asked for "a website chat widget that answers from our FAQ", a build made an agent and a knowledge base, and its card
said under "You asked for" that it was a website chat widget; no widget was made, and the Widgets screen said "No
widgets yet". A widget is made on its own card once its agent exists; a build plan has no widget of its own, so
nothing held the plan to saying it was not made here.

Now a create plan whose request asks for a widget on the person's site, and whose `asked` does not say the widget is
not done, is sent back once a run to say it; if it still does not, code says it under "Not in this plan", with where
the widget is made.

Near misses: a widget that is not on their site ("a dashboard widget that shows sales", "a widget on the Boards
screen", "the widget's colour") is never read as one; a plan that already says the widget is not done is untouched.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan
from core.native_builder import objects as O

WS = build_plan.Workspace()
ASK = "Make a website chat widget that answers our customers from the FAQ at faq.example.com."


def _draft(asked=()) -> dict:
    return {"kind": "create", "title": "FAQ answers", "summary": "A website chat widget that answers from the FAQ.",
            "steps": [{"id": "answer", "type": "agent", "name": "Answer", "does": "Answers a customer",
                       "agent": "Helper", "prompt": "Answer {{ state.user_input }}.", "next": "end"}],
            "agents": [{"name": "Helper", "purpose": "Answers customers from the FAQ"}],
            "asked": list(asked)}


async def _examined(draft: dict, state: dict):
    return await build_plan.examine(draft, None, ws=WS, state=state)


def _widget_lines(errors: list[str]) -> list[str]:
    return [e for e in errors if "website widget" in e]


class TestTheAsk:
    @pytest.mark.parametrize("words", [
        ASK, "I want a chat widget for my site", "Put a chat on our website that answers questions",
        "Embed the support agent on my website", "add a widget to our web site where visitors can ask"])
    def test_a_widget_on_their_site_is_read(self, words):
        assert build_plan.asks_for_a_website_widget(words), words

    @pytest.mark.parametrize("words", [
        "Make a dashboard widget that shows sales", "Add a widget on the Boards screen", "Change the widget's colour",
        "Summarise our website's FAQ every Monday", "Check the site is up every hour"])
    def test_a_near_miss_is_never_read_as_one(self, words):
        assert not build_plan.asks_for_a_website_widget(words), words


class TestThePlanCheck:
    async def test_a_plan_that_claims_nothing_is_sent_back_once(self):
        state = {"user_message": ASK}
        [said] = _widget_lines((await _examined(_draft(), state)).errors)
        assert "a build doesn't make" in said and "not_done" in said, said
        assert _widget_lines((await _examined(_draft(), state)).errors) == [], "sent back once a run, never a loop"

    async def test_a_plan_that_says_it_is_not_done_is_untouched(self):
        asked = [{"part": "A website chat widget", "how": "not_done",
                  "why_not": "a widget is made on its own card once the agent exists"}]
        assert _widget_lines((await _examined(_draft(asked), {"user_message": ASK})).errors) == []

    async def test_a_near_miss_ask_is_never_sent_back(self):
        state = {"user_message": "Make a dashboard widget that shows sales"}
        assert _widget_lines((await _examined(_draft(), state)).errors) == []


class TestTheCard:
    def test_still_missing_it_is_said_as_not_in_this_plan_with_where_it_is_made(self):
        plan = O.Plan.from_draft(_draft())
        build_plan.widget_said(plan, {"user_message": ASK})
        [part] = [a for a in plan.asked if a.how == "not_done"]
        assert "website widget" in part.part.lower(), part
        assert "Helper" in part.why_not and "Widgets" in part.why_not, part

    def test_said_once_and_never_when_already_said(self):
        plan = O.Plan.from_draft(_draft())
        build_plan.widget_said(plan, {"user_message": ASK})
        build_plan.widget_said(plan, {"user_message": ASK})
        assert len([a for a in plan.asked if a.how == "not_done"]) == 1
