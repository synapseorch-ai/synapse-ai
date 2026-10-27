"""
A build's research outcome says what each missing tool is for (SAI2-P3c, D300; for SAI2-P5's `look_up_providers`,
agreed 10-24, changed 10-27: P3B-HANDOFF.md).

check_build's research outcome held tool names only, so the lookup had nothing to look up by but a name. Now it also
carries `needs: [{tool, does, provider?}]`, which the route's own lookup step reads as
`{{state.plan_checked.needs}}`:
- `does` is the step's own sentence (or, for a tool only an agent holds, the agent's purpose);
- `provider` is set only from a service the person named (the request's `providers`, the front door's classify), and
  only on the need whose own words name it: the lookup gives a final only to a named provider, so code never guesses
  one, and a provider named for one tool is never given to another.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)


SEND = {"id": "send", "type": "tool", "name": "Send the campaign", "does": "Sends this week's campaign to the list",
        "tool": "send_mailchimp_campaign"}
LOG = {"id": "log", "type": "tool", "name": "Log the send", "does": "Adds a row for this send to the tracker",
       "tool": "append_airtable_row"}


def _draft(*steps, agents=()):
    return {"finish": "submit_plan", "kind": "create", "title": "Weekly campaign", "summary": "Sends it weekly.",
            "steps": list(steps), "agents": list(agents)}


async def _check(draft, *, request=None):
    state = {builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
             "user_message": "Send my weekly campaign and log it"}
    if request is not None:
        state["request"] = request
    run = OrchestrationRun(run_id="builder_needs", orchestration_id="orch_native_builder", shared_state=state)
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = draft
    with live(run):
        raw, _ = await execute_builder_tool_with_events("check_build", {"draft": draft}, _Server(), run_id=run.run_id)
    return run, (json.loads(raw) if isinstance(raw, str) else raw)


async def test_the_research_outcome_says_what_each_missing_tool_does():
    _, checked = await _check(_draft(SEND, LOG))
    assert checked["outcome"] == "research", checked
    assert checked["research"] == ["send_mailchimp_campaign", "append_airtable_row"]
    assert checked["needs"] == [
        {"tool": "send_mailchimp_campaign", "does": "Sends this week's campaign to the list"},
        {"tool": "append_airtable_row", "does": "Adds a row for this send to the tracker"}]


async def test_a_provider_the_person_named_is_set_only_on_the_need_that_names_it():
    _, checked = await _check(_draft(SEND, LOG), request={"kind": "build", "providers": ["Mailchimp"]})
    by_tool = {n["tool"]: n for n in checked["needs"]}
    assert by_tool["send_mailchimp_campaign"]["provider"] == "Mailchimp"
    assert "provider" not in by_tool["append_airtable_row"]


async def test_a_provider_written_in_the_steps_words_is_set_from_the_persons_own_spelling():
    step = {**LOG, "tool": "append_row", "does": "Adds a row for this send to the Airtable base"}
    _, checked = await _check(_draft(SEND, step), request={"kind": "build", "providers": ["Airtable", "Mailchimp"]})
    by_tool = {n["tool"]: n for n in checked["needs"]}
    assert by_tool["append_row"]["provider"] == "Airtable"
    assert by_tool["send_mailchimp_campaign"]["provider"] == "Mailchimp"


async def test_no_provider_is_guessed_when_the_person_named_none():
    _, checked = await _check(_draft(SEND, LOG), request={"kind": "build", "providers": []})
    assert all("provider" not in n for n in checked["needs"])


async def test_a_tool_only_an_agent_holds_is_said_by_the_agents_purpose():
    agent = {"name": "Campaign sender", "purpose": "Sends the weekly campaign", "tools": ["send_mailchimp_campaign"]}
    step = {"id": "go", "type": "agent", "name": "Send it", "does": "Sends it", "agent": "Campaign sender",
            "prompt": "Send this week's campaign."}
    _, checked = await _check(_draft(step, agents=[agent]))
    assert checked["outcome"] == "research", checked
    assert checked["needs"] == [{"tool": "send_mailchimp_campaign", "does": "Sends the weekly campaign"}]


async def test_the_lookup_steps_reference_reads_the_needs_as_a_list():
    from core.orchestration.steps import render_tool_arguments

    run, checked = await _check(_draft(SEND, LOG), request={"kind": "build", "providers": ["Mailchimp"]})
    state = {**run.shared_state, build_plan.CHECKED_KEY: checked}
    args = render_tool_arguments({"by_route": True, "needs": "{{state.plan_checked.needs}}"}, state)
    assert args["needs"] == checked["needs"] and isinstance(args["needs"], list)
