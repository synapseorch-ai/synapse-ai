"""A plan says what it assumed, in plain words on its card, and never guesses silently (F1600).

The rule: "be careful on not making the builder just assume or guess things". a tester's invoice build
checked invoices against "standard supplier terms" though their terms were never asked for, and its card said "against
your terms". Past the question cap, `ASKED_ENOUGH` told the planner "What you pick is on the plan's card for them to
check", but no card section said it: guesses landed under "Missing or off", among things the build cannot do.

Now the planner writes `assumed` (each thing it took as so without the person saying it), code adds its own picks past
the cap (an address never given, a period it took), and the card says them under "What I assumed", before "Missing
here". The card also says how a run starts, from the plan itself (`test_the_card_says_what_the_build_does`).

A plan saved before this (no `assumed`, parts with no `steps` or `when`) still loads and draws as it did.
"""
from __future__ import annotations

from pathlib import Path

from core.native_builder import change_tools as T, objects as O
from core.native_builder.render import render
from test_the_builder_asks_before_it_guesses import SITE, _check, _plan, _run, desk  # noqa: F401

FRONT = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "assistant"

BASE = {"kind": "create", "title": "Supplier invoice check", "summary": "Check each new supplier invoice.",
        "steps": [{"id": "check", "type": "llm", "name": "Check the invoice", "does": "Checks the invoice",
                   "prompt": "Check {{state.invoice}}.", "next": "end"}]}


class TestTheField:
    def test_the_planner_can_write_it(self):
        assert "assumed" in O.plan_schema()["properties"]

    def test_it_is_a_list_of_plain_lines(self):
        draft = O.PlanDraft.model_validate({**BASE, "assumed": ["Invoices are typed in by you, one at a time."]})
        assert draft.assumed == ["Invoices are typed in by you, one at a time."]


class TestTheCard:
    def test_it_says_what_was_assumed_before_what_is_missing(self):
        plan = O.Plan.model_validate({**BASE, "assumed": ["Checked against general terms: add your terms document."],
                                      "missing": ["send_payment"]})
        card = render(plan)
        assert "## What I assumed\n- Checked against general terms: add your terms document." in card
        if "## Missing here" in card:
            assert card.index("## What I assumed") < card.index("## Missing here")

    def test_the_panel_draws_it(self):
        plan_ts = (FRONT / "plan.ts").read_text()
        cards = (FRONT / "cards.tsx").read_text()
        assert "assumed: bullets(view.plan.assumed)" in plan_ts
        assert "What I assumed" in cards and "How it starts" in cards


class TestPastTheCapAGuessIsSaidAsOne:
    async def test_an_address_never_given_is_said_under_what_i_assumed(self, client, desk):
        run = await _run(**{T.ROUNDS_KEY: T.QUESTIONS})
        out = await _check(run, _plan())
        assert out["outcome"] == "review", "past the cap the build goes on"
        assumed = out["review"].split("## What I assumed", 1)
        assert len(assumed) == 2 and SITE in assumed[1].split("\n## ", 1)[0]


class TestOldPlansStillLoad:
    """Saved before F1600: no `assumed`, parts with no `steps` or `when`."""

    OLD = {**BASE, "asked": [{"part": "check each new supplier invoice", "how": "plan"},
                             {"part": "pay them", "how": "not_done", "why_not": "Paying is not done here."}]}

    def test_a_saved_plan_without_them_loads(self):
        plan = O.Plan.model_validate(self.OLD)
        assert plan.assumed == [] and all(p.steps == [] and p.when is None for p in plan.asked)

    def test_it_draws_with_no_empty_section(self):
        card = render(O.Plan.model_validate(self.OLD))
        assert "## What I assumed" not in card and "## You asked for" in card
