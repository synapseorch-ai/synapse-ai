"""
Synapse AI's build route decides every fact about a plan in code: the model only writes the Plan (F936 / F938,
SAI2-P3, D300).

Before, a markdown drafter wrote the plan, a quick model listed its tool names for `check_plan` to re-parse, and the
card parsed the markdown again. Now the planner finishes on `submit_plan` with a typed Plan. What it can fix — a step
that leads nowhere, a condition in words, a board tool called as a Tool step — goes back to it at once, with how to
fix it. `check_build` then fills in each tool's status, the approvals and the plan room (`objects.fill`), compiles the
Plan dry with placeholder ids and holds it to every rule a save door keeps, and decides: ask, said, research or the
review, with the card's text and the buttons the card offers. While a tool has no way in, Build it is never offered;
"Build without X" and "Find who can" are (Naveen, 10-23). Board tools are present while Boards are on (F866b).
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from _fakes.knowledge_env import use_saved_settings
from core import builder_access, builder_connect, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools.approved import digest

WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace():
    from core.store.resources import save_tool

    builder_access.forget_runs()
    await save_tool(dict(WEATHER))
    await S.seed_agents([S.make_agent(id="agent_desk", name="Front desk", tools=["rotterdam_weather"])])
    yield
    builder_access.forget_runs()
    builder_connect.set_connect_options(None)
    builder_tools.set_room_check(None)


@pytest.fixture
async def boards(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings

    async def switch(on: bool):
        await save_settings({"boards_enabled": on})

    return switch


async def _run(**state):
    run = OrchestrationRun(run_id="builder_bld", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", **state})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


def _plan(*steps, **more):
    return {"kind": "create", "title": "Rotterdam wind warning", "summary": "Warn when the wind is strong.",
            "steps": list(steps), **more}


CHECK = {"id": "check", "type": "tool", "name": "Check the weather", "does": "Reads today's weather",
         "tool": "rotterdam_weather"}
TELL = {"id": "tell", "type": "agent", "name": "Draft the warning", "does": "Drafts a delay warning",
        "agent": "agent_desk", "prompt": "Draft a short delay warning from {state.check}."}


async def _check(run, draft):
    run.shared_state[build_plan.DRAFT_KEY] = draft
    out, events = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = out
    return out, events


# ── the planner's finish ──────────────────────────────────────────────────────────────────────────────────────────

async def test_submit_plan_sends_back_what_the_planner_can_fix_with_how(client):
    run = await _run()
    out, _ = await _call("submit_plan", _plan(CHECK, {**TELL, "next": "nowhere"}), run)
    assert "goes to 'nowhere'" in out["error"]
    words = {"id": "windy", "type": "if_else", "name": "Windy?", "does": "Checks the wind",
             "condition": "if the wind is above 40 km/h", "branches": {"true": "tell", "false": "end"}}
    out, _ = await _call("submit_plan", _plan(CHECK, words, {**TELL, "next": "end"}), run)
    assert "if_condition" in out["error"] and "evaluator" in out["error"] and "state.check" in out["error"]


async def test_submit_plan_finishes_with_the_planner_s_plan_and_never_code_s_fields(client):
    run = await _run()
    out, _ = await _call("submit_plan", {**_plan(CHECK, TELL), "missing": ["x"], "approvals": {
        "acts_without_asking": ["rotterdam_weather"]}, "extra": 1}, run)
    assert out["finish"] == "submit_plan" and out["title"] == "Rotterdam wind warning"
    assert "missing" not in out and "approvals" not in out and "extra" not in out


async def test_a_plan_still_wrong_after_its_sendbacks_is_reviewed_with_its_issues_and_no_build_it(client):
    run = await _run()
    words = {"id": "windy", "type": "if_else", "name": "Windy?", "does": "x", "condition": "if it is windy",
             "branches": {"true": "tell", "false": "end"}}
    draft = _plan(CHECK, words, {**TELL, "next": "end"})
    for _ in range(build_plan.SENDBACKS):
        assert "error" in (await _call("submit_plan", draft, run))[0]
    out, _ = await _call("submit_plan", draft, run)
    assert out["finish"] == "submit_plan"
    checked, _ = await _check(run, out)
    assert checked["outcome"] == "review" and checked["issues"]
    assert "build" not in checked["offers"] and "## Can't be saved yet" in checked["review"]


# ── the check ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_plan_with_everything_here_is_reviewed_from_its_object(client):
    run = await _run()
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
    assert out["object"] == "plan" and out["outcome"] == "review"
    assert out["offers"] == ["build", "change", "stop"]
    assert [t["status"] for t in out["plan"]["tools"]] == ["present"]
    assert out["review"].startswith("# Rotterdam wind warning") and "Front desk" in out["review"]
    assert out["digest"] == digest(build_plan.reviewed_of(out))


async def test_a_tool_nothing_provides_is_looked_up_before_the_person_sees_the_plan(client):
    run = await _run()
    send = {"id": "send", "type": "tool", "name": "Send it", "does": "Sends the newsletter",
            "tool": "send_mailchimp_campaign"}
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(send)})
    assert out["outcome"] == "research" and out["research"] == ["send_mailchimp_campaign"]


async def test_an_api_only_provider_has_no_way_in_yet_so_build_it_is_never_offered(client):
    findings = {"goal": "x", "options": [{"provider": "Mailchimp", "how": "api", "detail": "REST",
                                          "sources": ["https://mailchimp.com/developer/"]}]}
    run = await _run(plan_findings=findings)
    send = {"id": "send", "type": "tool", "name": "Send it", "does": "Sends the Mailchimp newsletter",
            "tool": "send_mailchimp_campaign"}
    out, events = await _check(run, {"finish": "submit_plan", **_plan(send)})
    assert out["outcome"] == "review" and "build" not in out["offers"]
    assert "without:send_mailchimp_campaign" in out["offers"] and "find" not in out["offers"]
    assert "public API" in out["say"] and not [e for e in events if e.get("type") == "connection_needed"]


async def test_an_app_in_the_catalog_gets_its_connect_card_once_and_build_it_waits_for_it(client):
    async def options():
        return {"catalog": [{"slug": "notion", "name": "Notion", "auth": "oauth", "url": "https://mcp.notion.com"}]}

    builder_connect.set_connect_options(options)
    findings = {"goal": "x", "options": [{"provider": "Notion", "how": "mcp", "sources": ["https://notion.so"]}]}
    run = await _run(plan_findings=findings)
    note = {"id": "note", "type": "tool", "name": "Write it down", "does": "Adds a Notion page",
            "tool": "notion_add_page"}
    out, events = await _check(run, {"finish": "submit_plan", **_plan(note)})
    cards = [e for e in events if e.get("type") == "connection_needed"]
    assert [c["app"]["slug"] for c in cards] == ["notion"]
    assert "build" not in out["offers"] and "card above" in out["say"]
    _, again = await _check(run, {"finish": "submit_plan", **_plan(note)})
    assert not [e for e in again if e.get("type") == "connection_needed"]


async def test_build_without_x_drops_its_steps_in_code_and_then_build_it_is_offered(client):
    findings = {"goal": "x", "options": [{"provider": "Mailchimp", "how": "api", "sources": ["https://mailchimp.com"]}]}
    run = await _run(plan_findings=findings)
    send = {"id": "send", "type": "tool", "name": "Send it", "does": "Sends the Mailchimp newsletter",
            "tool": "send_mailchimp_campaign"}
    draft = {"finish": "submit_plan", **_plan(CHECK, send, TELL)}
    await _check(run, draft)
    run.shared_state["review_feedback"] = {"feedback": "Build without send_mailchimp_campaign"}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "without", "without": ["send_mailchimp_campaign"]}
    run.shared_state[build_plan.ANSWER_KEY] = answer
    out, _ = await _check(run, draft)
    assert [s["id"] for s in out["plan"]["steps"]] == ["check", "tell"]
    assert out["offers"][0] == "build" and out["without"] == ["send_mailchimp_campaign"]


@pytest.mark.parametrize("said, outcome", [
    ("Connected Notion.", "connected"), ("Saved the X-Api-Key for t.", "connected"),
    ("Make it run at 9 instead", "revise"), ("build without nothing", "revise")])
async def test_a_review_answer_that_is_not_a_yes_is_read_in_code(client, said, outcome):
    run = await _run(review_feedback={"feedback": said})
    assert (await _call("read_plan_answer", {}, run))[0]["outcome"] == outcome


async def test_board_tools_are_an_agents_while_boards_are_on(client, boards):
    post = {"id": "post", "type": "agent", "name": "Post it", "does": "Posts the warning on the board",
            "agent": "Board poster"}
    poster = {"name": "Board poster", "purpose": "Posts warnings on the board", "tools": ["board_add"]}
    await boards(True)
    run = await _run()
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, post, agents=[poster])})
    assert out["outcome"] == "review" and "build" in out["offers"]
    as_a_step = {"id": "post", "type": "tool", "name": "Post it", "does": "x", "tool": "board_add"}
    sent, _ = await _call("submit_plan", _plan(CHECK, as_a_step), run)
    assert "board tools are an agent's" in sent["error"]
    await boards(False)
    run = await _run()
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, post, agents=[poster])})
    assert out["outcome"] == "research" and out["research"] == ["board_add"]
    sent, _ = await _call("submit_plan", _plan(CHECK, boards=[{"name": "Shipment delays"}]), run)
    assert "Boards are off" in sent["error"]


async def test_an_update_applies_its_edits_to_the_workflow_as_it_is(client):
    existing = {"id": "orch_wind", "name": "Wind", "description": "", "entry_step_id": "check", "steps": [
        {"id": "check", "name": "Check the weather", "type": "tool", "forced_tool": "rotterdam_weather",
         "output_key": "check", "next_step_id": "done"},
        {"id": "done", "name": "End", "type": "end"}]}
    run = await _run(intent="update", existing_orch=existing, current_orchestration_id="orch_wind")
    edits = {"kind": "update", "title": "Wind", "summary": "Also draft a warning.", "edits": [
        {"op": "add_step", "after": "check", "step": TELL}]}
    out, _ = await _check(run, {"finish": "submit_plan", **edits})
    assert out["outcome"] == "review" and out["plan"]["kind"] == "update"
    assert [s["id"] for s in out["plan"]["steps"]][:2] == ["check", "tell"]


async def test_the_planner_s_question_and_cannot_are_passed_on(client):
    run = await _run()
    asked, _ = await _check(run, {"finish": "ask_person", "question": "Which list?", "about": "which",
                                  "options": ["Patients", "Staff"]})
    assert asked["outcome"] == "ask" and asked["question"] == "Which list?\n1. Patients\n2. Staff"
    said, _ = await _check(run, {"finish": "cannot", "why": "No printers here.", "nearest": "Email a PDF."})
    assert said["outcome"] == "said" and said["say"] == "No printers here. Email a PDF."


async def test_new_agents_are_sent_back_where_this_conversation_uses_existing_ones_only(client):
    run = await _run(can_create_agents=False)
    out, _ = await _call("submit_plan", _plan(CHECK, {**TELL, "agent": "Writer"}, agents=[
        {"name": "Writer", "purpose": "Writes warnings"}]), run)
    assert "existing agents only" in out["error"]


async def test_a_plan_that_does_not_fit_the_workspace_s_plan_is_said(client):
    async def room(kind, n):
        return "Your plan holds 3 workflows; this would be a 4th." if kind == "orchestration" else None

    builder_tools.set_room_check(room)
    run = await _run()
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
    assert out["outcome"] == "said" and "4th" in out["say"]


async def test_the_check_reads_the_workspace_s_own_catalogue_and_never_offers_synapse_ai_s_own(client):
    run = await _run()
    pack, _ = await _call("read_plan_context", {}, run)
    assert "rotterdam_weather" in {t["name"] for t in pack["tools"]}
    assert [a["id"] for a in pack["agents"]] == ["agent_desk"] and pack["kind"] == "create"


# ── the card's words and the check's (synced UI, F939) ────────────────────────────────────────────────────────────

def test_the_cards_build_without_words_are_what_the_answer_reads():
    """The card's "Build without X" sends `withoutWords(X)` (`plan.ts`); `read_plan_answer` must read it as that
    tool, as F759's test holds the yes words to the gates."""
    import re
    from pathlib import Path

    plan_ts = (Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/plan.ts").read_text()
    template = re.search(r"export function withoutWords\(tool: string\): string \{\s*return `([^`]+)`;", plan_ts)
    assert template, "plan.ts names the words its Build without button sends"
    sent = template.group(1).replace("${tool}", "send_mailchimp_campaign")
    assert build_plan._WITHOUT.match(sent).group("what") == "send_mailchimp_campaign"
    cards = (Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/cards.tsx").read_text()
    for offer in ("offers.includes('build')", "o.startsWith('without:')", "offers.includes('find')"):
        assert offer in cards, offer
