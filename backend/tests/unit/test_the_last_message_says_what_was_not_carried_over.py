"""The build's last message says what the plan did not carry over, as its card does (F1246, F1228's rule).

TRI1's class 6 (10-29): a plan from a pasted program lists what it leaves behind under "Not carried over", on the card
only (render.py); the build's last message, made from the plan's parts (F1228), said each part and what was missing
or off, and never what was left behind, so a person reading only the chat thought it all came across. The
make-new route's not-made lines are CAP3's (F1242, check_made).
"""
from __future__ import annotations

from core.native_builder import objects as O
from core.native_builder import parts_words as PW


def _plan(**code) -> O.Plan:
    plan = O.Plan.from_draft(O.PlanDraft(kind="create", title="Lead router", summary="routes new leads", steps=[
        O.Step(id="route", type="llm", name="Route the lead", does="picks the owner", next="end")],
        not_carried_over=code.pop("not_carried_over", [])))
    plan.step_words, plan.part_words, plan.off = PW.describe(plan, titles={})
    return plan


def test_what_was_not_carried_over_is_in_the_last_message():
    text = PW.summary(_plan(not_carried_over=["Zapier's Filter step: a condition on the lead's country",
                                              "the Slack DM to the sales lead"]))
    assert text.endswith("\n\n**Not carried over**\n- Zapier's Filter step: a condition on the lead's country\n"
                         "- The Slack DM to the sales lead")
    assert text.index("**What it does**") < text.index("**Not carried over**")


def test_it_comes_after_what_is_missing_or_off():
    plan = _plan(not_carried_over=["the Slack DM"])
    plan.off = ["its schedule is made paused: turn it on under Schedules"]
    text = PW.summary(plan)
    assert text.index("**Missing or off**") < text.index("**Not carried over**")


def test_a_plan_that_left_nothing_behind_says_nothing_of_it():
    assert "Not carried over" not in PW.summary(_plan())
