"""Through the engine: a build naming a tool nothing here provides runs the Researcher before its redraft, and the
redraft reads what it found (F763, BWA1, D274). A build whose tools all exist is not researched."""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import route, stream, tool_call


@pytest.fixture
async def builder():
    await seed_native_builder()


STRIPE_PLAN = ("## Steps\n1. **Create Payment Link** (tool step) — forced tool `stripe_create_payment_link`. "
               "Next: end.")


async def test_a_build_naming_a_missing_tool_finds_who_provides_it_before_the_review(client, builder, fake_llm):
    fake_llm.script([
        route("create"),                                                       # step_intent
        "Requirements: when a treatment plan is accepted, send a Stripe payment link.",   # step_understand
        route("draft"),                                                        # step_clarify_route
        STRIPE_PLAN,                                                           # step_draft_plan
        tool_call("check_plan", tools=["stripe_create_payment_link"]),         # step_check_plan (first)
        "Stripe has its own MCP server.\nStripe — mcp — a Stripe account — https://mcp.stripe.com",  # researcher
        tool_call("report_findings", goal="send a payment link", options=[      # step_plan_findings
            {"provider": "Stripe", "how": "mcp", "needs": "A Stripe account"}]),
        STRIPE_PLAN + "\n\n## Open questions\n- Connect Stripe first.",         # step_draft_plan (redraft)
        tool_call("check_plan", tools=["stripe_create_payment_link"]),         # step_check_plan (second)
    ])
    events = await stream(client, "/api/builder/chat",
                          {"message": "When a treatment plan is accepted, send the patient a Stripe payment link"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert order.index("step_plan_research") < order.index("step_plan_findings") < order.index("step_human_review")
    assert order.count("step_draft_plan") == 2 and order.count("step_plan_research") == 1
    assert [e["options"][0]["provider"] for e in events if e["type"] == "research_findings"] == ["Stripe"]
    assert any(e["type"] == "human_input_required" for e in events), "the person still reviews the plan"
    assert not [e for e in events if e["type"] == "resource_saved"], "nothing is built before the review"

    from core.orchestration.state import SharedState

    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]
    state = (await SharedState.restore(run_id)).run.shared_state
    assert isinstance(state["plan_findings"], dict) and state["plan_findings"]["options"][0]["provider"] == "Stripe"
    assert state["plan_check"]["research"] == [] and state["plan_check"]["checks"] == 2

    redraft_prompt = [c for c in fake_llm.calls if "Plan Drafter" in str(c.get("prompt_msg") or c.get("sys_prompt"))][-1]
    assert "Stripe" in str(redraft_prompt.get("prompt_msg")) and "mcp" in str(redraft_prompt.get("prompt_msg"))


async def test_a_build_whose_tools_all_exist_is_not_researched(client, builder, fake_llm):
    plan = "## Steps\n1. **List** (tool step) — forced tool `list_agents`. Next: end."
    fake_llm.script([
        route("create"),
        "Requirements: list the agents.",
        route("draft"),
        plan,
        tool_call("check_plan", tools=["list_agents"]),
    ])
    events = await stream(client, "/api/builder/chat", {"message": "Build a workflow that lists my agents"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert "step_plan_research" not in order and "step_human_review" in order
