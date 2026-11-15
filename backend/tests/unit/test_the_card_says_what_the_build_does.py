"""The plan card says what the build does, from the plan itself, not from the planner's words about it (F1600).

a tester's card (on prod) said the build "Automatically checks new supplier invoices" against "your terms":
the run started when a person typed each invoice into a form, and it checked against general terms the planner made
up. The card's "You asked for" is the planner's `summary`, which nothing held to the plan.

The class, fixed from the plan's structure (never by matching words where structure can decide):
- **How it starts** is drawn from the plan: its schedule; else a person's form, when its first step is one; else when
  someone runs it. Whatever a summary says, the card says how a run really starts, so "automatically" on a form-started
  build no longer stands alone, and nothing is sent back for the word.
- **The person's own terms, policy or rules**: structure can't tell whether a step relies on a document of theirs, so
  this one is said back once (a structure finding, `build_plan._STRUCTURE_SAID_KEY`), and only when nothing in the plan
  gives it to them: no knowledge base, no Vault file it reads, no form field asking for it, and no line of `assumed`
  saying general ones are used. Taken as it is, code says it under "What I assumed". Never a block, never a loop.
"""
from __future__ import annotations

from core.native_builder import objects as O
from core.native_builder.render import render
from test_the_builder_asks_before_it_guesses import _call, _check, _run, desk  # noqa: F401

FORM_FIRST = [{"id": "submit", "type": "human", "name": "Enter the invoice", "does": "You enter the invoice",
               "prompt": "The invoice's supplier, number and amount", "next": "check"}]
CHECK = {"id": "check", "type": "llm", "name": "Check against the terms", "does": "Checks it against our terms",
         "prompt": "Check {{state.invoice}} against our payment terms.", "next": "end"}
WRITE = {"id": "write", "type": "llm", "name": "Write the reply", "does": "Writes a reply",
         "prompt": "Write a reply to {{state.message}}.", "next": "end"}


def plan(steps: list[dict], summary: str, **more) -> dict:
    return {"finish": "submit_plan", "kind": "create", "title": "A build", "summary": summary, "steps": steps, **more}


def card(draft: dict) -> str:
    return render(O.Plan.model_validate({k: v for k, v in draft.items() if k != "finish"}))


class TestHowItStarts:
    def test_a_form_first_build_starts_when_someone_fills_it_in(self):
        text = card(plan([*FORM_FIRST, CHECK], "Automatically checks new supplier invoices."))
        start = text.split("## How it starts\n", 1)[1].split("\n## ", 1)[0]
        assert "Enter the invoice" in start and "fill" in start and "automatic" not in start.lower()

    def test_a_scheduled_build_starts_on_its_schedule(self):
        text = card(plan([WRITE], "Automatically writes the weekly note.",
                         schedule={"cron": "0 9 * * 1", "says": "every Monday at 9:00"}))
        start = text.split("## How it starts\n", 1)[1].split("\n## ", 1)[0]
        assert "schedule" in start.lower() or "Monday" in start

    def test_any_other_build_starts_when_someone_runs_it(self):
        text = card(plan([WRITE], "Writes a reply."))
        assert "## How it starts\nWhen you run it" in text


class TestNoSendBackForAWord:
    async def test_automatically_on_a_scheduled_build_passes(self, client, desk):
        out = await _call("submit_plan", plan([WRITE], "Automatically writes the weekly note.",
                                              schedule={"cron": "0 9 * * 1", "says": "every Monday at 9:00"}),
                          await _run("Every Monday write a note."))
        assert out.get("finish") == "submit_plan"

    async def test_automatically_about_the_work_not_the_start_passes(self, client, desk):
        out = await _call("submit_plan", plan([{**FORM_FIRST[0], "next": "write"}, WRITE],
                                              "Automatically formats the reply."),
                          await _run("Format my replies."))
        assert out.get("finish") == "submit_plan"


class TestTheirOwnTerms:
    async def test_a_check_against_their_terms_with_nothing_giving_them_is_said_once(self, client, desk):
        run = await _run("Check each invoice against our terms.")
        out = await _call("submit_plan", plan([*FORM_FIRST, CHECK], "Check each invoice against our terms."), run)
        assert "error" in out and "said once" in out["error"]
        assert "Check against the terms" in out["error"] and "assumed" in out["error"]

    async def test_taken_as_it_is_the_card_says_it_assumed_general_terms(self, client, desk):
        run = await _run("Check each invoice against our terms.")
        draft = plan([*FORM_FIRST, CHECK], "Check each invoice against our terms.")
        await _call("submit_plan", draft, run)
        again = await _call("submit_plan", draft, run)
        assert again.get("finish") == "submit_plan"
        out = await _check(run, again)
        assert out["outcome"] == "review" and "Can't be saved yet" not in out["review"]
        assumed = out["review"].split("## What I assumed", 1)
        assert len(assumed) == 2 and "terms" in assumed[1].split("\n## ", 1)[0]

    async def test_terms_from_a_knowledge_base_pass(self, client, desk):
        out = await _call("submit_plan", plan([*FORM_FIRST, CHECK], "Check each invoice against our terms.",
                                              knowledge=[{"name": "Payment terms", "purpose": "Our supplier payment terms",
                                                          "files": ["terms/payment-terms.md"]}]),
                          await _run("Check each invoice against our terms."))
        assert "Check against the terms" not in (out.get("error") or "")

    async def test_terms_asked_for_in_the_form_pass(self, client, desk):
        form = [{**FORM_FIRST[0], "prompt": "The invoice, and your payment terms for this supplier"}]
        out = await _call("submit_plan", plan([*form, CHECK], "Check each invoice against our terms."),
                          await _run("Check each invoice against our terms."))
        assert "Check against the terms" not in (out.get("error") or "")

    async def test_terms_the_plan_says_it_assumed_pass(self, client, desk):
        out = await _call("submit_plan", plan([*FORM_FIRST, CHECK], "Check each invoice against general terms.",
                                              assumed=["Checked against general terms: add your terms document."]),
                          await _run("Check each invoice against our terms."))
        assert out.get("finish") == "submit_plan"
