"""
Plan first, connect after the yes, for every way in (F949, SAI2-P3, D300; Naveen 10-24; SAI2's notes).

Before (P3a, Q3), an app or an MCP server the plan needed was connected at the review: its Connect card, the plan and
Build it (when it was offered) all arrived together, and Naveen expected one at a time. Now:
- the review shows the plan alone and offers Build it, saying "after your yes, I'll ask you to connect Notion";
- after the yes: the tools a build makes (F944), then one Connect card per provider (apps, MCP servers and made tools
  alike), then the wait (F946);
- on "Connected", code compares, tool by tool, what the approved plan names with what is here now:
  every one present → the save; the provider connected but its tools named otherwise (or only some there) → back to
  the planner on the real names and a second review, its own approval (Naveen, in SAI2-P3's tab: one more yes);
  the provider not connected yet → asked again;
- a tool the build made that the second plan no longer uses is removed at the save (SAI2's change 2).
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_connect
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, build_save, bundled_definitions
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role
from core.store.resources import load_tools, save_tool

NOTION = {"provider": "Notion", "how": "mcp", "detail": "Its own MCP server",
          "sources": ["https://registry.modelcontextprotocol.io/v0/servers/notion"], "own_domains": ["notion.so"]}
ASK = {"id": "ask", "type": "human", "name": "Approve the note", "does": "The person reads it", "prompt": "Add it?"}
ADD = {"id": "add", "type": "tool", "name": "Add the page", "does": "Adds a Notion page", "tool": "notion_add_page"}


class _Server:
    agent_sessions: dict = {}

    def __init__(self):
        self.tool_router = {}


SERVER = _Server()


@pytest.fixture(autouse=True)
async def workspace():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    async def options():
        return {"catalog": [{"slug": "notion", "name": "Notion", "auth": "oauth", "url": "https://mcp.notion.com"}],
                "providers": {}, "screens": {"apps": "Apps"}}

    hooks.set_role_source(owner)
    builder_connect.set_connect_options(options)
    SERVER.tool_router = {}
    yield
    hooks.set_role_source(None)
    builder_connect.set_connect_options(None)
    builder_access.forget_runs()


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, SERVER, run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _reviewed(steps=(ASK, ADD)):
    run = OrchestrationRun(run_id="builder_after_yes", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: {"goal": "x", "options": [NOTION]}})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Notes", "summary": "Adds a page.",
             "steps": list(steps)}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, events = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    return run, checked, events


async def _yes(run):
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    return approved


async def _connect(*names):
    """Notion connected: its server's tools are here under these names."""
    for name in names:
        await save_tool({"name": name, "tool_type": "http", "method": "POST", "url": f"https://api.notion.com/v1/{name}",
                         "headers": {"Authorization": "Bearer k"}, "inputSchema": {"type": "object"}})
        SERVER.tool_router[f"notion__{name}"] = ("ext_mcp_notion", name)


async def _answer(run, words):
    run.shared_state["connect_answer"] = {"feedback": words}
    return await _call("read_connect_answer", {}, run)


async def test_the_review_shows_the_plan_alone_and_offers_build_it():
    _, checked, events = await _reviewed()
    assert checked["outcome"] == "review" and "build" in checked["offers"]
    assert not [e for e in events if e.get("type") == "connection_needed"], "no card beside the plan"
    assert checked["after_yes"] == ["`notion_add_page`: after your yes, I'll ask you to connect Notion (in the Apps "
                                    "catalog)."]
    assert "## After your yes" in checked["review"] and "after your yes" not in checked["say"]


async def test_after_the_yes_one_card_per_provider_then_the_wait():
    run, _, _ = await _reviewed()
    await _yes(run)
    made, events = await _call("save_build", {"only": "tools"}, run)
    assert made["outcome"] == "tools_made" and made["tools"] == [] and made["waits"] == ["Notion"]
    [card] = [e for e in events if e.get("type") == "connection_needed"]
    assert card["kind"] == "app" and card["app"]["slug"] == "notion"
    assert "Connect Notion on the card above" in made["say"]


async def test_connected_with_every_planned_tool_here_goes_on_to_the_save():
    run, _, _ = await _reviewed()
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    await _connect("notion_add_page")
    out, _ = await _answer(run, "Connected Notion.")
    assert out["outcome"] == "connected"
    saved, _ = await _call("save_build", {}, run)
    assert saved["outcome"] == "saved", saved


async def test_connected_under_other_tool_names_re_plans_for_one_more_yes():
    run, _, _ = await _reviewed()
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    await _connect("notion_create_page", "notion_search")
    out, _ = await _answer(run, "Connected Notion.")
    assert out["outcome"] == "replan"
    assert "notion_create_page" in out["say"] and "one more yes" in out["say"]


async def test_some_of_the_planned_tools_here_is_a_re_plan_never_a_partial_save():
    second = {**ADD, "id": "find", "name": "Find it", "tool": "notion_search_pages"}
    run, _, _ = await _reviewed((ASK, ADD, second))
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    await _connect("notion_add_page", "notion_query")
    out, _ = await _answer(run, "Connected Notion.")
    assert out["outcome"] == "replan"


async def test_saying_connected_before_it_is_is_asked_again():
    run, _, _ = await _reviewed()
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    out, _ = await _answer(run, "Connected Notion.")
    assert out["outcome"] == "again" and "Notion is not connected yet" in out["say"]


async def test_the_second_review_says_why_and_is_its_own_approval():
    from core.native_builder.review import approved_review

    run, checked, _ = await _reviewed()
    await _yes(run)
    first = approved_review(run.shared_state)
    await _call("save_build", {"only": "tools"}, run)
    await _connect("notion_create_page")
    out, _ = await _answer(run, "Connected Notion.")
    run.shared_state[build_plan.ANSWER_KEY] = out
    draft = {"finish": "submit_plan", "kind": "create", "title": "Notes", "summary": "Adds a page.",
             "steps": [ASK, {**ADD, "tool": "notion_create_page"}]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    again, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = again
    assert again["outcome"] == "review" and "After connecting Notion" in again["review"]
    assert again["digest"] != checked["digest"]
    await _yes(run)
    second = approved_review(run.shared_state)
    assert first is not None and second is not None and second.id != first.id
    # Altered after the second review, before its yes would save it: refused (writes_between holds).
    run.shared_state[build_plan.CHECKED_KEY] = {**again, "plan": {**again["plan"], "title": "Something else"}}
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    refused, _ = await _call("approve_plan", {}, run)
    assert refused.get("error")


async def test_build_without_the_app_after_the_yes_goes_back_for_a_new_review():
    run, _, _ = await _reviewed()
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    out, _ = await _answer(run, "build without Notion")
    assert out["outcome"] == "without" and out["without"] == ["notion_add_page"]


async def test_a_made_tool_the_second_plan_no_longer_uses_is_removed_at_the_save():
    """SAI2's change 2: a carried-over made tool is kept only if the re-reviewed plan uses it."""
    run, _, _ = await _reviewed()
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    # As step_make_tools leaves a made tool: recorded in this build's save record.
    await save_tool({"name": "mailchimp_send_campaign", "tool_type": "http", "method": "POST",
                     "url": "https://us6.api.mailchimp.com/3.0/x", "base_lock": "https://us6.api.mailchimp.com/3.0",
                     "headers": {"Authorization": "Bearer k"}})
    from core.native_builder.review import approved_review

    run.step_progress[build_save.PROGRESS_KEY] = {
        "approval": approved_review(run.shared_state).id, "made": {}, "calls_done": [],
        "done": [["tool", "mailchimp_send_campaign", "mailchimp_send_campaign"]]}
    await _connect("notion_create_page")
    out, _ = await _answer(run, "Connected Notion.")
    run.shared_state[build_plan.ANSWER_KEY] = out
    draft = {"finish": "submit_plan", "kind": "create", "title": "Notes", "summary": "Adds a page.",
             "steps": [ASK, {**ADD, "tool": "notion_create_page"}]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    again, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = again
    await _yes(run)
    await _call("save_build", {"only": "tools"}, run)
    saved, events = await _call("save_build", {}, run)
    assert saved["outcome"] == "saved", saved
    assert "mailchimp_send_campaign" not in {t["name"] for t in await load_tools()}
    assert any(e.get("action") == "deleted" and e.get("name") == "mailchimp_send_campaign" for e in events)


def test_the_route_waits_for_every_way_in_and_re_plans_on_other_names():
    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    assert "waits" in steps["step_make_route"]["switch_expression"]
    cases = steps["step_connect_route"]["switch_cases"]
    assert cases["replan"] == "step_plan_context" and cases["connected"] == "step_save"


def test_the_card_draws_after_your_yes_as_the_review_does():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "frontend/src/components/assistant"
    plan_ts, cards = (root / "plan.ts").read_text(), (root / "cards.tsx").read_text()
    assert "afterYes: bullets(view.after_yes)," in plan_ts and ">After your yes</span>" in cards


# ── a server set up on its own screen, and "done" (F1104) ─────────────────────────────────────────────────────────
# P5's gate measure, res-04/05 (10-27): an MCP server the catalogue doesn't have (Shippo) gets the Apps screen's card,
# a link with no press of its own; and the wait went on only on "Connected …". A person who added the server and came
# back saying "done" was asked again, in a loop. Now any reply that says it is done runs the same exact check, which
# alone decides; a "not yet" is asked again, and "build without X" and "stop" go first.

SHIPPO = {"provider": "Shippo", "how": "mcp", "detail": "Its own MCP server",
          "sources": ["https://registry.modelcontextprotocol.io/v0/servers/com.shippo/mcp"],
          "own_domains": ["goshippo.com"]}
BUY = {"id": "buy", "type": "tool", "name": "Buy the label", "does": "Buys a Shippo shipping label",
       "tool": "shippo_buy_label"}


async def _waiting_for_shippo():
    run, _, _ = await _reviewed()
    run.shared_state[build_plan.FINDINGS_KEY] = {"goal": "x", "options": [SHIPPO]}
    draft = {"finish": "submit_plan", "kind": "create", "title": "Labels", "summary": "Buys a label.",
             "steps": [ASK, {**BUY, "next": None}]}
    draft["steps"][0] = {**ASK, "next": "buy"}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    await _yes(run)
    made, events = await _call("save_build", {"only": "tools"}, run)
    return run, made, events


async def _add_shippo():
    await save_tool({"name": "shippo_buy_label", "tool_type": "http", "method": "POST",
                     "url": "https://api.goshippo.com/transactions", "headers": {"Authorization": "ShippoToken k"},
                     "inputSchema": {"type": "object"}})
    SERVER.tool_router["shippo__shippo_buy_label"] = ("ext_mcp_shippo", "shippo_buy_label")


async def test_a_server_set_up_on_its_own_screen_is_waited_for_with_what_to_say():
    run, made, events = await _waiting_for_shippo()
    [card] = [e for e in events if e.get("type") == "connection_needed"]
    assert card["kind"] == "settings", "Shippo is not in the catalogue: the Apps screen's card"
    assert card["waits_for"] == "Shippo", "its Connected press says \"Connected Shippo.\", and the check decides"
    assert made["waits"] == ["Shippo"]
    assert "Connect Shippo on the card above and tell me when it's done" in made["say"]


@pytest.mark.parametrize("words", ["done", "Done!", "I added it", "ok, it's set up", "all set", "finished",
                                   "Connected Shippo."])
async def test_done_with_the_server_here_goes_on_to_the_save(words):
    run, _, _ = await _waiting_for_shippo()
    await _add_shippo()
    out, _ = await _answer(run, words)
    assert out["outcome"] == "connected", (words, out)


async def test_done_before_the_server_is_here_waits_with_the_same_line_each_time():
    run, _, _ = await _waiting_for_shippo()
    first, _ = await _answer(run, "done")
    again, _ = await _answer(run, "done")
    assert first["outcome"] == again["outcome"] == "again"
    assert first["say"] == again["say"]
    assert "Shippo is not connected yet" in first["say"] and "tell me when it's done" in first["say"]
    assert "press Connect." not in first["say"], "the Apps screen's card has no Connect press"


@pytest.mark.parametrize("words", ["not done yet", "I haven't added it", "I can't", "it won't connect", "not yet",
                                   "no, not done"])
async def test_a_not_yet_is_asked_again_even_with_the_server_here(words):
    run, _, _ = await _waiting_for_shippo()
    await _add_shippo()
    out, _ = await _answer(run, words)
    assert out["outcome"] == "again", (words, out)
    assert "tell me when it's done" in out["say"]


async def test_build_without_and_stop_go_before_a_done_word():
    run, _, _ = await _waiting_for_shippo()
    out, _ = await _answer(run, "build without Shippo")
    assert out["outcome"] == "without" and out["without"] == ["shippo_buy_label"]
    run, _, _ = await _waiting_for_shippo()
    out, _ = await _answer(run, "stop")
    assert out["outcome"] == "stop"


@pytest.mark.parametrize("words", ["how do I set it up?", "is it added?", "Done? What next?"])
async def test_a_question_at_the_wait_is_answered_never_saved(words):
    """SAI2's tightening: "set up" and "added" are done-words, but a question asks; with Shippo here it still waits."""
    run, _, _ = await _waiting_for_shippo()
    await _add_shippo()
    out, _ = await _answer(run, words)
    assert out["outcome"] == "again" and "tell me when it's done" in out["say"]
