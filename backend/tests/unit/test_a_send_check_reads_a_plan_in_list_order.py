"""The send checks read a plan written in list order as they read one that names every next (F1756).

A model often lists a plan's steps in order and leaves `next` out; a list is an order, and the save settles each
unsaid `next` as the step after it (`objects.settled`). The checks on a send after an approval (`approved_sends`: a
model-filled send, F1743; a field a send reads that starts empty, F1753; a value a send sends that its approval never
shows, F1754) walked only a `next` or a branch the plan named, so a plan in list order had no send after its approval
and none of them asked: a stronger planner, which writes such plans, was never checked at all.

Near misses: a branch whose arms end does not fall through into the step listed after it; an End step stops the walk;
a plan that names every next reads as before.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan

EMAIL = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"}, "subject": {"type": "string"},
    "text": {"type": "string"}}}
F1743, F1753, F1754 = "it sends after", "starts empty", "never shows"

DRAFT = {"id": "draft", "type": "llm", "name": "Draft the reply", "does": "Drafts the reply",
         "prompt": "Draft a reply to {{ state.user_input }}.",
         "config": {"output_fields": [{"name": "to", "type": "text"}, {"name": "subject", "type": "text"},
                                      {"name": "body", "type": "text"}]}}
APPROVED = {"name": "approved", "type": "boolean", "label": "Send it?"}
ASKS = {"id": "check", "type": "human", "name": "Check the reply", "does": "Asks for approval",
        "prompt": "Here is the reply drafted. Approve or decline.", "config": {"human_fields": [APPROVED]}}
TYPED = {**ASKS, "prompt": "To: {{state.draft.to}}\nSubject: {{state.draft.subject}}\n\n{{state.draft.body}}",
         "config": {"human_fields": [APPROVED, {"name": "cc", "type": "email", "label": "Copy to"}]}}
GATE = {"id": "gate", "type": "if_else", "name": "Approved?", "does": "Goes on when approved",
        "condition": "state.check.approved", "branches": {"true": "send", "false": "end"}}
MODEL_SEND = {"id": "send", "type": "tool", "name": "Send the reply", "does": "Sends it", "tool": "send_email"}
SET_SEND = {**MODEL_SEND, "config": {"tool_arguments": {"to": "{{state.draft.to}}", "subject": "{{state.draft.subject}}",
                                                        "text": "{{state.draft.body}}"}}}
CC_SEND = {**MODEL_SEND, "config": {"tool_arguments": {"to": "{{state.check.cc}}", "subject": "{{state.draft.subject}}",
                                                       "text": "{{state.draft.body}}"}}}


@pytest.fixture(autouse=True)
async def tools_here():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([{"name": "send_email", "description": "Sends an email", "tool_type": "http",
                                 "method": "POST", "url": "https://api.harborpine.example/send_email",
                                 "inputSchema": EMAIL}])


def _ws() -> build_plan.Workspace:
    return build_plan.Workspace(tools=[{"name": "send_email", "does": "send_email"}], takes={"send_email": EMAIL})


def _plan(*steps) -> dict:
    return {"kind": "create", "title": "Replies", "summary": "Drafts each reply, asks, then sends it.",
            "steps": [dict(s) for s in steps], "agents": []}


async def _said(plan: dict, mark: str) -> list[str]:
    found = await build_plan.examine(plan, None, ws=_ws(), state={"user_message": "Draft it, ask me, then send it."})
    return [e for e in found.errors if mark in e]


class TestAPlanInListOrder:
    """Every step's `next` left out, as a model often writes it."""

    @pytest.mark.parametrize("approval, send, mark", [
        pytest.param(ASKS, MODEL_SEND, F1743, id="a model-filled send after an approval (F1743)"),
        pytest.param(TYPED, CC_SEND, F1753, id="a field a send reads that starts empty (F1753)"),
        pytest.param(ASKS, SET_SEND, F1754, id="a value a send sends that its approval never shows (F1754)"),
    ])
    async def test_an_approval_then_a_send_is_found(self, approval, send, mark):
        assert await _said(_plan(DRAFT, approval, send), mark)

    @pytest.mark.parametrize("approval, send, mark", [
        pytest.param(ASKS, MODEL_SEND, F1743, id="F1743"),
        pytest.param(TYPED, CC_SEND, F1753, id="F1753"),
        pytest.param(ASKS, SET_SEND, F1754, id="F1754"),
    ])
    async def test_and_through_a_decision_whose_arm_it_is(self, approval, send, mark):
        assert await _said(_plan(DRAFT, approval, GATE, send), mark)


class TestNearMisses:
    @pytest.mark.parametrize("mark", [F1743, F1754])
    async def test_a_branch_whose_arms_end_does_not_fall_through(self, mark):
        ends = {**GATE, "branches": {"true": "end", "false": "end"}}
        send = MODEL_SEND if mark == F1743 else SET_SEND
        assert await _said(_plan(DRAFT, ASKS, ends, send), mark) == []

    @pytest.mark.parametrize("mark", [F1743, F1754])
    async def test_an_end_step_stops_the_walk(self, mark):
        end = {"id": "stop", "type": "end", "name": "Stop", "does": "Ends the run"}
        send = MODEL_SEND if mark == F1743 else SET_SEND
        assert await _said(_plan(DRAFT, ASKS, end, send), mark) == []

    @pytest.mark.parametrize("approval, send, mark", [
        pytest.param(ASKS, MODEL_SEND, F1743, id="F1743"),
        pytest.param(TYPED, CC_SEND, F1753, id="F1753"),
        pytest.param(ASKS, SET_SEND, F1754, id="F1754"),
    ])
    async def test_a_plan_that_names_every_next_reads_as_before(self, approval, send, mark):
        named = _plan({**DRAFT, "next": "check"}, {**approval, "next": "gate"}, GATE, {**send, "next": "end"})
        assert len(await _said(named, mark)) == 1
