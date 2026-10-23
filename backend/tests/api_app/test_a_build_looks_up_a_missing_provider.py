"""Through the engine: a build naming a tool nothing here provides runs the Researcher before the plan is redrafted,
and the planner reads what it found (F763, BWA1, D274). A build whose tools all exist is not researched. Since
SAI2-P3 the planner writes the Plan once per pass and the check is code."""
from __future__ import annotations

import json

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import route, stream, tool_call


@pytest.fixture
async def builder():
    await seed_native_builder()


STRIPE_PLAN = {"kind": "create", "title": "Payment link", "summary": "Send a Stripe payment link.", "steps": [
    {"id": "link", "type": "tool", "name": "Create Payment Link", "does": "Makes a Stripe payment link",
     "tool": "stripe_create_payment_link"}]}


def _stripe_script():
    return [
        route("create"),                                                       # step_intent
        tool_call("submit_plan", **STRIPE_PLAN),                               # step_plan (the planner's finish)
        tool_call("report_findings", goal="send a payment link", options=[      # the researcher's finish (E2)
            {"provider": "Stripe", "how": "mcp", "needs": "A Stripe account"}]),
        tool_call("submit_plan", **{**STRIPE_PLAN, "questions": ["Connect Stripe first."]}),  # step_plan again
    ]


async def test_a_build_naming_a_missing_tool_finds_who_provides_it_before_the_review(client, builder, fake_llm):
    fake_llm.script(_stripe_script())
    events = await stream(client, "/api/builder/chat",
                          {"message": "When a treatment plan is accepted, send the patient a Stripe payment link"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert order.index("step_plan_research") < order.index("step_human_review") and "step_plan_findings" not in order
    assert order.count("step_plan") == 2 and order.count("step_plan_research") == 1
    assert [e["options"][0]["provider"] for e in events if e["type"] == "research_findings"] == ["Stripe"]
    asked = next(e for e in events if e["type"] == "human_input_required")
    assert asked["context"]["object"] == "plan", "the person reviews the plan, drawn from its object"
    assert not [e for e in events if e["type"] == "resource_saved"], "nothing is built before the review"

    from core.orchestration.state import SharedState

    state = (await SharedState.restore(asked["run_id"])).run.shared_state
    assert isinstance(state["plan_findings"], dict) and state["plan_findings"]["options"][0]["provider"] == "Stripe"
    checked = json.loads(state["plan_checked"]) if isinstance(state["plan_checked"], str) else state["plan_checked"]
    assert checked["outcome"] == "review" and checked["checks"] == 2
    # The fake researcher read no page and searched no registry, so its "mcp" is unclear (F577): the tool is still
    # missing, and the card offers building without it, never Build it.
    [tool] = checked["plan"]["tools"]
    assert tool["status"] == "missing" and "build" not in checked["offers"]
    assert "without:stripe_create_payment_link" in checked["offers"]

    replanned = [c for c in fake_llm.calls
                 if "Planner for Synapse AI" in str(c.get("sys_prompt")) + str(c.get("prompt_msg"))][-1]
    said = str(replanned.get("prompt_msg"))
    assert "'provider': 'Stripe'" in said and "'how': 'unclear'" in said, "the planner reads what the lookup found"


async def test_a_build_whose_tools_all_exist_is_not_researched(client, builder, fake_llm):
    from core.store.resources import save_tool

    await save_tool({"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
                     "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}})
    plan = {"kind": "create", "title": "Weather", "summary": "Read the weather.", "steps": [
        {"id": "read", "type": "tool", "name": "Read", "does": "Reads the weather", "tool": "rotterdam_weather"}]}
    fake_llm.script([route("create"), tool_call("submit_plan", **plan)])
    events = await stream(client, "/api/builder/chat", {"message": "Build a workflow that reads today's weather"})
    order = [e.get("orch_step_id") for e in events if e["type"] == "step_start"]
    assert "step_plan_research" not in order and "step_human_review" in order


async def test_an_approved_build_that_still_needs_its_tool_says_which_and_what_to_connect(client, builder,
                                                                                            fake_llm):
    """F768: a typed yes to a plan whose tool has no way in builds nothing: the card never offered Build it (Naveen,
    10-23), the approval refuses it in code, and the person is back at the same review, which says which tool and
    what to connect (SAI2's review, 18: no dead end, no model call)."""
    fake_llm.script(_stripe_script())
    events = await stream(client, "/api/builder/chat",
                          {"message": "When a treatment plan is accepted, send the patient a Stripe payment link"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    after = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": "approve"}})
    again = next(e for e in after if e["type"] == "human_input_required")
    assert again["orch_step_id"] == "step_human_review"
    assert again["prompt"].startswith("Here is the proposed plan. It can't be built as it is yet")
    assert "`stripe_create_payment_link`" in again["prompt"] and "Connect an app or service" in again["prompt"]
    assert not [e for e in after if e["type"] == "resource_saved"] and not fake_llm.calls
