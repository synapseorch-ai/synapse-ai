"""After a research turn's findings, "go ahead and build it" goes to the build, with those findings (F766, BWA1, D274).

BWA1's self-hosted measure (2026-10-20, Gemini Flash): every follow-up to the findings card — "Please go ahead and
set up the workflow to send a Stripe payment link", or "approve" — was routed `research` again or `answer`, never
the build. The router had no sign that the provider was already found, and its research route wins "even when
worded as build". Naveen: after findings, a research-shaped request goes to build, with `plan_findings` from the same
record. The previous turn's findings come into the turn's state in code; the router's `research` then goes to the
build, and the drafter plans from them (F763's look-up does not run again for what is already found).
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


def test_a_build_with_findings_already_does_not_look_them_up_again():
    gate = _steps()["step_plan_needs_research"]
    state = {"plan_check": {"research": ["stripe_create_payment_link"]}, "plan_findings": STRIPE}
    value, _ = _eval_state_expr(gate["if_condition"], state, default=False)
    assert value is False
    value, _ = _eval_state_expr(gate["if_condition"], {**state, "plan_findings": {}}, default=False)
    assert value is True


def test_the_analyst_and_the_drafter_read_the_findings():
    steps = _steps()
    assert "plan_findings" in steps["step_understand"]["input_keys"]
    assert "plan_findings" in steps["step_draft_plan"]["input_keys"]
    schema = bundled_definitions()["orchestration"]["state_schema"]
    assert schema["prior_findings"]["type"] == "dict"


@pytest.fixture
async def builder():
    await seed_native_builder()


PLAN = "## Steps\n1. **Create Payment Link** (tool step) — forced tool `stripe_create_payment_link`. Next: end."


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
        "Requirements: send a Stripe payment link when a plan is accepted.",   # step_understand
        route("draft"),                                        # step_clarify_route
        PLAN,                                                  # step_draft_plan
        tool_call("check_plan", tools=["stripe_create_payment_link"]),
        PLAN + "\n\n## Open questions\n- Connect Stripe first.",               # the redraft
        tool_call("check_plan", tools=["stripe_create_payment_link"]),
    ])
    second = await stream(client, "/api/builder/chat",
                          {"message": "Please go ahead and set up the workflow to send the Stripe link", **conv})
    order = [e.get("orch_step_id") for e in second if e["type"] == "step_start"]
    assert "step_research" not in order and "step_plan_research" not in order, order
    assert order.index("step_may_change") < order.index("step_understand") < order.index("step_human_review")
    drafts = [c for c in fake_llm.calls if "Plan Drafter" in str(c.get("prompt_msg") or c.get("sys_prompt"))]
    # The findings as the first turn recorded them (its sources only what that run read): the goal and the provider.
    first_draft = str(drafts[0].get("prompt_msg")) if drafts else ""
    assert "send a payment link" in first_draft and "Stripe" in first_draft, "the first draft already has them"
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
