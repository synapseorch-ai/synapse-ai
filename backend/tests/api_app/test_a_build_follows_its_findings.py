"""After a research turn's findings, "go ahead and build it" goes to the build, with those findings (F766, BWA1, D274).

BWA1's self-hosted measure (2026-10-20, Gemini Flash): every follow-up to the findings card — "Please go ahead and
set up the workflow to send a Stripe payment link", or "approve" — was routed `research` again or `answer`, never
the build. The router had no sign that the provider was already found, and its research route wins "even when
worded as build". Naveen: after findings, a research-shaped request goes to build, with `plan_findings` from the same
record. The previous turn's findings come into the turn's state in code; the router's `research` then goes to the
build, and the planner plans from them (F763's look-up does not run again for what is already found; since SAI2-P3
that is `check_build`'s own decision).
"""
from __future__ import annotations

import json

import pytest

from core.builder_research import previous_findings
from core.native_builder import bundled_definitions, seed_native_builder
from core.orchestration.steps import _eval_state_expr
from test_builder_routes import route, stream, tool_call

STRIPE = {"goal": "send a payment link", "options": [{"provider": "Stripe", "how": "mcp", "needs": "A Stripe account",
                                                      "sources": ["https://mcp.stripe.com"]}]}


def _steps() -> dict:
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


@pytest.mark.parametrize("state, found", [
    ({"findings": {"status": "reported", **STRIPE}}, True),
    ({"findings": json.dumps({"status": "reported", **STRIPE})}, True),   # a str-typed key may hold its JSON
    ({"findings": {"options": []}}, False),
    ({"findings": "not json"}, False),
    ({"summary": "Built it."}, False),
    ({}, False),
    (None, False),
])
def test_the_previous_turns_findings_are_its_findings_card_only(state, found):
    got = previous_findings(state)
    assert (got.get("options", [{}])[0].get("provider") == "Stripe") is found
    assert isinstance(got, dict)
    if not found:
        assert got == {}


@pytest.mark.parametrize("state, goes_to", [
    ({"prior_findings": STRIPE}, "step_may_change"),
    ({"prior_findings": {}}, "step_research"),
    ({"prior_findings": {"options": []}}, "step_research"),
    ({}, "step_research"),
])
def test_research_after_findings_goes_to_the_build_through_the_role_door(state, goes_to):
    steps = _steps()
    assert steps["step_intent"]["route_map"]["research"] == "step_researched_before"
    gate = steps["step_researched_before"]
    value, _ = _eval_state_expr(gate["if_condition"], state, default=False)
    assert (gate["if_true_step_id"] if value else gate["if_false_step_id"]) == goes_to
    # The build it reaches is the create path: the role door, then the plan room.
    assert steps["step_change_route"]["switch_default_step_id"] == "step_plan_room"


@pytest.mark.parametrize("findings, outcome", [(STRIPE, "review"), ({}, "research")])
async def test_a_build_with_findings_already_does_not_look_them_up_again(client, findings, outcome):
    from core import builder_access
    from core.builder_tools import execute_builder_tool_with_events
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import live
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_f766", orchestration_id="orch_native_builder", shared_state={
        builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", "plan_findings": findings})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Payment link", "summary": "s", "steps": [
        {"id": "link", "type": "tool", "name": "Link", "does": "x", "tool": "stripe_create_payment_link"}]}
    server = type("S", (), {"agent_sessions": {}, "tool_router": {}})()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("check_build", {"draft": draft}, server, run_id=run.run_id)
    assert json.loads(raw)["outcome"] == outcome


def test_the_planner_and_the_check_read_the_findings():
    import inspect

    from core.native_builder import build_plan
    assert "{state.plan_findings}" in _steps()["step_plan"]["prompt_template"]
    assert build_plan.FINDINGS_KEY == "plan_findings" and "FINDINGS_KEY" in inspect.getsource(build_plan._found)
    schema = bundled_definitions()["orchestration"]["state_schema"]
    assert schema["prior_findings"]["type"] == "dict" and schema["plan_findings"]["type"] == "dict"


@pytest.fixture
async def builder():
    await seed_native_builder()


PLAN = {"kind": "create", "title": "Payment link", "summary": "Send a Stripe payment link.", "steps": [
    {"id": "link", "type": "tool", "name": "Create Payment Link", "does": "Makes a Stripe payment link",
     "tool": "stripe_create_payment_link"}]}


async def test_go_ahead_after_the_findings_builds_from_them(client, builder, fake_llm):
    conv = {"client_session_id": "f766-conv"}
    fake_llm.script([
        route("research"),
        "Stripe has its own MCP server.\nStripe — mcp — a Stripe account — https://mcp.stripe.com",
        tool_call("report_findings", **STRIPE),
    ])
    first = await stream(client, "/api/builder/chat",
                         {"message": "When a plan is accepted, send the patient a Stripe payment link", **conv})
    assert [e for e in first if e["type"] == "research_findings"], "turn one ends in findings"

    fake_llm.reset()
    fake_llm.script([
        route("research"),                                     # the router still reads it as research
        tool_call("submit_plan", **PLAN),                      # the planner's finish (E2)
    ])
    second = await stream(client, "/api/builder/chat",
                          {"message": "Please go ahead and set up the workflow to send the Stripe link", **conv})
    order = [e.get("orch_step_id") for e in second if e["type"] == "step_start"]
    assert "step_research" not in order and "step_plan_research" not in order, order
    assert order.index("step_may_change") < order.index("step_plan") < order.index("step_human_review")
    plans = [c for c in fake_llm.calls
             if "Planner for Synapse AI" in str(c.get("sys_prompt")) + str(c.get("prompt_msg"))]
    # The findings as the first turn recorded them (its sources only what that run read): the goal and the provider.
    first_plan = str(plans[0].get("prompt_msg")) if plans else ""
    assert "send a payment link" in first_plan and "Stripe" in first_plan, "the first plan already has them"
    assert not [e for e in second if e["type"] == "resource_saved"], "the person still reviews the plan first"


async def test_research_with_no_findings_before_still_researches(client, builder, fake_llm):
    fake_llm.script([
        route("research"),
        "Shippo has a public API.\nShippo — api — an account and a key — https://docs.goshippo.com",
        tool_call("report_findings", goal="buy labels", options=[{"provider": "Shippo", "how": "api"}]),
    ])
    events = await stream(client, "/api/builder/chat",
                          {"message": "Automate buying shipping labels", "client_session_id": "f766-fresh"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert "step_research" in order and "step_may_change" not in order
