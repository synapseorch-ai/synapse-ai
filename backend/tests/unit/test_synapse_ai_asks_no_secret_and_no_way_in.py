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
    "What's your Stripe key?",
    "Could you share the key for your OpenAI account?",
    "What is your login for the patient portal?",
    "What's the PIN for the account?",
    "What 2FA code did you get?",
    "What's the Stripe secret?",
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
    ("How many tokens may one reply use at most?", "how_much"),
    ("What should happen when a password reset is requested?", "what"),
    ("Which LinkedIn connections should get the update?", "which"),
    ("How many license keys does each new hire need?", "how_much"),
    ("Which HTTP tool do you mean, the old one or the new one?", "which"),
    ("Should the summary include the CRM notes it integrates with?", "what"),
    ("Which pin should the map use for the clinic?", "which"),
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


async def test_an_option_asking_for_a_secret_is_sent_back_too(client):
    out = await _ask({"question": "How should it sign in?", "about": "what",
                      "options": ["With the shared login", "Paste your API key here"]})
    assert "error" in out


# ── every word a model says to the person (F1093) ─────────────────────────────────────────────────────────────────
# H45's res-05 (P5's measure, 10-27): `ask_person` refused, so the ask came out in the plan's open questions instead
# ("you'll need to provide your Stripe Secret API Key…"). The class is every place a model's words reach the person:
# the plan's own words (`test_the_build_route_decides_in_code`), a `cannot`, a change's `says`, and the reply a turn
# ends with. One detector says all of them (`core.secret_scrub`, `test_secret_words`).

from core.secret_scrub import NEVER_IN_CHAT  # noqa: E402

RES_05 = ("To complete this workflow, you'll need to provide your Stripe Secret API Key (starts with sk_test_ or "
          "sk_live_) and your SendGrid API Key (starts with SG.).")


async def _call(tool, args):
    run = OrchestrationRun(run_id="builder_said", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_a_cannot_that_names_a_key_is_sent_back(client):
    out = await _call("cannot", {"why": "I can't send Stripe payment links without your Stripe Secret API Key.",
                                 "nearest": "Paste your API key and I'll build it."})
    assert "error" in out and "Connect card" in out["error"]
    plain = await _call("cannot", {"why": "There are no printers here.", "nearest": "Email a PDF instead."})
    assert plain == {"finish": "cannot", "why": "There are no printers here.", "nearest": "Email a PDF instead."}


async def test_a_change_whose_words_ask_for_a_key_is_sent_back(client):
    op = {"op": "rename_workflow", "target": {"kind": "workflow", "name": "Digest"}, "fields": {"name": "Daily"},
          "says": f"Rename it. {RES_05}"}
    out = await _call("submit_change", {"ops": [op]})
    assert "error" in out and "op 1" in out["error"] and "Connect card" in out["error"]
    out = await _call("submit_change", {"ops": [{**op, "says": "Rename the digest to Daily."}]})
    assert out.get("finish") == "submit_change"


@pytest.mark.parametrize("orchestration_id, status, kept", [
    ("orch_native_builder", "completed", False),
    ("orch_someone_elses", "completed", True),
    ("orch_native_builder", "failed", True),
])
async def test_a_turns_reply_never_asks_for_a_key(client, orchestration_id, status, kept):
    """The talk and answer routes' replies are a model's own words, with no finish tool to refuse them: where every
    run ends, a sentence that asks is taken out and code's line is said in its place, in the stored state too."""
    from core.native_builder.change_tools import said_without_secret_asks

    said = f"Here's how it works. {RES_05} Then it runs on its own."
    run = OrchestrationRun(run_id="builder_reply", orchestration_id=orchestration_id, status=status,
                           shared_state={"summary": said})
    await SharedState(run).checkpoint()
    await said_without_secret_asks(run)
    stored = (await SharedState.restore(run.run_id)).run.shared_state["summary"]
    if kept:
        assert run.shared_state["summary"] == said and stored == said
        return
    for text in (run.shared_state["summary"], stored):
        assert "Secret API Key" not in text and text.endswith(NEVER_IN_CHAT)
        assert text.startswith("Here's how it works.") and "Then it runs on its own." in text


@pytest.mark.parametrize("orchestration_id, session_id, kept", [
    ("orch_0192f3a1c4d07b2e9a6f5d8c3b1e0a47", "builder:u1:default", False),
    ("orch_someone_elses", "builder:u1:default", True),
])
async def test_a_hosts_builder_reply_never_asks_for_a_key(client, orchestration_id, session_id, kept):
    """F1126: cloud's builder is a per-workspace copy under an id it mints, so the net keyed on `orch_native_builder`
    never ran there. A host's own builder id is the builder's; an ordinary run named `builder:…` is not."""
    from core.native_builder import identity
    from core.native_builder.change_tools import said_without_secret_asks

    async def ids():
        return ["orch_0192f3a1c4d07b2e9a6f5d8c3b1e0a47"]

    identity.set_builder_ids(ids)
    try:
        said = f"Here's how it works. {RES_05} Then it runs on its own."
        run = OrchestrationRun(run_id="builder_reply", orchestration_id=orchestration_id, session_id=session_id,
                               status="completed", shared_state={"summary": said})
        await SharedState(run).checkpoint()
        await said_without_secret_asks(run)
    finally:
        identity.set_builder_ids(None)
    stored = (await SharedState.restore(run.run_id)).run.shared_state["summary"]
    if kept:
        assert run.shared_state["summary"] == said and stored == said
        return
    for text in (run.shared_state["summary"], stored):
        assert "Secret API Key" not in text and text.endswith(NEVER_IN_CHAT)


def test_the_reply_check_runs_where_every_run_ends():
    import core.builder_tools  # noqa: F401 — loads the builder's modules, as at boot
    from core.native_builder.change_tools import said_without_secret_asks
    from core.orchestration.cancel import _ON_END

    assert said_without_secret_asks in _ON_END
