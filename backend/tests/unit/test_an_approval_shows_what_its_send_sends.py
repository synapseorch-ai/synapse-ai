"""An approval before a send shows what the send sends, so the person approves what goes out (F1754).

F1743 asked the plan to set a model-filled send's inputs from what the approval showed. A send whose inputs were set
already passed whatever the approval showed, or nothing: "Here is the email drafted. Please review and approve or
decline", with no value from the drafting step in it, then the draft sent to its address unseen. Now a value a send
after an approval sets from an earlier step, who it goes to and what it says, that the approval does not show (in its
words, as a field's starting value, F1753, or through a value that holds it: `{{state.draft}}` shows
`{{state.draft.body}}`) is sent back once, naming it, saying what would happen and how to show it. The person may have
asked not to see it: then the plan keeps the step as they asked, and the card says it is approved without showing
what goes out. The send-back never says the plan must, and nothing states it as a rule. Never a block.

Near misses read as before: everything shown; an input that is an id; a send after no approval; a model-filled send
(F1743's ask alone, never both); a tool that only reads; a send that reads the approval's own fields (what the person
answered is what they saw).
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan

MARK = "never shows"
EMAIL = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"}, "subject": {"type": "string"},
    "text": {"type": "string"}, "thread_id": {"type": "string", "description": "The thread to reply in"}}}
LOOKUP = {"type": "object", "required": ["email"], "properties": {"email": {"type": "string"}}}

DRAFT = {"id": "draft", "type": "llm", "name": "Draft the reply", "does": "Drafts the reply",
         "prompt": "Draft a reply to {{ state.user_input }}.", "next": "check",
         "config": {"output_fields": [{"name": "to", "type": "text"}, {"name": "subject", "type": "text"},
                                      {"name": "body", "type": "text"}, {"name": "thread", "type": "text"}]}}
APPROVED = {"name": "approved", "type": "boolean", "label": "Send this reply?"}
CHECK = {"id": "check", "type": "human", "name": "Check the reply", "does": "Asks for approval",
         "prompt": "Here is the reply drafted for the enquiry. Please review and approve or decline.",
         "config": {"human_fields": [APPROVED]}, "next": "gate"}
GATE = {"id": "gate", "type": "if_else", "name": "Approved?", "does": "Goes on when approved",
        "condition": "state.check.approved", "branches": {"true": "send", "false": "end"}}
ARGS = {"to": "{{state.draft.to}}", "subject": "{{state.draft.subject}}", "text": "{{state.draft.body}}"}
SEND = {"id": "send", "type": "tool", "name": "Send the reply", "does": "Sends it", "tool": "send_email",
        "next": "end", "config": {"tool_arguments": ARGS}}
LINE = ('step send: it sends {{state.draft.to}}, {{state.draft.subject}}, {{state.draft.body}} after "Check the reply" '
        "is approved, and that step never shows them: the person approves without seeing what goes out. If they want "
        'to see it, show each in its words, e.g. "To: {{state.draft.to}}", or start a field with it as its `default`. If '
        "they asked not to see it, keep the step as they asked: the card says so.")
#: Words a send-back never uses: it says what would happen and how to show it, never that the plan must (F1754).
RULE_WORDS = ("must", "cannot", "can't", "never saw", "not allowed", "required", "has to")
CARD = '"Check the reply" is approved without showing who it goes to, the subject and the text "Send the reply" sends.'


@pytest.fixture(autouse=True)
async def tools_here():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([
        {"name": n, "description": d, "tool_type": "http", "method": m, "url": f"https://api.harborpine.example/{n}",
         "inputSchema": s}
        for n, d, m, s in (("send_email", "Sends an email", "POST", EMAIL),
                           ("look_up_customer", "Finds a customer by email", "GET", LOOKUP))])


def _ws() -> build_plan.Workspace:
    return build_plan.Workspace(tools=[{"name": n, "does": n} for n in ("send_email", "look_up_customer")],
                                takes={"send_email": EMAIL, "look_up_customer": LOOKUP})


def _plan(*steps) -> dict:
    return {"kind": "create", "title": "Replies", "summary": "Drafts each reply, asks, then sends it.",
            "steps": [dict(s) for s in steps], "agents": []}


async def _said(plan: dict, state: dict | None = None):
    state = {"user_message": "Draft each reply, let me approve it, then send it."} if state is None else state
    found = await build_plan.examine(plan, None, ws=_ws(), state=state)
    return [e for e in found.errors if MARK in e], found


def _check(prompt: str, *fields) -> dict:
    return {**CHECK, "prompt": prompt, "config": {"human_fields": [APPROVED, *fields]}}


class TestAnApprovalThatShowsNothing:
    async def test_is_sent_back_naming_what_goes_out(self):
        said, _ = await _said(_plan(DRAFT, CHECK, GATE, SEND))
        assert said == [LINE]

    async def test_kept_as_it_is_it_goes_on_and_the_card_says_it(self):
        state = {"user_message": "Draft each reply, let me approve it, then send it."}
        first, _ = await _said(_plan(DRAFT, CHECK, GATE, SEND), state)
        again, found = await _said(_plan(DRAFT, CHECK, GATE, SEND), state)
        assert first == [LINE] and again == [], "sent back once a run, never a block"
        assert CARD in found.notes

    async def test_one_that_shows_some_names_only_the_rest(self):
        check = _check("To: {{state.draft.to}}\nSubject: {{ state.draft.subject }}\n\nApprove?")
        said, _ = await _said(_plan(DRAFT, check, GATE, SEND))
        assert len(said) == 1 and said[0].startswith("step send: it sends {{state.draft.body}} after"), said
        assert "never shows it:" in said[0]


class TestThePersonsChoice:
    async def test_the_send_back_keeps_their_choice_and_states_no_rule(self):
        said, _ = await _said(_plan(DRAFT, CHECK, GATE, SEND))
        assert "If they asked not to see it, keep the step as they asked" in said[0]
        assert not [w for w in RULE_WORDS if w in said[0].lower()], said[0]

    def test_the_planner_keeps_a_step_they_asked_to_be_shown_nothing(self):
        from core.native_builder import seed

        human = next(line for line in seed.prompt_fragment("plan_steps").splitlines() if line.startswith("- `human`"))
        assert "unless they asked not to see it" in human and "asks as they asked" in human


class TestNearMisses:
    @pytest.mark.parametrize("check", [
        pytest.param(_check("To: {{state.draft.to}}\nSubject: {{state.draft.subject}}\n\n{{state.draft.body}}"),
                     id="shown in its words"),
        pytest.param(_check("Review the reply.", {"name": "to", "type": "email", "default": "{{state.draft.to}}"},
                            {"name": "subject", "type": "string", "default": "{{state.draft.subject}}"},
                            {"name": "body", "type": "textarea", "default": "{{state.draft.body}}"}),
                     id="shown as its fields' starting values"),
        pytest.param(_check("The draft: {{ state.draft }}"), id="shown through the value that holds it"),
    ])
    async def test_everything_shown(self, check):
        said, _ = await _said(_plan(DRAFT, check, GATE, SEND))
        assert said == []

    async def test_an_id_need_not_be_shown(self):
        shown = _check("To: {{state.draft.to}}\nSubject: {{state.draft.subject}}\n\n{{state.draft.body}}")
        send = {**SEND, "config": {"tool_arguments": {**ARGS, "thread_id": "{{state.draft.thread}}"}}}
        said, _ = await _said(_plan(DRAFT, shown, GATE, send))
        assert said == []

    async def test_a_send_after_no_approval(self):
        said, _ = await _said(_plan({**DRAFT, "next": "send"}, SEND))
        assert said == []

    async def test_a_model_filled_send_is_asked_by_f1743_alone(self):
        found = await build_plan.examine(_plan(DRAFT, CHECK, GATE, {**SEND, "config": {}}), None, ws=_ws(),
                                         state={"user_message": "Draft each reply, let me approve it, then send it."})
        assert not [e for e in found.errors if MARK in e]
        assert [e for e in found.errors if "it sends after" in e], "F1743's own ask, as before"

    async def test_a_tool_that_only_reads(self):
        look = {**SEND, "tool": "look_up_customer", "config": {"tool_arguments": {"email": "{{state.draft.to}}"}}}
        said, _ = await _said(_plan(DRAFT, CHECK, GATE, look))
        assert said == []

    async def test_a_send_that_reads_the_approvals_own_fields(self):
        check = _check("Review the reply.", {"name": "to", "type": "email", "label": "To"},
                       {"name": "body", "type": "textarea", "label": "Body"})
        send = {**SEND, "config": {"tool_arguments": {"to": "{{state.check.to}}", "subject": "Your order",
                                                      "text": "{{state.check.body}}"}}}
        said, _ = await _said(_plan(DRAFT, check, GATE, send))
        assert said == []


class TestThePlannerIsTold:
    def test_the_human_step_rule_says_an_approval_shows_what_goes_out(self):
        from core.native_builder import seed

        human = next(line for line in seed.prompt_fragment("plan_steps").splitlines() if line.startswith("- `human`"))
        assert "shows what goes out" in human
