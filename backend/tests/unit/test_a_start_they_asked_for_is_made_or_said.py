"""How the person asked a run to start is made by the plan, or said on the card with why and what starts it instead
(F1645).

Built from a drawing whose first box was "New email to support@example.com", a plan started "When you run it": nothing
here starts a workflow when an email arrives, and the card never said the start they asked for was gone. The
workflow's own `trigger` field is never read, so no wire was missing to check: what was missing was the plan saying
what it did with the start they asked for.

Now the planner writes `asked_start` when they said how or when a run starts: what they said, and how the plan makes
it start (`schedule`, `form`, or `cannot` with why and what starts it instead). Code holds it to the plan: a
`schedule` start needs the plan's schedule, a `form` start a form as its first step, and a `cannot` its why; a
`cannot` is said on the card under "Missing here".

Near misses: a plan with no `asked_start` is checked as before; a schedule start with its schedule, and a form start
with its form, pass and add nothing to the card.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.native_builder import objects as O
from core.native_builder import render as R

PLANNER = Path(O.__file__).resolve().parent / "agents" / "planner.json"
INVENTORY = O.Inventory(agents={}, workflows={}, tools=set())

ANSWER = {"id": "answer", "type": "llm", "name": "Answer", "does": "Drafts the reply", "prompt": "Reply to {state.input}.",
          "next": "end"}
FORM = {"id": "form", "type": "human", "name": "Expense form", "does": "Asks for the expense",
        "prompt": "What did you spend?", "next": "answer"}
SCHEDULE = {"cron": "*/15 * * * *", "says": "every 15 minutes"}


def _plan(asked_start=None, steps=(ANSWER,), schedule=None) -> O.Plan:
    draft = {"kind": "create", "title": "Support inbox", "summary": "Answers support email.", "steps": list(steps)}
    if asked_start is not None:
        draft["asked_start"] = asked_start
    if schedule is not None:
        draft["schedule"] = schedule
    return O.Plan.from_draft(draft)


def _errors(plan: O.Plan) -> list[str]:
    return [e for e in O.check_structure(plan, INVENTORY)[0] if "asked_start" in e]


EMAIL = "when a new email reaches support@example.com"


class TestTheStartTheyAskedFor:
    def test_the_plan_keeps_what_they_said_and_how_it_is_made(self):
        plan = _plan({"said": EMAIL, "made": "cannot", "why_not": "nothing here starts a workflow when an email "
                                                                  "arrives; it starts when you run it"})
        assert plan.asked_start is not None and plan.asked_start.said == EMAIL and plan.asked_start.made == "cannot"

    def test_a_schedule_start_with_no_schedule_goes_back(self):
        [said] = _errors(_plan({"said": "every 15 minutes", "made": "schedule"}))
        assert "no schedule" in said, said

    def test_a_form_start_whose_first_step_is_no_form_goes_back(self):
        [said] = _errors(_plan({"said": "when someone fills in the expense form", "made": "form"}))
        assert "first step" in said, said

    def test_a_start_it_cannot_make_without_why_goes_back(self):
        [said] = _errors(_plan({"said": EMAIL, "made": "cannot"}))
        assert "why_not" in said, said

    def test_a_start_it_cannot_make_is_said_on_the_card_with_why(self):
        plan = _plan({"said": EMAIL, "made": "cannot",
                      "why_not": "nothing here starts a workflow when an email arrives; run it, or add a schedule "
                                 "that checks the inbox"})
        assert _errors(plan) == []
        plan.off += R.start_off(plan)
        card = R.render(plan)
        assert f"It doesn't start “{EMAIL}”: nothing here starts a workflow when an email arrives" in card, card


class TestNearMisses:
    def test_no_asked_start_is_checked_as_before(self):
        plan = _plan()
        assert plan.asked_start is None and _errors(plan) == [] and R.start_off(plan) == []

    def test_a_schedule_start_with_its_schedule_passes(self):
        plan = _plan({"said": "every 15 minutes", "made": "schedule"}, schedule=SCHEDULE)
        assert _errors(plan) == [] and R.start_off(plan) == []

    def test_a_form_start_with_its_form_passes(self):
        plan = _plan({"said": "when someone fills in the expense form", "made": "form"}, steps=(FORM, ANSWER))
        assert _errors(plan) == [] and R.start_off(plan) == []


class TestThePlanner:
    def test_it_is_told_to_write_the_start_they_asked_for(self):
        prompt = json.loads(PLANNER.read_text(encoding="utf-8"))["system_prompt"]
        assert "`asked_start`" in prompt and "`cannot` with `why_not`" in prompt
