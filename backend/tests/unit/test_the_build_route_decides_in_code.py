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
    # A new draft from the planner (a re-plan) starts with none: X is never dropped again unasked.
    replanned, _ = await _check(run, {**draft, "summary": "Send it after all."})
    assert "send" in [s["id"] for s in replanned["plan"]["steps"]] and replanned["without"] == []


@pytest.mark.parametrize("said, outcome", [
    ("Connected Notion.", "connected"), ("Saved the X-Api-Key for t.", "connected"),
    ("Signed in to Notion.", "connected"),
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
    # Buildable: its new agent's instructions are written next, before the review (Q2).
    assert out["outcome"] == "prompts" and [t["status"] for t in out["plan"]["tools"]] == ["present", "present"]
    as_a_step = {"id": "post", "type": "tool", "name": "Post it", "does": "x", "tool": "board_add"}
    sent, _ = await _call("submit_plan", _plan(CHECK, as_a_step), run)
    assert "board tools are an agent's" in sent["error"]
    await boards(False)
    run = await _run()
    sent, _ = await _call("submit_plan", _plan(CHECK, post, agents=[poster]), run)
    assert "Boards are off" in sent["error"] and "`board_add`" in sent["error"]  # never looked up as a provider
    sent, _ = await _call("submit_plan", _plan(CHECK, boards=[{"name": "Shipment delays"}]), run)
    assert "a plan makes no shared board" in sent["error"]


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
    # The card says what changes, not only the whole workflow (SAI2's review, 13).
    assert out["changes"] == ['Change the step "Check the weather": what follows it',
                              'Add the step "Draft the warning" (agent): Drafts a delay warning']
    assert "## What changes" in out["review"]


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


async def test_a_shared_board_the_plan_names_is_said_to_be_out_of_reach_and_never_made_again(client, boards):
    """SAI2's review: Dana's "let me approve it before it goes on the Shipment delays board". The agent posts to its
    workflow's own board; giving a workflow a shared board's access has no tool yet, so the card says so plainly and
    no second "Shipment delays" is made."""
    from core.store.boards import create_shared_board

    await boards(True)
    await create_shared_board("Shipment delays")
    run = await _run()
    post = {"id": "post", "type": "agent", "name": "Post it", "does": "Posts the warning on the Shipment delays board",
            "agent": "agent_desk"}
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, post)})
    assert out["plan"]["boards"] == []
    assert 'not to "Shipment delays"' in out["say"] and "## Boards" in out["review"]
    assert out["notes"] and "can't do yet" in out["notes"][0]


async def test_a_plan_holding_a_key_is_sent_back(client):
    """SAI2's review, 14: a key in a step's prompt would be saved with the workflow."""
    from core.native_builder.export import CREDENTIAL_MARK

    run = await _run()
    keyed = {**TELL, "prompt": "Use the key sk_live_" + "Z" * 24 + " to post."}
    sent, _ = await _call("submit_plan", _plan(CHECK, keyed), run)
    assert "holds a key" in sent["error"]
    marked = {**TELL, "prompt": f"Use the key {CREDENTIAL_MARK} to post."}
    sent, _ = await _call("submit_plan", _plan(CHECK, marked), run)
    assert "holds a key" in sent["error"]


async def test_an_update_is_not_refused_for_what_already_runs_only_for_what_it_adds(client):
    """SAI2's review, 1: an existing workflow with an ungated step that acts runs today; an edit elsewhere in it is
    not refused for that step, while an acting step the edit adds is held to decision 4."""
    from core.store.resources import save_tool

    await save_tool({"name": "post_update", "description": "Posts an update outside.", "tool_type": "http",
                     "method": "POST", "url": "https://api.example.com/post", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}})
    existing = {"id": "orch_live", "name": "Live", "description": "", "entry_step_id": "post", "steps": [
        {"id": "post", "name": "Post", "type": "tool", "forced_tool": "post_update", "output_key": "post",
         "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]}
    run = await _run(intent="update", existing_orch=existing, current_orchestration_id="orch_live")
    edit = {"kind": "update", "title": "Live", "summary": "s", "edits": [{"op": "add_step", "after": "post", "step": {
        "id": "note", "type": "llm", "name": "Note it", "does": "x", "prompt": "Note what was posted."}}]}
    out, _ = await _check(run, {"finish": "submit_plan", **edit})
    assert out["outcome"] == "review" and "build" in out["offers"], out.get("say")
    adds = {**edit, "edits": [{"op": "add_step", "after": "post", "step": {
        "id": "again", "type": "tool", "name": "Post again", "does": "x", "tool": "post_update"}}]}
    sent, _ = await _call("submit_plan", adds, run)
    assert "Post again" in sent["error"] and "Human step" in sent["error"]


def test_build_without_keeps_the_rest_of_a_branch():
    """SAI2's review, 5: `true → post → log` without `post` is `true → log`, not `true → end`; inner ones too."""
    from core.native_builder import objects as O

    steps = [{"id": "windy", "type": "if_else", "name": "Windy?", "does": "x", "condition": "state.check.wind > 40",
              "branches": {"true": "post", "false": "end"}},
             {"id": "post", "type": "tool", "name": "Post", "does": "x", "tool": "slack_post"},
             {"id": "log", "type": "llm", "name": "Log", "does": "x", "prompt": "p"}]
    plan = O.Plan.from_draft({"kind": "create", "title": "t", "summary": "s", "steps": steps,
                              "inner_workflows": [{"title": "I", "does": "i", "steps": steps}]})
    out = build_plan._without(plan, ["slack_post"])
    for got in (out.steps, out.inner_workflows[0].steps):
        by_id = {s.id: s for s in got}
        assert "post" not in by_id and by_id["windy"].branches == {"true": "log", "false": "end"}


async def test_only_the_person_lets_a_tool_act_without_asking(client):
    """SAI2's review, 11 (BWA1's decision 4): the planner can't move a tool out of "asks you first"; the person's
    press on the card does, read in code, and it holds while the tool is in the plan."""
    from core.store.resources import save_tool

    await save_tool({"name": "post_update", "description": "Posts an update outside.", "tool_type": "http",
                     "method": "POST", "url": "https://api.example.com/post", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}})
    run = await _run()
    ask = {"id": "ok", "type": "human", "name": "Approve", "does": "x", "prompt": "Post it?"}
    post = {"id": "post", "type": "tool", "name": "Post", "does": "x", "tool": "post_update"}
    draft = {"finish": "submit_plan", **_plan(CHECK, ask, post), "acts_without_asking": ["post_update"]}
    out, _ = await _check(run, draft)
    assert out["plan"]["approvals"]["asks_first"] == ["post_update"] and "alone:post_update" in out["offers"]
    run.shared_state["review_feedback"] = {"feedback": "let post_update act without asking"}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "alone", "alone": ["post_update"]}
    run.shared_state[build_plan.ANSWER_KEY] = answer
    moved, _ = await _check(run, draft)
    assert moved["plan"]["approvals"] == {"asks_first": [], "acts_without_asking": ["post_update"]}


def test_the_cards_let_it_act_words_are_what_the_answer_reads():
    import re
    from pathlib import Path

    plan_ts = (Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/plan.ts").read_text()
    template = re.search(r"export function aloneWords\(tool: string\): string \{\s*return `([^`]+)`;", plan_ts)
    assert template and build_plan._ALONE.match(template.group(1).replace("${tool}", "post_update"))


async def test_a_question_round_keeps_what_was_found_and_every_answer(client):
    """SAI2's review, 22: an ask dropped the lookup and the cards offered (so both ran again), and only the last
    answer reached the planner."""
    run = await _run()
    run.shared_state[build_plan.CHECKED_KEY] = {"object": "plan", "outcome": "ask", "question": "Which list?",
                                                "research": ["send_x"], "offered": ["notion"], "asked": [
                                                    {"question": "When?", "answer": "Mondays"}]}
    run.shared_state["human_response"] = {"answer": "Patients"}
    out, _ = await _check(run, {"finish": "ask_person", "question": "Who approves?", "about": "who"})
    assert out["research"] == ["send_x"] and out["offered"] == ["notion"]
    assert out["asked"] == [{"question": "When?", "answer": "Mondays"}, {"question": "Which list?",
                                                                         "answer": "Patients"}]


async def test_an_update_neither_doubles_a_schedule_nor_turns_into_a_create(client):
    """SAI2's review, 23."""
    from core.store import schedules

    existing = {"id": "orch_wind", "name": "Wind", "description": "", "entry_step_id": "check", "steps": [
        {"id": "check", "name": "Check the weather", "type": "tool", "forced_tool": "rotterdam_weather",
         "output_key": "check", "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]}
    await schedules.save({"id": "sch_wind", "name": "Wind", "target_type": "orchestration", "target_id": "orch_wind",
                          "schedule_type": "cron", "cron_expression": "0 8 * * *", "enabled": False, "prompt": "x"})
    run = await _run(intent="update", existing_orch=existing, current_orchestration_id="orch_wind")
    timed = {"kind": "update", "title": "Wind", "summary": "s", "schedule": {"cron": "0 9 * * *", "says": "at 9"},
             "edits": [{"op": "set_meta", "fields": {"summary": "s"}}]}
    assert "already runs on a schedule" in (await _call("submit_plan", timed, run))[0]["error"]
    assert "write kind update" in (await _call("submit_plan", _plan(CHECK), run))[0]["error"]
    unread = await _run(intent="update", current_orchestration_id="orch_gone")
    out, _ = await _check(unread, {"finish": "submit_plan", **_plan(CHECK)})
    assert out["outcome"] == "said" and "could not read the workflow" in out["say"]


async def test_an_updates_new_inner_workflow_is_held_whole_to_decision_4(client):
    """SAI2's re-check of blocker 1: the update's scope is the open workflow's; an inner workflow the update makes is
    new, so all its own."""
    from core.store.resources import save_tool

    await save_tool({"name": "post_update", "description": "Posts an update outside.", "tool_type": "http",
                     "method": "POST", "url": "https://api.example.com/post", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}})
    existing = {"id": "orch_live", "name": "Live", "description": "", "entry_step_id": "check", "steps": [
        {"id": "check", "name": "Check the weather", "type": "tool", "forced_tool": "rotterdam_weather",
         "output_key": "check", "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]}
    run = await _run(intent="update", existing_orch=existing, current_orchestration_id="orch_live")
    edit = {"kind": "update", "title": "Live", "summary": "s",
            "inner_workflows": [{"title": "Poster", "does": "posts", "steps": [
                {"id": "post", "type": "tool", "name": "Post", "does": "x", "tool": "post_update"}]}],
            "edits": [{"op": "add_step", "after": "check", "step": {
                "id": "run_poster", "type": "workflow", "name": "Run the poster", "does": "x", "workflow": "Poster"}}]}
    sent, _ = await _call("submit_plan", edit, run)
    assert "Post" in sent["error"] and "Human step" in sent["error"]


async def test_an_update_of_a_workflow_already_holding_a_key_like_string_is_still_possible(client):
    existing = {"id": "orch_live", "name": "Live", "description": "", "entry_step_id": "say", "steps": [
        {"id": "say", "name": "Say", "type": "llm", "prompt_template": "Mention sk_live_" + "Z" * 24 + " never.",
         "output_key": "say", "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]}
    run = await _run(intent="update", existing_orch=existing, current_orchestration_id="orch_live")
    edit = {"kind": "update", "title": "Live", "summary": "s", "edits": [{"op": "set_meta", "fields": {
        "title": "Live 2"}}]}
    out, _ = await _call("submit_plan", edit, run)
    assert out.get("finish") == "submit_plan", out


async def test_a_pasted_exports_removed_key_may_be_named_in_what_is_not_carried_over(client):
    from core.native_builder.export import CREDENTIAL_MARK

    run = await _run()
    out, _ = await _call("submit_plan", _plan(CHECK, not_carried_over=[f"The key it held ({CREDENTIAL_MARK})"]), run)
    assert out.get("finish") == "submit_plan", out


@pytest.mark.parametrize("prompt, fields", [
    ("Please connect your Xero account by providing the API key", []),
    ("Ready to sync?", [{"name": "token", "type": "string", "label": "Your Xero access token"}]),
])
async def test_a_human_step_never_asks_for_a_key_at_run_time(client, prompt, fields):
    """SAI2, 10-23 (gpt-oss on dev, chg-09): the plan put a Human step asking for the API key before the fetch; the
    answer would be kept in the run. ask_person's rule, held over a step's prompt and fields."""
    run = await _run()
    ask = {"id": "key", "type": "human", "name": "Key", "does": "x", "prompt": prompt,
           **({"config": {"human_fields": fields}} if fields else {})}
    sent, _ = await _call("submit_plan", _plan(ask, CHECK), run)
    assert "never asked for a key" in sent["error"]


def test_a_rename_only_update_makes_nothing_but_the_rename():
    """SAI2, 10-23 (Nova on dev, effect-03): a rename made an agent "Rename Orchestration" on the old route."""
    from core.native_builder import objects as O
    from core.native_builder.build_save import save_calls

    existing = {"id": "orch_nd", "name": "Nightly digest", "description": "", "entry_step_id": "a", "steps": [
        {"id": "a", "name": "A", "type": "llm", "prompt_template": "p", "output_key": "a", "next_step_id": "done"},
        {"id": "done", "name": "End", "type": "end"}]}
    from core.native_builder.compile import apply_edits, plan_from_workflow

    rename = O.Edit(op="set_meta", fields={"title": "Daily summary"})
    plan, problems = apply_edits(plan_from_workflow(existing), [rename])
    assert problems == []
    calls = save_calls(plan, {}, {"intent": "update", "existing_orch": existing})
    assert [(c["tool"], c["args"].get("name")) for c in calls] == [("update_orchestration", "Daily summary")]
