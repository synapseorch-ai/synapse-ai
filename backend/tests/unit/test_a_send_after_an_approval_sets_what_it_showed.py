"""A send after an approval sets its inputs from what the approval showed, never left to a model (F1743).

Planned "show me the reply, then send it", a Tool step with no `config.tool_arguments` has a model write the call at
run time from its own prompt, which does not hold what the Human step before it showed: who it goes to can be other
than who was approved. The plan check finds a Human step, then (through decisions only) a Tool step a model fills
whose tool takes an address and does more than read, and sends the plan back once a run, naming the
`{{state.<step id>.<field>}}` the approval showed and no mapping of its own; kept, the card says a model writes who it
goes to and that the run stops on an address it doesn't hold.

Near misses read as before: the send's arguments set, a Tool step before any approval, an agent step between the
approval and the send, a tool with no address input or none known here, and a tool that only reads (a look-up by
email sends to no one), while a tool that writes one (adding a contact) is held.
"""
from __future__ import annotations

import pytest

from core.native_builder import approved_sends, build_plan
from core.native_builder import objects as O

MARK = "it sends after"
EMAIL = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"}, "subject": {"type": "string"},
    "text": {"type": "string"}}}
LOOKUP = {"type": "object", "required": ["email"], "properties": {"email": {"type": "string"}}}
VAULT = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}

DRAFT = {"id": "claim_and_draft", "type": "llm", "name": "Draft the reply", "does": "Drafts the reply",
         "prompt": "Draft a reply to {{ state.user_input }}.", "next": "check",
         "config": {"output_fields": [{"name": "recipient_email", "type": "text"},
                                      {"name": "subject", "type": "text"}, {"name": "email_body", "type": "text"}]}}
CHECK = {"id": "check", "type": "human", "name": "Check the reply", "does": "Shows the reply for approval",
         "prompt": "To: {{state.claim_and_draft.recipient_email}}\nSubject: {{state.claim_and_draft.subject}}\n\n"
                   "{{state.claim_and_draft.email_body}}",
         "config": {"human_fields": [{"name": "approved", "type": "boolean", "label": "Send this reply?"}]},
         "next": "gate"}
GATE = {"id": "gate", "type": "if_else", "name": "Approved?", "does": "Goes on when approved",
        "condition": "state.check.approved", "branches": {"true": "send", "false": "end"}}
SEND = {"id": "send", "type": "tool", "name": "Send email", "does": "Sends the reply", "tool": "send_email",
        "next": "end"}
SET = {**SEND, "config": {"tool_arguments": {"to": "{{state.claim_and_draft.recipient_email}}",
                                             "subject": "{{state.claim_and_draft.subject}}",
                                             "text": "{{state.claim_and_draft.email_body}}"}}}
LINE = ('step send: it sends after "Check the reply" is approved, but a model would write its inputs, and it never '
        "sees what was approved. Set every input in its `config.tool_arguments` from what that step showed: "
        "{{state.claim_and_draft.recipient_email}}, {{state.claim_and_draft.subject}}, "
        '{{state.claim_and_draft.email_body}}, e.g. "to": "{{state.claim_and_draft.recipient_email}}".')
CARD = ('"Send email" sends after "Check the reply", and a model writes who it goes to: the run stops and asks if '
        "that's an address the run doesn't hold.")


@pytest.fixture(autouse=True)
async def tools_here():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([
        {"name": n, "description": d, "tool_type": "http", "method": m, "url": f"https://api.harborpine.example/{n}",
         "inputSchema": s}
        for n, d, m, s in (("send_email", "Sends an email", "POST", EMAIL),
                           ("look_up_customer", "Finds a customer by email", "GET", LOOKUP),
                           ("add_contact", "Adds a contact", "POST", LOOKUP))])


def _ws() -> build_plan.Workspace:
    names = ("send_email", "look_up_customer", "add_contact", "vault_write")
    return build_plan.Workspace(tools=[{"name": n, "does": n} for n in names],
                                takes={"send_email": EMAIL, "look_up_customer": LOOKUP, "add_contact": LOOKUP,
                                       "vault_write": VAULT})


def _plan(*steps, **more) -> dict:
    return {"kind": "create", "title": "Sales replies", "summary": "Drafts each reply, shows it, then sends it.",
            "steps": [dict(s) for s in steps], "agents": [], **more}


async def _said(plan: dict, state: dict | None = None):
    state = {"user_message": "Draft each reply, show it to me, then send it."} if state is None else state
    found = await build_plan.examine(plan, None, ws=_ws(), state=state)
    return [e for e in found.errors if MARK in e], found


class TestSentBackOnce:
    async def test_an_approval_a_decision_then_a_send_names_what_the_card_showed(self):
        said, _ = await _said(_plan(DRAFT, CHECK, GATE, SEND))
        assert said == [LINE]

    async def test_an_approval_straight_into_the_send(self):
        said, _ = await _said(_plan(DRAFT, {**CHECK, "next": "send"}, SEND))
        assert said == [LINE]

    async def test_kept_as_it_is_it_goes_on_and_the_card_says_it(self):
        state = {"user_message": "Draft each reply, show it to me, then send it."}
        first, _ = await _said(_plan(DRAFT, CHECK, GATE, SEND), state)
        again, found = await _said(_plan(DRAFT, CHECK, GATE, SEND), state)
        assert first == [LINE] and again == [], "sent back once a run, never a block"
        assert CARD in found.notes

    async def test_a_card_that_showed_nothing_points_to_what_earlier_steps_wrote(self):
        said, _ = await _said(_plan(DRAFT, {**CHECK, "prompt": "Send the reply?", "next": "send"}, SEND))
        assert said and "from what earlier steps wrote, as {{state.<step id>.<field>}}" in said[0]
        assert "e.g." not in said[0], "no input is paired with a value the card never showed"

    async def test_a_tool_that_writes_an_email_somewhere_is_held(self):
        add = {**SEND, "tool": "add_contact", "name": "Add the contact"}
        said, _ = await _said(_plan(DRAFT, {**CHECK, "next": "send"}, add))
        assert said and '"email": "{{state.claim_and_draft.recipient_email}}"' in said[0]


class TestNearMisses:
    @pytest.mark.parametrize("steps", [
        pytest.param((DRAFT, CHECK, GATE, SET), id="its arguments set"),
        pytest.param(({**SEND, "next": "claim_and_draft"}, DRAFT, {**CHECK, "next": "end"}), id="before the approval"),
        pytest.param((DRAFT, {**CHECK, "next": "redraft"},
                      {"id": "redraft", "type": "llm", "name": "Redraft", "does": "Redrafts", "prompt": "Redraft.",
                       "next": "send"}, SEND), id="a model step between"),
        pytest.param((DRAFT, {**CHECK, "next": "send"}, {**SEND, "tool": "look_up_customer"}), id="a look-up"),
        pytest.param((DRAFT, {**CHECK, "next": "send"}, {**SEND, "tool": "vault_write"}), id="no address input"),
        pytest.param((DRAFT, {**CHECK, "next": "send"}, {**SEND, "tool": "post_somewhere"}), id="schema unknown"),
    ])
    async def test_says_nothing(self, steps):
        said, _ = await _said(_plan(*steps))
        assert said == []


class TestWhereItLooks:
    async def test_an_inner_workflows_approval_and_send(self):
        plan = O.Plan.model_validate(_plan(DRAFT, {**CHECK, "next": "end"}, inner_workflows=[
            {"title": "Reply", "does": "Drafts, shows and sends one reply",
             "steps": [dict(DRAFT), dict(CHECK), dict(GATE), dict(SEND)]}]))
        found = await approved_sends.found_in(plan, _ws(), None)
        assert [(f.approval.id, f.send.id) for f in found] == [("check", "send")]

    async def test_a_tool_the_build_makes_is_read_by_what_it_will_take(self):
        plan = O.Plan.model_validate(_plan(DRAFT, {**CHECK, "next": "send"}, {**SEND, "tool": "send_note"}))
        plan.tools = [O.ToolStatus(name="send_note", status="found", via="http_tool",
                                   api={"tool": {"inputSchema": EMAIL}, "endpoint": {"method": "POST"}})]
        found = await approved_sends.found_in(plan, _ws(), None)
        assert [f.address for f in found] == ["to"]

    @pytest.mark.parametrize("name, prop, takes", [
        ("to", {}, True), ("customer_email", {}, True), ("mobile", {}, True),
        ("target", {"description": "The recipient's email address"}, True),
        ("path", {}, False), ("url", {}, False), ("email_body", {}, False)])
    def test_an_address_input_by_its_name_or_what_it_says(self, name, prop, takes):
        assert (approved_sends.address_input({"properties": {name: prop}}) == name) is takes
