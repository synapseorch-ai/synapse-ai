"""
Synapse AI's question to the person never asks for a secret or for how to connect a service (F937, SAI2-P3, D300).

SAI2-P2's chg-09: the analyst asked for an API key in chat, the person typed it, and the door did not know its shape.
res-05: the analyst asked "How should Stripe be connected: Apps catalog, MCP URL or HTTP tool?", which research finds
out, not the person. Both routes' planners finish with the one `ask_person`, so its handler refuses both kinds of
question and says where each goes instead: a key through a Connect card, a way in through the lookup. The model is
sent back (E2's retry) to ask what only the person knows.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


async def _ask(args):
    run = OrchestrationRun(run_id="builder_ask", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("ask_person", args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.parametrize("question", [
    "What is your Mailchimp API key?",
    "Could you paste the access token for Xero here?",
    "What's the portal password?",
    "Please share the client secret for the Zoom app.",
    "What are your login details for Open Dental?",
])
async def test_a_question_asking_for_a_secret_is_sent_back_to_a_connect_card(client, question):
    out = await _ask({"question": question, "about": "what"})
    assert "error" in out and "Connect card" in out["error"]


@pytest.mark.parametrize("question", [
    "How should Stripe be connected: Apps catalog, MCP URL or HTTP tool?",
    "How do you want me to connect to Mailchimp?",
    "Do you have an MCP server for Calendly?",
    "Should I integrate with Shopify through its API or a webhook?",
])
async def test_a_question_asking_how_to_connect_is_sent_back_to_the_lookup(client, question):
    out = await _ask({"question": question, "about": "what"})
    assert "error" in out and "looked up" in out["error"]


@pytest.mark.parametrize("question, about", [
    ("Which Mailchimp list should the newsletter go to?", "which"),
    ("What time should it run each morning?", "when"),
    ("Who should approve each refund?", "who"),
    ("How much may one run spend at most?", "how_much"),
    ("Which key dates should the reminder cover?", "which"),
    ("Which Slack channel should it post the summary in?", "which"),
])
async def test_a_question_only_the_person_can_answer_is_asked(client, question, about):
    out = await _ask({"question": question, "about": about})
    assert out == {"finish": "ask_person", "question": question, "about": about, "options": []}


async def test_after_five_questions_the_planner_goes_on_with_what_it_has(client):
    run = OrchestrationRun(run_id="builder_ask5", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}},
                           step_history=[{"step_id": "step_human_clarify", "status": "completed"}] * 5)
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("ask_person", {"question": "Which list?", "about": "which"},
                                                        _Server(), run_id=run.run_id)
    out = json.loads(raw) if isinstance(raw, str) else raw
    assert "asked the person 5 questions" in out["error"]


async def test_an_unknown_about_reads_as_what(client):
    out = await _ask({"question": "What should the summary include?", "about": "credentials"})
    assert out["about"] == "what"
