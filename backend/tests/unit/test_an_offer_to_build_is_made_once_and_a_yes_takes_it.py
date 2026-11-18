"""An offer to build is made once, and a yes to it builds what was asked (F1671, F1697).

Asked about a page, Synapse AI offered to build a workflow for it mid-reply ("Would you like me to build that? Just say
where…"), and code then added its own offer after it, so the reply offered twice (F1671): code's line was added
whenever the reply's last character was not a question mark. And asked to make a new workflow, it described the
workflow and asked "Shall I go ahead and plan this out?"; the yes that followed got the same description again and
no plan (F1697): nothing kept the offer it had made, so the yes had nothing behind it.

Now a reply that already offers to build or plan, in any of its questions, gets no second offer; and such an offer,
made where no offer was kept, keeps the person's ask, so the next yes builds it.

Near misses: a reply that asks something else ("Which page did you mean?") is no offer: it still gets code's line
where an offer was kept, and keeps no offer of its own; a viewer's turn keeps none.
"""
from __future__ import annotations

import types

import pytest

S1 = ("The json module encodes Python objects as JSON and decodes JSON back. Its main functions are dumps, loads, "
      "dump and load. Would you like me to build that? Just say where to send it — your email or a Slack channel.")
S7 = ("I can't read your GitHub settings page without your sign-in, and the random page changes with each visit. "
      "Would you like me to set either of those up as a workflow? It could run on a schedule.")
PLANS = ("Here's what that workflow would do: it reads the new support tickets, then an AI step writes a reply for "
         "each one. Shall I go ahead and plan this out?")
LIVE = {"fact": "the euro to dollar rate", "ask": "Build a workflow that gets the euro to dollar rate each time"}


def _ap():
    from core.native_builder import answer_pack

    return answer_pack


@pytest.mark.parametrize("reply", [
    S1, S7, PLANS,
    "Want me to make that a workflow? It would run every Monday.",
    "Should I schedule it for 9am each weekday?",
    "I can draft that for you. Do you want me to create it now?",
])
def test_a_reply_that_offers_to_build_or_plan_is_an_offer(reply):
    assert _ap().offers_to_build(reply), reply


@pytest.mark.parametrize("reply", [
    "Which page did you mean?",
    "Do you mean Python 3.12 or 3.13?",
    "Would you like the summary shorter?",
    "Do you want me to explain how its loads function reads a file?",
    "It runs every Monday at 9am. The last run failed on its second step.",
    "Your workflow 'Build report' ran today. Anything else?",
    "I can't make that here. Which page did you mean?",
])
def test_a_reply_that_asks_something_else_is_no_offer(reply):
    assert not _ap().offers_to_build(reply), reply


@pytest.mark.parametrize("reply", [S1, S7])
def test_a_reply_that_already_offers_gets_no_second_offer(reply):
    out = _ap().offer_kept(reply, LIVE)
    assert out == reply and _ap().offer_line(LIVE["fact"]) not in out, out


@pytest.mark.parametrize("reply", ["The euro is worth about 1.08 dollars today. Which day did you mean?",
                                   "The rate moves through the day, so I can't tell you it here."])
def test_a_reply_that_makes_no_offer_still_gets_codes(reply):
    assert _ap().offer_line(LIVE["fact"]) in _ap().offer_kept(reply, LIVE)


class TestAYesHasItsOfferBehindIt:
    def test_an_offer_to_plan_keeps_the_persons_ask(self):
        kept = _ap().offer_to_keep(PLANS, None, "Make a new workflow that reads support tickets and drafts replies",
                                   may_build=True)
        assert kept == {"fact": "", "ask": "Make a new workflow that reads support tickets and drafts replies"}, kept

    def test_an_offer_already_kept_is_left_as_it_is(self):
        assert _ap().offer_to_keep(S1, LIVE, "Read the json docs", may_build=True) is None

    def test_a_reply_that_asks_something_else_keeps_none(self):
        assert _ap().offer_to_keep("Which page did you mean?", None, "Summarise the docs", may_build=True) is None

    def test_a_viewers_turn_keeps_none(self):
        assert _ap().offer_to_keep(PLANS, None, "Make a new workflow", may_build=False) is None

    async def test_the_answer_check_keeps_it_for_the_next_yes(self, monkeypatch):
        from core.native_builder import answer_pack, change_tools
        from core.native_builder.classify import OFFERED_KEY

        state = {"summary": PLANS, "user_message": "Make a new workflow that reads tickets", "may_change": True}
        monkeypatch.setattr(change_tools, "_run", lambda: ("run_f1697", types.SimpleNamespace(shared_state=state)))
        out = await answer_pack.check_answer({}, None, [])
        assert state.get(OFFERED_KEY) == {"fact": "", "ask": "Make a new workflow that reads tickets"}, state
        assert "Shall I go ahead and plan this out?" in out["text"], out
