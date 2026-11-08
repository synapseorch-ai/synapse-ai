"""
A build makes the HTTP tools its research read, after the yes, from the approved spec only (F944, SAI2-P3, D300;
P3B-HANDOFF.md, agreed with SAI2).

The approval covers every call the save makes; a read API's tools come first: one `create_http_tool` per endpoint the
plan calls, its URL on the provider's own host, its base locked (A8), its account part (`{dc}`) kept as `__dc__` until
its Connect card fills it, its tags from its method (claims only tighten). `step_make_tools` makes those first calls
and keeps its record; the save after the person connects goes on from there and never makes a tool twice, so the key
the card saved is never overwritten. What a failed save made is removed, the tools included (addition 2), and what
is made is the approved plan's spec, never findings written after the yes (addition 5).
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, build_save
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

SPEC = {"base_url": "https://{dc}.api.mailchimp.com/3.0",
        "host_params": [{"name": "dc", "says": "the part of your API key after the dash"}],
        "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
        "endpoints": [{"method": "POST", "path": "/campaigns/{campaign_id}/actions/send", "does": "Sends a campaign",
                       "tool": "mailchimp_send_campaign"}],
        "docs_url": "https://mailchimp.com/developer/"}
FINDINGS = {"goal": "send the newsletter", "options": [{
    "provider": "Mailchimp", "how": "api", "detail": "REST", "sources": ["https://mailchimp.com/developer/"],
    "own_domains": ["mailchimp.com"], "api": SPEC}]}
SEND = {"id": "send", "type": "tool", "name": "Send it", "does": "Sends the newsletter", "tool": "mailchimp_send_campaign"}
ASK = {"id": "ask", "type": "human", "name": "Approve the send", "does": "The person reads the newsletter",
       "prompt": "Send this newsletter?"}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace(monkeypatch):
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    monkeypatch.setattr(build_plan, "MAKES_HTTP_TOOLS", True)  # F946 turns it on with the wait
    await S.seed_agents([S.make_agent(id="agent_desk", name="Front desk", tools=[])])
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _approved():
    run = OrchestrationRun(run_id="builder_make", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: FINDINGS})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Newsletter", "summary": "Sends the newsletter.",
             "steps": [ASK, SEND]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    assert "build" in checked["offers"], checked["say"]
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    return run, checked


async def _tool(name="mailchimp_send_campaign"):
    from core.store.resources import load_tools

    return next((t for t in await load_tools() if t["name"] == name), None)


async def test_the_tools_come_first_in_the_approved_calls_from_the_spec(client):
    run, checked = await _approved()
    calls = build_save.save_calls(build_save._plan_of(checked), {}, run.shared_state)
    first = calls[0]
    assert (first["tool"], first["kind"]) == ("create_http_tool", "tool")
    args = first["args"]
    assert args["name"] == "mailchimp_send_campaign" and args["method"] == "POST"
    assert args["url"] == "https://__dc__.api.mailchimp.com/3.0/campaigns/{campaign_id}/actions/send"
    assert args["base_lock"] == "https://__dc__.api.mailchimp.com/3.0" and args["group"] == "Mailchimp"
    schema = json.loads(args["input_schema_json"])
    assert schema["required"] == ["campaign_id"] and set(schema["properties"]) == {"campaign_id", "body"}
    assert args["body_template"] == "{{body}}" and "read_only" not in args
    assert "## The tools it makes" in checked["review"] and "POST /campaigns/{campaign_id}/actions/send" in checked["review"]


async def test_make_tools_makes_only_the_tools_and_the_save_after_never_makes_them_again(client):
    run, _ = await _approved()
    made, _ = await _call("save_build", {"only": "tools"}, run)
    assert made["outcome"] == "tools_made" and made["tools"] == ["mailchimp_send_campaign"], made
    tool = await _tool()
    assert tool["base_lock"] == "https://__dc__.api.mailchimp.com/3.0"
    from core.store.resources import load_orchestrations

    assert not [o for o in await load_orchestrations() if o.get("name") == "Newsletter"], "nothing but the tools yet"
    # The Connect card saved the key meanwhile (F945): the save after never overwrites it.
    from core.store.resources import save_tool

    await save_tool({**tool, "headers": {"Authorization": "Bearer stored-on-connect"}})
    saved, _ = await _call("save_build", {}, run)
    assert saved["outcome"] == "saved", saved
    assert (await _tool())["headers"] == {"Authorization": "Bearer stored-on-connect"}
    assert [o for o in await load_orchestrations() if o.get("name") == "Newsletter"]


async def test_a_failed_save_removes_the_tools_it_made(client, monkeypatch):
    run, _ = await _approved()
    await _call("save_build", {"only": "tools"}, run)
    assert await _tool() is not None
    from core.product_tools import registry

    real = registry.invoke

    async def fails(name, *a, **kw):
        if name == "create_orchestration":
            raise RuntimeError("down")
        return await real(name, *a, **kw)

    monkeypatch.setattr(registry, "invoke", fails)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped" and await _tool() is None
    assert any(e.get("type") == "resource_saved" and e.get("action") == "deleted"
               and e.get("name") == "mailchimp_send_campaign" for e in events)


async def test_what_is_made_is_the_approved_spec_never_findings_written_after_the_yes(client):
    """Addition 5: `writes_between` holds; findings changed after the yes change nothing made."""
    run, _ = await _approved()
    moved = json.loads(json.dumps(FINDINGS))
    moved["options"][0]["api"]["base_url"] = "https://{dc}.api.evil.example/3.0"
    run.shared_state[build_plan.FINDINGS_KEY] = moved
    await _call("save_build", {"only": "tools"}, run)
    assert (await _tool())["url"].startswith("https://__dc__.api.mailchimp.com/3.0/")


async def test_a_tool_named_since_the_yes_by_someone_else_stops_the_make(client):
    from core.store.resources import save_tool

    run, _ = await _approved()
    await save_tool({"name": "mailchimp_send_campaign", "tool_type": "http", "method": "GET",
                     "url": "https://hooks.example.org/x", "headers": {}, "inputSchema": {"type": "object"}})
    out, _ = await _call("save_build", {"only": "tools"}, run)
    assert out["outcome"] == "stopped" and (await _tool())["url"] == "https://hooks.example.org/x"


async def test_a_tool_it_will_make_that_acts_needs_an_approval_before_the_yes(client):
    """Not here to read until it is made, a POST is held by its method now, not after (decision 4)."""
    run = OrchestrationRun(run_id="builder_make", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: FINDINGS})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Newsletter", "summary": "Sends it.", "steps": [SEND]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    assert "build" not in checked["offers"] and any("Human step" in i for i in checked["issues"])
