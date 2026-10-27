"""
A plan's step does what each run does, and a model step says what it asks (SAI2-P3, D300; HOT2's read of Naveen's
"Synapse AI Weekly Twitter Post", built on prod rc4.3, the old route, in four review rounds).

That plan held LLM steps "Create Twitter Developer Account" and "Integrate Tool into Orchestration", with no prompt and
no agent. Two classes, sent back to the planner in code:
- an llm step with no prompt says nothing to its model (it has no agent's instructions; an agent step runs on its
  agent's, and an evaluator on its routes, so those may leave the prompt out: 15 of P0's recorded plans do);
- a step whose job is the person's own setup, done once (sign up or create a developer account, get an API key or a
  token, install or integrate a tool, connect an account, enable a setting), is never a step of each run: connecting
  is the plan's Connect card after the yes (F949). A real act of each run, a person's included, stays.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401  (the builder's tools load before build_plan, as at boot)
from core.native_builder import build_plan, objects as O


def _plan(*steps):
    return O.PlanDraft.model_validate({"kind": "create", "title": "Weekly post", "summary": "Posts weekly.",
                                       "steps": list(steps)})


def _errors(*steps):
    errors, _ = O.check_structure(_plan(*steps), O.Inventory())
    return errors


@pytest.mark.parametrize("prompt", [None, "", "  "])
def test_an_llm_step_with_no_prompt_is_sent_back(prompt):
    errors = _errors({"id": "s", "type": "llm", "name": "Draft the tweet", "does": "Drafts it", "prompt": prompt})
    assert any("step s" in e and "says what it asks its model" in e for e in errors), errors


@pytest.mark.parametrize("step_type", ["agent", "evaluator"])
def test_an_agent_or_evaluator_step_may_leave_its_prompt_to_its_agent_or_routes(step_type):
    step = {"id": "s", "type": step_type, "name": "Triage", "does": "Sorts it",
            **({"agent": "Writer"} if step_type == "agent" else {"branches": {"yes": "end", "no": "end"}})}
    plan = O.PlanDraft.model_validate({"kind": "create", "title": "t", "summary": "s", "steps": [step],
                                       "agents": [{"name": "Writer", "purpose": "Writes"}]})
    errors, _ = O.check_structure(plan, O.Inventory())
    assert not [e for e in errors if "says what it asks its model" in e], errors


@pytest.mark.parametrize("name, does", [
    ("Create Twitter Developer Account", "Sets up a developer account on X"),
    ("Integrate Tool into Orchestration", "Adds the posting tool to the workflow"),
    ("Sign up for Buffer", "Registers an account"),
    ("Get an API key", "Obtains the access token for the API"),
    ("Install the Slack app", "Installs the app into the workspace"),
    ("Connect your Google account", "Authorises access"),
    ("Enable the Gmail API", "Turns on the API in the console"),
])
def test_a_step_that_is_the_persons_own_setup_goes_back(name, does):
    said = build_plan.setup_step(f"{name} {does}")
    assert said, name


@pytest.mark.parametrize("name, does", [
    ("Send the post", "Posts the tweet"),
    ("Draft the tweet", "Writes this week's post"),
    ("Create a new customer account in Stripe", "Makes the customer for this order"),
    ("Get the latest orders", "Reads this week's orders"),
    ("Connect to the database and read the rows", "Reads new rows"),
    ("Approve the post", "The person reads the draft"),
])
def test_what_each_run_does_is_kept(name, does):
    assert not build_plan.setup_step(f"{name} {does}")


async def test_the_twitter_plans_setup_steps_are_sent_back_with_why(client):
    import json

    from core import builder_access
    from core.builder_tools import execute_builder_tool_with_events
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import live
    from core.orchestration.state import SharedState

    class _Server:
        agent_sessions: dict = {}
        tool_router: dict = {}

    run = OrchestrationRun(run_id="builder_setup", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    plan = {"kind": "create", "title": "Weekly post", "summary": "Posts weekly.", "steps": [
        {"id": "acct", "type": "llm", "name": "Create Twitter Developer Account", "does": "Sets up access",
         "prompt": "Create the account."},
        {"id": "draft", "type": "llm", "name": "Draft the tweet", "does": "Writes it", "prompt": "Write a tweet."}]}
    with live(run):
        raw, _ = await execute_builder_tool_with_events("submit_plan", plan, _Server(), run_id=run.run_id)
    out = json.loads(raw) if isinstance(raw, str) else raw
    assert "step acct" in out["error"] and "done once by the person" in out["error"]
    assert "step draft" not in out["error"]
