"""A part of the ask is done on every path of the run, or the plan says when it is not (F1600).

a tester (on prod) took our starter "Check each new supplier invoice against our terms, and ask me before
paying any over $1,000." The plan's `asked` said "check each" was done, but its terms check sat on the "over $1,000"
arm of an if/else: an invoice of $1,000 or less went straight to "Approved", never checked. Nothing tied a part of the
ask to the steps doing it, so nothing could see that.

Now a part the plan does (`how: plan`) names its `steps`, and may say `when` it applies (the condition the person
gave, "only over $1,000"). A part whose steps all sit on one arm of an if/else or switch, with no `when`, is said back
to the planner once, as a structure finding (`build_plan._STRUCTURE_SAID_KEY`: each finding said once per build,
then the plan is taken as it is). The build is never blocked by it and never loops: taken as it is, the card says
under "Missing here" that the part is done only on that arm.

Parts with no `steps` (a plan saved before this, or a planner that left them out) are not checked: a missing `steps`
is said once too, then the part goes as it is. Parts that are `run_once` or `not_done` are never checked.
"""
from __future__ import annotations

from core.native_builder import build_plan, objects as O
from test_the_builder_asks_before_it_guesses import _call, _check, _run, desk  # noqa: F401

ASK = "Check each new supplier invoice against our terms, and ask me before paying any over $1,000."


def invoices(*, check_on_every_path: bool = False, when: str | None = None, steps: list[str] | None = None,
             how: str = "plan") -> dict:
    """a tester's plan, near enough: a form, then over $1,000?, the terms check on one arm (or before the branch)."""
    if check_on_every_path:
        flow = [{"id": "check", "type": "llm", "name": "Check against the terms", "does": "Checks the invoice",
                 "prompt": "Check {{state.invoice}} against the terms.", "next": "over"},
                {"id": "over", "type": "if_else", "name": "Over $1,000?", "does": "Splits by amount",
                 "condition": "state.invoice.amount > 1000", "branches": {"true": "approve", "false": "end"}}]
    else:
        flow = [{"id": "over", "type": "if_else", "name": "Over $1,000?", "does": "Splits by amount",
                 "condition": "state.invoice.amount > 1000", "branches": {"true": "check", "false": "end"}},
                {"id": "check", "type": "llm", "name": "Check against the terms", "does": "Checks the invoice",
                 "prompt": "Check {{state.invoice}} against the terms.", "next": "approve"}]
    part = {"part": "check each new supplier invoice against our terms", "how": how,
            **({"steps": steps if steps is not None else ["check"]} if how == "plan" else {}),
            **({"when": when} if when else {}), **({"why_not": "Not here."} if how == "not_done" else {})}
    return {"finish": "submit_plan", "kind": "create", "title": "Supplier invoice approval",
            "summary": "Check each new supplier invoice, and ask before paying any over $1,000.",
            "steps": [{"id": "submit", "type": "human", "name": "Enter the invoice", "does": "You enter the invoice",
                       "prompt": "The invoice's supplier, number and amount", "next": flow[0]["id"]},
                      *flow,
                      {"id": "approve", "type": "human", "name": "Approve payment", "does": "Asks you to approve",
                       "prompt": "Approve paying this invoice?", "next": "end"}],
            "asked": [part, {"part": "ask me before paying any over $1,000", "how": "plan", "steps": ["approve"],
                             "when": "over $1,000"}]}


class TestTheFields:
    def test_a_part_names_its_steps_and_when(self):
        part = O.PlanPart.model_validate({"part": "x", "how": "plan", "steps": ["check"], "when": "over $1,000"})
        assert part.steps == ["check"] and part.when == "over $1,000"

    def test_both_are_optional(self):
        part = O.PlanPart.model_validate({"part": "x", "how": "plan"})
        assert part.steps == [] and part.when is None


class TestTheCheck:
    async def test_a_part_on_one_arm_is_said_back_once_naming_the_arm(self, client, desk):
        run = await _run(ASK)
        out = await _call("submit_plan", invoices(), run)
        assert "error" in out and "said once" in out["error"]
        assert "check each new supplier invoice" in out["error"] and "Over $1,000?" in out["error"]

    async def test_taken_as_it_is_the_build_goes_on_and_the_card_says_it(self, client, desk):
        run = await _run(ASK)
        await _call("submit_plan", invoices(), run)
        again = await _call("submit_plan", invoices(), run)
        assert again.get("finish") == "submit_plan", "said once, then the plan as it is: never a loop"
        out = await _check(run, again)
        assert out["outcome"] == "review" and "Can't be saved yet" not in out["review"]
        missing = out["review"].split("## Missing here", 1)
        assert len(missing) == 2 and "check each new supplier invoice" in missing[1]

    async def test_a_part_that_says_when_passes(self, client, desk):
        out = await _call("submit_plan", invoices(when="only invoices over $1,000"), await _run(ASK))
        assert out.get("finish") == "submit_plan"

    async def test_a_part_done_on_every_path_passes(self, client, desk):
        out = await _call("submit_plan", invoices(check_on_every_path=True), await _run(ASK))
        assert out.get("finish") == "submit_plan"

    async def test_a_part_not_done_is_never_checked(self, client, desk):
        out = await _call("submit_plan", invoices(how="not_done"), await _run(ASK))
        assert "Over $1,000?" not in (out.get("error") or "")


class TestOldAndPartialDraftsGoOn:
    def test_a_part_with_no_steps_is_not_checked(self):
        plan = O.Plan.model_validate({k: v for k, v in invoices(steps=[]).items() if k != "finish"})
        assert build_plan.parts_off_a_path(plan) == []

    def test_the_part_on_one_arm_is_found(self):
        plan = O.Plan.model_validate({k: v for k, v in invoices().items() if k != "finish"})
        [(part, arm)] = build_plan.parts_off_a_path(plan)
        assert part.part.startswith("check each") and arm == "Over $1,000?"
