"""A plan whose send after an approval is left to a model goes back once, and set from what the card showed it goes on
(F1743).

Through the builder's own `submit_plan`, with a `send_email` tool here that takes who it goes to: "draft each reply,
show it to me, then send it" planned with the send's inputs left to a model is sent back, naming what the approval
showed; the same plan with `config.tool_arguments` set from those values is taken, and the line is not said again.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState

EMAIL = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"}, "subject": {"type": "string"},
    "text": {"type": "string"}}}
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
#: What the send-back says, as the planner reads it.
SAID = ('step send: it sends after "Check the reply" is approved, but a model would write its inputs, and it never '
        "sees what was approved. Set every input in its `config.tool_arguments` from what that step showed: "
        "{{state.claim_and_draft.recipient_email}}, {{state.claim_and_draft.subject}}, "
        '{{state.claim_and_draft.email_body}}, e.g. "to": "{{state.claim_and_draft.recipient_email}}".')


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def desk(client):
    from core.store.resources import save_tool

    builder_access.forget_runs()
    await save_tool({"name": "send_email", "description": "Sends an email through our mail service.",
                     "tool_type": "http", "method": "POST", "url": "https://relay.harborpine.example/send",
                     "headers": {}, "inputSchema": EMAIL})
    yield
    builder_access.forget_runs()


async def _run() -> OrchestrationRun:
    run = OrchestrationRun(run_id="builder_f1743", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": "Draft each reply, show it to me, then send it."})
    await SharedState(run).checkpoint()
    return run


async def _call(run, tool: str, args: dict):
    from core.orchestration.engine import live

    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _plan(*steps) -> dict:
    return {"kind": "create", "title": "Sales replies", "summary": "Drafts each reply, shows it, then sends it.",
            "steps": [dict(s) for s in steps], "agents": []}


async def test_sent_back_naming_what_the_card_showed_then_set_it_goes_on(client):
    run = await _run()
    first = await _call(run, "submit_plan", _plan(DRAFT, CHECK, GATE, SEND))
    assert SAID in (first.get("error") or ""), first
    then = await _call(run, "submit_plan", _plan(DRAFT, CHECK, GATE, SET))
    assert "it sends after" not in json.dumps(then), then
    assert not then.get("error"), then
