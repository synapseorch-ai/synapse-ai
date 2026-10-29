"""The Asked card lists each choice once (F1220, Naveen's prod turn 10-29).

Synapse AI's question for a "fix all the bugs" change showed its choices 1–3, then repeated them as 4–5 ("Fix the
missing prompt templates…", "Reb…"). The card is the question in Markdown with the planner's `options` appended as a
numbered list; the planner had numbered the same choices in its question too, and Markdown ran the two lists into one.
Now a list in the question is lifted into the choices and a repeated choice is kept once, at the source (`ask_person`)
and where the card's words are made, on both the build and the change route.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — the builder's tool modules load in their own order first
from core.native_builder.change_tools import asked_options, asked_text  # noqa: E402

from tests.unit.test_the_build_route_decides_in_code import _check as _check_build, _run as _build_run
from tests.unit.test_the_change_route_decides_in_code import _call, _run as _change_run

QUESTION = ("There are several bugs in this workflow. Which should I fix first?\n"
            "1. Fix the missing prompt templates in the two agents\n"
            "2. Rebuild the broken route after the check\n"
            "3. Remove the duplicate schedule")
OPTIONS = ["Fix the missing prompt templates in the two agents", "Rebuild the broken route after the check"]


def _numbered(text: str) -> list[str]:
    return [line for line in text.splitlines() if line[:1].isdigit()]


class TestTheCardsWords:
    def test_a_list_in_the_question_and_the_options_are_one_list(self):
        text = asked_text(QUESTION, OPTIONS)
        assert _numbered(text) == ["1. Fix the missing prompt templates in the two agents",
                                   "2. Rebuild the broken route after the check",
                                   "3. Remove the duplicate schedule"]
        assert text.startswith("There are several bugs in this workflow. Which should I fix first?\n1. ")

    @pytest.mark.parametrize("again", ["fix the missing prompt templates in the two agents.",
                                       "FIX THE MISSING PROMPT TEMPLATES IN THE TWO AGENTS"])
    def test_a_choice_said_twice_is_kept_once_as_first_said(self, again):
        _, kept = asked_options("Which first?", [again, "Rebuild the broken route after the check",
                                                 "Fix the missing prompt templates in the two agents"])
        assert kept == [again, "Rebuild the broken route after the check"]

    def test_a_choice_cut_short_gives_way_to_its_whole_words(self):
        _, kept = asked_options("Which first?", ["Fix the missing prompt templates…", "Rebuild the broken route",
                                                 "Fix the missing prompt templates in the two agents"])
        assert kept == ["Fix the missing prompt templates in the two agents", "Rebuild the broken route"]

    def test_bullets_are_lifted_as_numbers_are(self):
        question, kept = asked_options("Which list?\n- Patients\n• Staff", [])
        assert (question, kept) == ("Which list?", ["Patients", "Staff"])

    def test_plain_options_read_as_before(self):
        assert asked_text("Which list?", ["Patients", "Staff"]) == "Which list?\n1. Patients\n2. Staff"
        assert asked_text("What time?", []) == "What time?"

    def test_short_choices_that_only_share_a_start_are_both_kept(self):
        assert asked_options("Which?", ["Daily", "Daily at 9"])[1] == ["Daily", "Daily at 9"]

    @pytest.mark.parametrize("question", ["Which one?\n1. Only one listed", "1. Patients\n2. Staff"])
    def test_one_listed_line_or_a_list_only_question_keeps_its_words(self, question):
        assert asked_options(question, [])[0] == question


# ── at the source, and on both routes ─────────────────────────────────────────────────────────────────────────────
async def test_ask_person_hands_on_the_choices_once(client):
    asked, _ = await _call("ask_person", {"question": QUESTION, "about": "which", "options": OPTIONS},
                           await _change_run())
    assert asked["question"] == "There are several bugs in this workflow. Which should I fix first?"
    assert asked["options"] == ["Fix the missing prompt templates in the two agents",
                                "Rebuild the broken route after the check", "Remove the duplicate schedule"]


async def test_the_change_route_s_card_lists_each_once(client):
    run = await _change_run()
    out, _ = await _call("check_change", {"draft": {"finish": "ask_person", "question": QUESTION, "options": OPTIONS}},
                         run)
    assert out["outcome"] == "ask" and len(_numbered(out["question"])) == 3


async def test_the_build_route_s_card_lists_each_once(client):
    out, _ = await _check_build(await _build_run(), {"finish": "ask_person", "question": QUESTION, "about": "which",
                                                     "options": OPTIONS})
    assert out["outcome"] == "ask" and len(_numbered(out["question"])) == 3
