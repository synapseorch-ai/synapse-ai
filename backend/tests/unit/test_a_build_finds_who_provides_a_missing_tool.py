"""A build that names a tool nothing here provides finds who provides it before it is redrafted (F763, BWA1, D274).

BWA1's batches (2026-10-16/17): research personas whose run asked a clarifying question researched 6 times in 18,
the others 17 in 21. Every one of those misses was routed `create`, and the create path never looked a provider up.
"When a treatment plan is accepted, send the patient a Stripe payment link": the analyst asked the person how Stripe
should be connected; check_plan found the Stripe and email tools that nothing provides, and its one redraft note
said what has no real tool "becomes an Open question, not a step" — so the drafter deleted them, and Haiku built
"… (Extraction Stage)": extract, transform, end.

Naveen (2026-10-20): the Researcher runs on check_plan's own list of missing tools — decided in code, never the
analyst's words — once, before the redraft; the build still waits until the app is connected, and a Connect card
makes that one click. Since SAI2-P3 that list is `check_build`'s, read off the typed Plan's tool status, and the
research goes back to the planner (`test_the_build_route_decides_in_code`); `check_plan` stays for a run paused on
the old definition (F443).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.builder_guard import check_plan, offer_connections
from core.native_builder import bundled_definitions
from core.orchestration.steps import _eval_state_expr

PLAN = """## Overview
When a treatment plan is accepted, create a Stripe payment link and email it to the patient.

## Steps
1. **Create Payment Link** (tool step) — forced tool `stripe_create_payment_link`. Next: Email the Link.
2. **Email the Link** (agent step) — new agent `link_mailer` with tools: [send_email_via_gmail]. Next: end.
"""


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _steps() -> dict:
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


LISTED = ["stripe_create_payment_link", "send_email_via_gmail"]  # what the check step's model reads off the plan


async def test_the_first_check_names_the_tools_to_look_up():
    first = await check_plan(PLAN, _Server(), listed=LISTED)
    assert first["redraft"] is True
    assert first["research"] == ["stripe_create_payment_link", "send_email_via_gmail"]
    second = await check_plan(PLAN, _Server(), checks=first["checks"], listed=LISTED)
    assert second["research"] == [], "looked up once per build"


async def test_a_missing_reused_agent_alone_is_not_looked_up():
    plan = ("## Steps\n1. **Answer** *(agent)*\n\n## Agents summary\n### Existing agents reused\n"
            "- `reader → agent_01a136be55157353 (Front desk)`\n")
    got = await check_plan(plan, _Server())
    assert got["redraft"] is True and got["missing_agents"] and got["research"] == []


async def test_a_tool_only_mentioned_is_not_looked_up():
    plan = ("## Overview\nPosts the digest. A later version could use `slack_post_message`.\n\n"
            "## Steps\n1. **Digest** (llm step). Next: end.")
    got = await check_plan(plan, _Server())
    assert got["research"] == []


async def test_the_redraft_note_keeps_what_a_provider_may_supply():
    """The note no longer tells the drafter to drop every tool nothing here provides: who provides it is looked up
    first, and only what nobody provides becomes an Open question."""
    first = await check_plan(PLAN, _Server(), listed=LISTED)
    assert "not a step" not in first["note"]
    assert "plan_findings" in first["note"]


@pytest.mark.parametrize("state, goes_to", [
    # Code looks it up first (P3c's splice of P5's look-up); the Researcher reads only what it leaves.
    ({"plan_checked": {"outcome": "research", "research": ["stripe_create_payment_link"]}}, "step_plan_lookup"),
    ({"plan_checked": {"outcome": "review", "research": ["stripe_create_payment_link"]}}, "step_human_review"),
    ({"plan_checked": {"outcome": "review"}}, "step_human_review"),
    ({"plan_checked": None}, "step_build_failed"),   # unreadable: said, never researched or reviewed
])
def test_the_check_goes_to_the_researcher_only_on_its_own_list(state, goes_to):
    steps = _steps()
    assert steps["step_check_plan"]["next_step_id"] == "step_plan_route"
    route = steps["step_plan_route"]
    assert route["type"] == "switch"
    value, _ = _eval_state_expr(route["switch_expression"], state, default=None)
    assert route["switch_cases"].get(str(value), route["switch_default_step_id"]) == goes_to


def test_the_researcher_reports_and_the_planner_reads_the_findings():
    """P3b (SAI2's call, option a): the build's own researcher finishes on `report_findings` (E2), so its report is
    the findings, with no second model call to note them; the research route's researcher is untouched (P5's)."""
    steps = _steps()
    research, plan = steps["step_plan_research"], steps["step_plan"]
    assert research["type"] == "agent" and research["agent_id"] == "agent_native_builder_plan_researcher"
    assert research["finish_tools"] == ["report_findings"] and research["output_key"] == "plan_findings"
    assert "{state.plan_checked.research}" in research["prompt_template"]
    assert "{state.plan_draft.summary}" in research["prompt_template"]  # what the build is for, in the plan's words
    assert research["next_step_id"] == "step_plan" and "step_plan_findings" not in steps
    assert "{state.plan_findings}" in plan["prompt_template"]
    schema = bundled_definitions()["orchestration"]["state_schema"]
    assert schema["plan_findings"]["type"] == "dict"
    agents = {a["id"]: a for a in bundled_definitions()["agents"]}
    assert "report_findings" in agents["agent_native_builder_plan_researcher"]["tools"]
    # The research route (SAI2-P5): code looks first, and its Researcher finishes on report_findings too (E2). Until
    # P5, SAI2's interim rule kept report_findings away from it: the card was a quick step's, from its prose.
    researcher = agents["agent_native_builder_researcher"]
    assert steps["step_research"]["forced_tool"] == "look_up_providers" and steps["step_research"]["agent_id"] is None
    lookup_research = steps["step_lookup_research"]
    assert lookup_research["agent_id"] == "agent_native_builder_researcher"
    assert lookup_research["finish_tools"] == ["report_findings"] and "report_findings" in researcher["tools"]
    # One source for what both researchers share (F764): the same two passages, each named once.
    for name in ("research_role_and_ways", "research_rules"):
        from core.native_builder.seed import prompt_fragment

        text = prompt_fragment(name)
        assert text in researcher["system_prompt"] and text in agents["agent_native_builder_plan_researcher"][
            "system_prompt"]


def test_nothing_on_the_way_to_the_redraft_can_write():
    """From the research to the redraft, no step saves, creates or changes anything: the Researcher holds only its
    reads, and the review still comes before any build."""
    steps = _steps()
    agents = {a["id"]: a for a in bundled_definitions()["agents"]}
    writes = {"create_agents", "finish_build", "create_orchestration", "update_orchestration", "create_http_tool",
              "create_schedule", "update_agent", "create_agent", "prepare_action"}
    sid, seen = "step_plan_research", []
    while sid != "step_plan":
        step = steps[sid]
        seen.append(sid)
        tools = set(agents[step["agent_id"]]["tools"]) if step.get("agent_id") else {step.get("forced_tool")}
        assert not tools & writes, (sid, tools & writes)
        sid = step["next_step_id"]
    assert seen == ["step_plan_research"]


CATALOG = [{"slug": "stripe", "name": "Stripe", "auth": "oauth", "url": "https://mcp.stripe.com"}]


@pytest.fixture
def catalog(monkeypatch):
    import core.builder_connect as bc

    async def options():
        return {"catalog": CATALOG, "providers": {}, "screens": {}}

    monkeypatch.setattr(bc, "_read_options", options)


async def test_a_found_app_from_the_catalog_gets_its_connect_card_once(catalog):
    findings = {"options": [
        {"provider": "Stripe", "how": "mcp", "sources": ["https://registry.modelcontextprotocol.io/v0/servers/com.stripe/mcp"]},
        {"provider": "Mailchimp", "how": "api", "sources": ["https://mailchimp.com/developer/"]},
        {"provider": "Booking.com", "how": "none", "sources": ["https://developers.booking.com"]},
    ]}
    events: list[dict] = []
    said, offered = await offer_connections(PLAN + "\nUses Mailchimp too.", findings, [], events)
    [card] = [e for e in events if e["type"] == "connection_needed"]
    assert card["kind"] == "app" and card["app"]["slug"] == "stripe"
    assert offered == ["stripe"]
    assert "Stripe" in said and "card" in said
    assert "Mailchimp" in said and "https://mailchimp.com/developer/" in said
    assert "Booking.com" not in said, "a provider the plan does not use is not offered"

    again: list[dict] = []
    await offer_connections(PLAN, findings, offered, again)
    assert again == [], "one card per app per build"


async def test_with_no_catalog_a_found_provider_is_named_with_its_page(monkeypatch):
    import core.builder_connect as bc

    async def none():
        return {"catalog": [], "providers": {}, "screens": {}}

    monkeypatch.setattr(bc, "_read_options", none)
    events: list[dict] = []
    said, offered = await offer_connections(PLAN, {"options": [
        {"provider": "Stripe", "how": "mcp", "sources": ["https://docs.stripe.com/mcp"]}]}, [], events)
    assert events == [] and offered == []
    assert "Stripe" in said and "https://docs.stripe.com/mcp" in said


async def test_no_findings_offer_nothing(catalog):
    events: list[dict] = []
    assert await offer_connections(PLAN, None, [], events) == ("", [])
    assert await offer_connections(PLAN, SimpleNamespace(), [], events) == ("", [])
    assert events == []
