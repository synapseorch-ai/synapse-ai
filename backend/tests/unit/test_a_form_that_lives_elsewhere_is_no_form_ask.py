"""A form that lives somewhere else is not an ask for a form this workflow shows (F1737).

F1616's check reads a request that says "form" as an ask for one: a new workflow with no schedule, planned without
a `human` form step first, is told once to start from one, then the card says it. But a request often names a form
that is somewhere else: a website's contact form whose enquiries arrive, a Google Form's responses, tax forms someone
uploads, a form letter. Told to start from a form there, a planner that complies puts a person's form in front of a
workflow that items arrive at, and every run waits for someone to fill it in; one that doesn't still leaves a false
line on the card.

So the word still counts, as before, except where the words around it say the form is elsewhere: a source before it
(contact, website, Google, "our X form", "their form"), an arriving item after it (enquiry, submissions, responses,
entries, data), the form as the one acting ("the form sends…"), a document (tax or PDF forms, forms someone uploads),
and a form letter. A plan that has said it cannot start the way they asked, and why (F1645), is not told to start
from a form either. Everything else that asked for a form still does.
"""
from __future__ import annotations

import pytest

from tests.unit.test_a_form_ask_starts_from_a_form import APPROVE, READ, _examined, _plan


def _form_said(found) -> list[str]:
    return [s for s in found.structure if "asked for a form" in s]


# ── a form somewhere else (F1633's near misses) ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ask", [
    # a source before it
    "It gets what arrives from the website: a contact form enquiry, a shop order, or a support ticket.",
    "When someone submits our Google Form, email me.",
    "Our website form posts leads to this workflow.",
    "Collect what comes in through their form and file it.",
    "Add each lead from the website's form to the sheet.",
    # an arriving item after it
    "Sort the form submissions by date and summarise each one.",
    "Read each form response and add it to the sheet.",
    # the form as the one acting
    "the contact form sends us an email; triage those emails",
    "When the form emails us a lead, add it to the sheet.",
    # a document
    "Read the tax forms I upload and pull out the totals.",
    "Check the forms we attach for a missing signature.",
    # a form letter
    "Send the new hire a form letter.",
], ids=["contact-enquiry", "google-form", "website-form", "their-form", "websites-form", "submissions", "response",
        "contact-form-sends", "form-emails", "tax-forms", "forms-we-attach", "form-letter"])
async def test_a_form_that_lives_elsewhere_is_no_ask_for_one(ask):
    found = await _examined(READ, APPROVE, ask=ask)
    assert not _form_said(found), found.structure


# ── an ask for a form still is one ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ask", [
    "Make an intake form for new hires and send their answers to HR.",
    "Make a form where customers enter their order, then confirm it by email.",
    "Show a form asking for the amount, then ask me to approve it.",
    "Run it whenever a customer fills in a form on our site.",
    # a source word near the form, but not right before it
    "Give our new hires a form to fill in on day one.",
    "Our team wants a form where people log expenses.",
    "On our contact page, show a form asking for their order number.",
])
async def test_an_ask_for_a_form_this_workflow_shows_is_still_one(ask):
    found = await _examined(READ, APPROVE, ask=ask)
    assert len(_form_said(found)) == 1, found.structure


async def test_one_form_asked_for_beside_one_elsewhere_is_still_an_ask():
    ask = "Show a form asking for the amount; our Google Form responses come in separately."
    found = await _examined(READ, APPROVE, ask=ask)
    assert len(_form_said(found)) == 1, found.structure


# ── a plan that said it cannot start that way ────────────────────────────────────────────────────────────────────
async def test_a_plan_that_said_why_it_cannot_start_that_way_is_not_told_to_start_from_a_form():
    from core.native_builder import build_plan

    ask = "Run it whenever a customer fills in a form on our site."
    cannot = {**_plan(READ, APPROVE), "asked_start": {
        "said": "whenever a customer fills in a form on our site", "made": "cannot",
        "why_not": "nothing here starts a workflow when a website form is sent; the site calls it with what was sent"}}
    found = await build_plan.examine(cannot, None, ws=build_plan.Workspace(), state={"user_message": ask})
    assert not _form_said(found), found.structure
    unsaid = {**cannot, "asked_start": {**cannot["asked_start"], "why_not": " "}}
    found = await build_plan.examine(unsaid, None, ws=build_plan.Workspace(), state={"user_message": ask})
    assert any("say why in why_not" in e for e in found.errors), "a cannot with no why goes back to say why first"
