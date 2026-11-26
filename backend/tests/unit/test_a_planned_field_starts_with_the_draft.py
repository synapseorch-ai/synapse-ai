"""The builder plans a field a person edits before it goes with the draft as its starting value (F1753).

Asked "show me the welcome to approve, then send it", the planner wrote the draft into the Human step's words and
To, Subject and Body fields under them ("edit the fields below if needed"), which the send read; the fields started
empty, so approved as shown the send got blanks. A field can now start with a value (`default`), and the planner is
told to use it: a field that carries a draft takes it as its starting value, and the step's words do not repeat the
same text above it; a field the person must type has none.

Two checks hold the plan to it. A starting value written out that its field cannot take (not one of its options, not a
number, not an email) is sent back, every time. A field a later send sets as an input that starts empty is sent back
once ("give it a starting value from the step that wrote it, or leave it empty if the person types it"); kept so, the
card says the person fills it in, never a block.

Near misses: a field with a starting value, a field only a Print reads, the approval's own Yes / No, a send whose
inputs read the draft itself, and a starting value written as a reference (checked when the run asks).
"""
from __future__ import annotations

import json

import pytest

from core.native_builder import build_plan
from core.native_builder import objects as O

EMAIL = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"}, "subject": {"type": "string"},
    "text": {"type": "string"}}}
MARK = "starts empty"

DRAFT = {"id": "draft", "type": "llm", "name": "Draft the welcome", "does": "Drafts the welcome",
         "prompt": "Draft a welcome for {{ state.user_input }}.", "next": "review",
         "config": {"output_fields": [{"name": "to", "type": "text"}, {"name": "subject", "type": "text"},
                                      {"name": "body", "type": "text"}]}}
APPROVED = {"name": "approved", "type": "boolean", "label": "Send this email?"}
REVIEW = {"id": "review", "type": "human", "name": "Review the welcome", "does": "Shows the welcome to approve",
          "prompt": "Review the welcome before it is sent.",
          "config": {"human_fields": [APPROVED,
                                      {"name": "to", "type": "email", "label": "To",
                                       "default": "{{state.draft.to}}"},
                                      {"name": "subject", "type": "string", "label": "Subject",
                                       "default": "{{state.draft.subject}}"},
                                      {"name": "body", "type": "textarea", "label": "Body",
                                       "default": "{{state.draft.body}}"}]},
          "next": "gate"}
GATE = {"id": "gate", "type": "if_else", "name": "Approved?", "does": "Goes on when approved",
        "condition": "state.review.approved", "branches": {"true": "send", "false": "end"}}
SEND = {"id": "send", "type": "tool", "name": "Send the welcome", "does": "Sends it", "tool": "send_email",
        "next": "end", "config": {"tool_arguments": {"to": "{{state.review.to}}", "subject": "{{state.review.subject}}",
                                                     "text": "{{state.review.body}}"}}}


def _without_default(review: dict, *names: str) -> dict:
    fields = [{k: v for k, v in f.items() if not (k == "default" and f["name"] in names)}
              for f in review["config"]["human_fields"]]
    return {**review, "config": {**review["config"], "human_fields": fields}}


@pytest.fixture(autouse=True)
async def tools_here():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([{"name": "send_email", "description": "Sends an email", "tool_type": "http",
                                 "method": "POST", "url": "https://api.harborpine.example/send_email",
                                 "inputSchema": EMAIL}])


def _ws() -> build_plan.Workspace:
    return build_plan.Workspace(tools=[{"name": "send_email", "does": "send_email"}], takes={"send_email": EMAIL})


def _plan(*steps) -> dict:
    return {"kind": "create", "title": "Welcomes", "summary": "Drafts each welcome, shows it, then sends it.",
            "steps": [dict(s) for s in steps], "agents": []}


async def _examined(plan: dict, state: dict | None = None):
    state = {"user_message": "Draft each welcome, show it to me to approve, then send it."} if state is None else state
    return await build_plan.examine(plan, None, ws=_ws(), state=state)


class TestThePlannerIsTold:
    def test_the_human_step_rule_says_a_draft_is_a_fields_starting_value(self):
        from core.native_builder import seed

        words = seed.prompt_fragment("plan_steps")
        human = next(line for line in words.splitlines() if line.startswith("- `human`"))
        assert '"default": "{{state.<step id>.<field>}}"' in human
        assert "never repeats" in human and "the person types" in human

    def test_the_field_shape_names_a_starting_value(self):
        from core.native_builder import STEP_TYPE_CHEATSHEET

        shape = next(line for line in STEP_TYPE_CHEATSHEET.splitlines() if line.startswith("- `human_fields_json`"))
        assert "`default`" in shape

    def test_the_save_tool_says_it_too(self):
        from core import builder_tools

        said = json.dumps([t for t in builder_tools.BUILDER_TOOL_SCHEMAS if "human_fields_json" in json.dumps(t)])
        assert "default" in said


class TestAStartingValueItsFieldCannotTake:
    @pytest.mark.parametrize("field, value", [
        ({"name": "when", "type": "choice", "label": "When", "options": ["Now", "Later"]}, "Soon"),
        ({"name": "count", "type": "number", "label": "How many"}, "a few"),
        ({"name": "to", "type": "email", "label": "To"}, "the customer"),
    ])
    async def test_is_sent_back_naming_it_every_time(self, field, value):
        review = {**REVIEW, "config": {"human_fields": [APPROVED, {**field, "default": value}]}}
        state = {"user_message": "Draft each welcome, show it to me to approve, then send it."}
        for _ in range(2):
            found = await _examined(_plan(DRAFT, review, GATE, {**SEND, "config": {"tool_arguments": {
                "to": "{{state.draft.to}}", "subject": "{{state.draft.subject}}", "text": "{{state.draft.body}}"}}}),
                state)
            said = [e for e in found.errors if "cannot take" in e]
            assert said and f"`{field['name']}`" in said[0] and f"“{value}”" in said[0], found.errors

    async def test_one_it_can_take_or_a_reference_is_not(self):
        review = {**REVIEW, "config": {"human_fields": [
            APPROVED, {"name": "when", "type": "choice", "options": ["Now", "Later"], "default": "later"},
            {"name": "to", "type": "email", "default": "{{state.draft.to}}"}]}}
        found = await _examined(_plan(DRAFT, review, GATE, SEND))
        assert not [e for e in found.errors if "cannot take" in e], found.errors


class TestAFieldASendReadsThatStartsEmpty:
    async def test_is_sent_back_once_then_the_card_says_the_person_fills_it_in(self):
        plan = _plan(DRAFT, _without_default(REVIEW, "to"), GATE, SEND)
        state = {"user_message": "Draft each welcome, show it to me to approve, then send it."}
        first = await _examined(plan, state)
        said = [e for e in first.errors if MARK in e]
        assert said == [
            'step review: its field `to` starts empty, and "Send the welcome" sends it. Give it a starting value from '
            'the step that wrote it, as `"default": "{{state.<step id>.<field>}}"`, or leave it empty if the person '
            "types it."], first.errors
        again = await _examined(plan, state)
        assert not [e for e in again.errors if MARK in e], "sent back once a run, never a block"
        assert '"Review the welcome" asks the person to fill in To before "Send the welcome" sends it.' in again.notes

    @pytest.mark.parametrize("steps", [
        pytest.param((DRAFT, REVIEW, GATE, SEND), id="every field starts with the draft"),
        pytest.param((DRAFT, _without_default(REVIEW, "to", "subject", "body"), GATE,
                      {**SEND, "config": {"tool_arguments": {"to": "{{state.draft.to}}",
                                                             "subject": "{{state.draft.subject}}",
                                                             "text": "{{state.draft.body}}"}}}),
                     id="the send reads the draft itself"),
        pytest.param((DRAFT, _without_default(REVIEW, "to"), GATE,
                      {"id": "send", "type": "print", "name": "Show it", "does": "Shows it",
                       "prompt": "{{state.review.to}}", "next": "end"}), id="only a print reads it"),
    ])
    async def test_says_nothing(self, steps):
        found = await _examined(_plan(*steps))
        assert not [e for e in found.errors if MARK in e], found.errors

    async def test_the_approvals_own_yes_no_is_never_a_field_to_fill(self):
        send = {**SEND, "config": {"tool_arguments": {**SEND["config"]["tool_arguments"],
                                                      "subject": "{{state.review.approved}}"}}}
        found = await _examined(_plan(DRAFT, REVIEW, GATE, send))
        assert not [e for e in found.errors if MARK in e and "`approved`" in e], found.errors


class TestAStartingValueReadsWhatAnEarlierStepWrote:
    async def test_a_key_no_step_writes_is_sent_back(self):
        review = {**REVIEW, "config": {"human_fields": [APPROVED, {"name": "to", "type": "email", "label": "To",
                                                                   "default": "{{state.drafted.to}}"}]}}
        found = await _examined(_plan(DRAFT, review, GATE, SEND))
        assert [e for e in found.errors if "drafted" in e], found.errors

