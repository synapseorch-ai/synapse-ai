"""
A build's look-up is code first, as the research route's is (SAI2-P3's splice of SAI2-P5's `look_up_providers`; the
contract in P3B-HANDOFF.md, changed 10-27).

A plan that calls tools nothing here provides went straight to the plan researcher, a model reading the web. Now the
build route asks code first, with the needs check_build wrote (`{tool, does, provider?}`, the provider only one the
person named), and its own look-up locks code's finals (`by_route`):
- everything found → `report_findings` with no options of its own folds code's finals into `plan_findings` (and puts
  the findings card up), and the planner re-plans on them, with no model read the web;
- anything left → the plan researcher, told what code has already looked up and what is still to settle; code's
  finals still reach `plan_findings` through its own `report_findings`.
"""
from __future__ import annotations

import json

import pytest

import core.builder_connect as bc
import core.builder_research as br
import core.safe_fetch as sf
from core.native_builder import build_plan, lookup, seed

SHIPPO_OWN = {"server": {"name": "com.shippo/mcp", "websiteUrl": "https://goshippo.com",
                         "remotes": [{"url": "https://mcp.goshippo.com/mcp"}]}}


def _steps() -> dict[str, dict]:
    return {s["id"]: s for s in seed.bundled_definitions()["orchestration"]["steps"]}


# ── the definition ────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_plan_that_needs_research_is_looked_up_in_code_first():
    steps = _steps()
    assert steps["step_plan_route"]["switch_cases"]["research"] == "step_plan_lookup"
    look = steps["step_plan_lookup"]
    assert look["type"] == "tool" and look["forced_tool"] == "look_up_providers"
    assert look["tool_arguments"] == {"by_route": True, "needs": "{{state.plan_checked.needs}}"}
    assert look["output_key"] == "plan_lookup" and look["next_step_id"] == "step_plan_lookup_route"


def test_only_everything_found_skips_the_researcher():
    route = _steps()["step_plan_lookup_route"]
    assert route["switch_expression"] == '"found" if state.plan_lookup.found and not state.plan_lookup.left else ' \
                                         '"research"'
    assert route["switch_cases"] == {"found": "step_plan_found", "research": "step_plan_research"}
    assert route["switch_default_step_id"] == "step_plan_research"


def test_everything_found_is_reported_with_codes_finals_and_re_planned():
    found = _steps()["step_plan_found"]
    assert found["forced_tool"] == "report_findings"
    assert found["tool_arguments"] == {"goal": "{{state.plan_lookup.findings.goal}}", "options": []}
    assert found["output_key"] == "plan_findings" and found["next_step_id"] == "step_plan"


def test_the_researcher_reads_only_what_is_left_and_still_what_the_plan_is_for():
    template = _steps()["step_plan_research"]["prompt_template"]
    assert "{{ state.plan_lookup.left }}" in template
    assert "{{ state.plan_checked.research }}" in template and "{{ state.plan_draft.summary }}" in template
    assert "read_web_page" in template, "the page code read is served again at no cost"


# ── what it does ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_evidence():
    br._LOOSE.clear()
    yield
    br._LOOSE.clear()


@pytest.fixture
def world(monkeypatch):
    state: dict = {}

    async def options():
        return {"catalog": [{"slug": "stripe", "name": "Stripe", "url": "https://stripe.com"}], "providers": {},
                "screens": {}}

    async def fetch(url):
        term = url.split("search=", 1)[1].split("&", 1)[0].lower()
        return json.dumps({"servers": [SHIPPO_OWN] if term == "shippo" else []}).encode()

    async def scrape(server_module, args):
        if args["url"].startswith(br.SEARCH_URL):
            return {"markdown": "", "links": []}
        return {"markdown": ""}

    monkeypatch.setattr(bc, "_read_options", options)
    monkeypatch.setattr(sf, "fetch_bytes", fetch)
    monkeypatch.setattr(br, "_scrape", scrape)
    monkeypatch.setattr(build_plan, "_state", lambda: state)
    return state


async def test_needs_with_named_providers_found_in_code_become_the_plans_findings(world):
    from core.orchestration.steps import render_tool_arguments

    world["plan_checked"] = {"outcome": "research", "research": ["create_payment_link", "create_label"], "needs": [
        {"tool": "create_payment_link", "does": "Makes a Stripe payment link", "provider": "Stripe"},
        {"tool": "create_label", "does": "Buys a Shippo shipping label", "provider": "Shippo"}]}
    args = render_tool_arguments(_steps()["step_plan_lookup"]["tool_arguments"], world)
    world["plan_lookup"] = await lookup.look_up_providers(args, None, [])
    assert world["plan_lookup"]["found"] and not world["plan_lookup"]["left"], world["plan_lookup"]
    events: list[dict] = []
    reported = await br.report_findings(
        render_tool_arguments(_steps()["step_plan_found"]["tool_arguments"], world), None, events)
    assert {(o["provider"], o["how"]) for o in reported["options"]} == {("Stripe", "catalog_app"), ("Shippo", "mcp")}
    assert [e["type"] for e in events] == ["research_findings"], "the findings card is put up"


async def test_a_need_no_one_named_is_left_for_the_researcher(world):
    from core.orchestration.steps import render_tool_arguments

    world["plan_checked"] = {"outcome": "research", "research": ["send_sms"],
                             "needs": [{"tool": "send_sms", "does": "Texts the customer"}]}
    world["plan_lookup"] = await lookup.look_up_providers(
        render_tool_arguments(_steps()["step_plan_lookup"]["tool_arguments"], world), None, [])
    assert not world["plan_lookup"]["found"]
    assert [x["tool"] for x in world["plan_lookup"]["left"]] == ["send_sms"]
