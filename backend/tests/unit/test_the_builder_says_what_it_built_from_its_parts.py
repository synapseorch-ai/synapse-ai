"""The builder says what a build does from its parts, in code, never from the planner's words (F1228, GUIDE1).

CAP3's stage 1 (10-29) caught the builder promising more than it built: brief 4's card said it "posts the results as
a chart card" on a board that holds tasks and notes; brief 2's answer said the workflow it had just built "can look up
order status" from agents with no tools; brief 5's question said an existing workflow "does exactly what you
described" when its browser agent had no kept sign-in. Those three plans are rebuilt here as the planner wrote them.

The rule (Naveen 10-29): say what each part does, name what is missing or off and where it is put right, and never
claim a result. The planner's summary stays on the card as "You asked for"; an agent step's own words are what it is
asked to do. BLD1's parts (F1223–F1225) are read where a plan has them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.native_builder import objects as O
from core.native_builder import parts_words as PW
from core.native_builder.render import render

NB = Path(__file__).resolve().parents[2] / "core" / "native_builder"

TITLES = {"vault_read": PW.title_of("vault_read", "Read a file from the vault by its path."),
          "board_add": PW.title_of("board_add")}


def _plan(raw: dict, **code) -> O.Plan:
    plan = O.Plan.from_draft(raw)
    for key, value in code.items():
        setattr(plan, key, value)
    return plan


def _said(plan: O.Plan, agents: dict | None = None) -> O.Plan:
    plan.step_words, plan.part_words, plan.off = PW.describe(plan, titles=TITLES, agents=agents)
    return plan


#: Brief 4 as stage 1's planner wrote it: a sales CSV read, summed, and posted to a board "as a chart card".
SALES = {"kind": "create", "title": "Sales Analysis by Region", "summary": (
    "Reads the sales CSV from the vault, analyzes sales by region for last week, and posts the results to a board "
    "as a chart card showing regional sales as bars."), "steps": [
    {"id": "read_sales_csv", "type": "tool", "name": "Read Sales CSV", "does": "Reads the sales data CSV file.",
     "tool": "vault_read", "config": {"tool_arguments": {"path": "sales.csv"}}},
    {"id": "analyze", "type": "transform", "name": "Analyze Sales by Region",
     "does": "Processes the CSV data to calculate sales by region for the past 7 days.",
     "config": {"transform_code": "result = {}"}},
    {"id": "post", "type": "agent", "name": "Post Sales Analysis to Board", "agent": "agent_poster",
     "does": "Posts the sales analysis results to the board as a chart card with regions on the axis."}],
    "reuse_agents": ["agent_poster"], "boards": [{"name": "Sales Analysis"}]}
NO_SESSION = ("No kept session for portal.localhost yet: sign in to it under Agents › Browser › Sessions, then pick it "
              "in this agent's Browser settings. It is built without it until then")
MISSING_FILE = ("`help/b.md` is not in the Vault: add it there, then to “Help docs” on the Knowledge screen. It is built "
                "without it")
POSTER = {"agent_poster": {"id": "agent_poster", "name": "sales_board_poster", "tools": ["board_add"]}}

#: Brief 2: help docs and an orders API, built as two agents with no tools at all.
SUPPORT = {"kind": "create", "title": "Customer Support Assistant", "summary": (
    "Answers customer questions from the help docs and looks up an order's status in the orders API."), "steps": [
    {"id": "answer", "type": "agent", "name": "Answer from Help Docs", "agent": "help_answerer",
     "does": "Answers the question from the help docs."},
    {"id": "order", "type": "agent", "name": "Look Up Order", "agent": "order_assistant",
     "does": "Looks up the order's status in the orders API."}],
    "agents": [{"name": "help_answerer", "purpose": "Answers from the help docs."},
               {"name": "order_assistant", "purpose": "Looks up orders."}]}

#: Brief 5: a portal order checked by a browser agent, you approve, then the refund form.
REFUND = {"kind": "create", "title": "Refund Request Handler", "summary": "Checks the order, asks you, files the "
          "refund.", "steps": [
    {"id": "check", "type": "agent", "name": "Check Order in Portal", "agent": "agent_portal",
     "does": "Checks the order in the portal."},
    {"id": "approve", "type": "human", "name": "Approve Refund", "does": "You approve.",
     "prompt": "Approve this refund?"},
    {"id": "file", "type": "agent", "name": "Fill Refund Form", "agent": "agent_portal",
     "does": "Fills in the refund form."}], "reuse_agents": ["agent_portal"]}


class TestBrief4TheBoardSaysTheCardsItHolds:
    def test_the_board_and_its_poster_say_tasks_and_notes_and_nothing_says_a_chart(self):
        plan = _said(_plan(SALES), POSTER)
        assert plan.part_words == ['the board "Sales Analysis" (made with it): it holds tasks and notes, '
                                   "nothing else"]
        assert plan.step_words["post"] == ('done by the agent "sales_board_poster", with tools: adds cards to a '
                                           "board, as tasks and notes")
        assert plan.step_words["read_sales_csv"] == "read a file from the vault by its path"
        said = " ".join([*plan.step_words.values(), *plan.part_words, *plan.off])
        assert "chart" not in said and "vault_read" not in said, "code's words: no promise, no raw id"

    def test_the_card_keeps_the_planners_words_only_as_the_request_and_the_ask(self):
        text = render(_said(_plan(SALES), POSTER))
        assert "## Overview" not in text
        assert "## You asked for\nReads the sales CSV" in text
        assert ('3. **Post Sales Analysis to Board** (agent): done by the agent "sales_board_poster", with tools: adds '
                'cards to a board, as tasks and notes — asks it to: "Posts the sales analysis results to the board '
                'as a chart card with regions on the axis."') in text
        # A tool step's own words are its tool's: the planner's line for it is not said at all.
        assert "1. **Read Sales CSV** (tool): read a file from the vault by its path — uses `vault_read`" in text
        assert "Reads the sales data CSV file" not in text

    def test_the_last_message_says_each_part_and_never_a_result(self):
        text = PW.summary(_said(_plan(SALES), POSTER))
        assert text.startswith("\n\n**What it does**\n- **Read Sales CSV**: read a file from the vault by its path")
        assert '- The board "Sales Analysis" (made with it): it holds tasks and notes, nothing else' in text
        assert "chart" not in text and "Missing or off" not in text


class TestBrief2AnAgentWithNoToolsSaysSo:
    def test_each_says_it_fetches_nothing(self):
        plan = _said(_plan(SUPPORT))
        assert plan.step_words["order"] == 'done by the agent "order_assistant", with no tools'
        assert plan.off == [
            '"help_answerer" has no tools: it works only from what the run gives it, and fetches or changes nothing',
            '"order_assistant" has no tools: it works only from what the run gives it, and fetches or changes '
            "nothing"]
        text = PW.summary(plan)
        assert '**Missing or off**\n- "help_answerer" has no tools' in text
        assert "can look up" not in text and "orders API" not in text, "nothing says what it cannot do"

    def test_a_missing_tool_and_a_connection_are_said_too(self):
        plan = _said(_plan(SUPPORT, missing=["orders_lookup"], connections_needed=["Orders API"]))
        text = PW.summary(plan)
        assert "- `orders_lookup`: nothing here provides it yet" in text and "- Orders API: connect it first" in text
        assert "## Missing here\n- `orders_lookup`: nothing here provides it yet\n- Orders API: connect it first\n" \
               '- "help_answerer" has no tools' in render(plan)


class TestBrief5ASiteWithNoKeptSignInSaysWhereToSignIn:
    def test_a_saved_browser_agent_says_its_sites(self):
        portal = {"agent_portal": {"id": "agent_portal", "name": "portal_agent", "tools": ["browser_navigate"],
                                   "browser": {"sites": ["portal.localhost"], "sessions": []}}}
        plan = _said(_plan(REFUND), portal)
        assert plan.step_words["check"] == ('done by the agent "portal_agent", with tools: browser navigate; Browser: '
                                            "portal.localhost; reads only")
        assert plan.step_words["approve"] == 'stops and asks you: "Approve this refund?"'

    def test_bld1s_kept_session_mark_says_where_to_sign_in(self):
        # F1223's NewAgent: `browser` with `sessions_missing`, filled by code.
        agent = {"name": "portal_agent", "type": "browser", "tools": [],
                 "browser": {"sites": ["portal.localhost"], "sessions": ["portal.localhost"],
                             "sessions_missing": ["portal.localhost"], "mode": "co_drive"}}
        plan = _said(_plan(REFUND), {"agent_portal": agent})
        # BLD1's own line (render.missing_session_line), the agent named before it: off the card, "this agent"
        # needs its name.
        assert plan.off == [f'"portal_agent": {NO_SESSION}']
        assert plan.step_words["check"] == ('done by the agent "portal_agent", Browser: portal.localhost; types and '
                                            "sends with you watching")
        assert "No kept session" not in plan.step_words["check"], "said once, as missing or off"

    def test_the_card_says_a_missing_session_once(self):
        new = {"name": "portal_agent", "purpose": "checks the portal", "type": "browser",
               "browser": {"sites": ["portal.localhost"], "sessions": ["portal.localhost"],
                           "sessions_missing": ["portal.localhost"]}}
        plan = _said(_plan({**REFUND, "agents": [*REFUND.get("agents", []), new]}))
        assert f'"portal_agent": {NO_SESSION}' in plan.off
        assert render(plan).count(NO_SESSION) == 1


class TestBld1sPartsAreRows:
    def test_an_agents_switches_are_said(self):
        a = PW.agent_parts({"name": "Clerk", "tools": ["vault_read"], "memory": True, "knowledge": ["kb_1"],
                            "servers": ["Tracker"]}, kb_names={"kb_1": "Help docs"})
        # BLD1's own lines (render.switch_lines, F1223), the card's and these words alike.
        assert PW.agent_words(a, TITLES) == (
            'the agent "Clerk", with tools: read a file from the vault by its path; Memory: on — it keeps what it '
            "learns across runs and conversations; Knowledge: answers from “Help docs”; MCP server Tracker: its tools "
            "as of now; tools it adds later are not added")

    def test_the_plans_knowledge_apis_and_apps_are_parts_and_what_is_off(self):
        plan = O.Plan.model_validate({**_plan(SUPPORT).model_dump(), "knowledge": [
            {"name": "Help docs", "purpose": "answers", "files": ["help/a.md", "help/b.md"],
             "files_missing": ["help/b.md"]}],
            "apis": [{"name": "Orders", "spec": "http://x/openapi.json", "tools": ["get_order"]},
                     {"name": "Billing", "spec": "billing.yaml", "problem": "its spec did not read as OpenAPI"}],
            "mcp_servers": [{"name": "Tracker", "url": "https://t.example/mcp", "connected": False}]})
        _said(plan)
        # BLD1's own lines (render.knowledge_lines, F1224; render.own_part_lines' helpers, F1225).
        assert plan.part_words == ["“Help docs”: made with it, in the Vault folder knowledge/help-docs; it is given "
                                   "`help/a.md`",
                                   "“Orders”, from the spec at `http://x/openapi.json`: made with it as `get_order`"]
        assert plan.off[-3:] == [MISSING_FILE, "“Billing”: its spec did not read as OpenAPI",
                                 "MCP server “Tracker” at https://t.example/mcp is not connected yet: connect it on its "
                                 "card above, and I'll plan on its tools"]
        card = render(plan)
        for line in [*plan.part_words[1:], *plan.off[-2:]]:
            assert card.count(line) == 1, line

    def test_the_card_says_a_knowledge_base_and_its_missing_file_once(self):
        plan = _said(O.Plan.model_validate({**_plan(SUPPORT).model_dump(), "knowledge": [
            {"name": "Help docs", "purpose": "answers", "files": ["help/a.md", "help/b.md"],
             "files_missing": ["help/b.md"]}]}))
        assert MISSING_FILE in plan.off
        card = render(plan)
        assert card.count(MISSING_FILE) == 1
        assert card.count("“Help docs”: made with it") == 1

    def test_a_plan_without_them_says_none(self):
        plan = _said(_plan(SALES), POSTER)
        assert not any("knowledge base" in w or "API" in w or "Vault" in w or "Knowledge:" in w
                       for w in [*plan.part_words, *plan.off])


class TestEveryPartHasItsRow:
    def test_every_step_type_has_words(self):
        assert set(O.STEP_TYPES) - {"end"} <= set(PW._STEP), "a new step type needs its row in `_STEP`"

    def test_a_browser_step_says_it_replays_a_recording(self):
        step = O.Step(id="b", type="browser", name="File Refund", does="Files it.",
                      config={"recording_id": "rec_1", "browser_sessions": ["portal.localhost"]})
        assert PW._STEP["browser"](step, None) == ("replays a browser recording made in the app, signed in to "
                                                   "portal.localhost; a page that changed waits for you")

    def test_a_board_holds_what_its_tools_say(self):
        from core.boards.tools import _SCHEMAS

        assert PW.board_kinds() == _SCHEMAS["board_add"][1]["properties"]["kind"]["enum"] == ["task", "note"]

    def test_a_tool_is_said_by_its_own_description_never_invented(self):
        assert PW.title_of("send_mail", "Send an email. It goes from your account.") == "send an email"
        assert PW.title_of("weird_tool", "") == "weird tool"
        assert PW.title_of("long_tool", "x" * 120) == "long tool"

    def test_a_schedule_is_made_paused_and_says_where_to_turn_it_on(self):
        plan = _said(_plan({**SALES, "schedule": {"cron": "0 7 * * *", "says": "every day at 7:00"}}), POSTER)
        assert "runs every day at 7:00, by its schedule" in plan.part_words
        assert plan.off == ["its schedule is made paused: turn it on under Schedules"]


class TestTheLastWordAndLaterTurnsReadTheParts:
    def test_the_build_prints_its_parts_after_what_it_wrote(self):
        steps = {s["id"]: s for s in json.loads((NB / "orchestration.json").read_text("utf-8"))["steps"]}
        assert steps["step_build_done"]["print_content"] == (
            "{state.run_changes}{state.build_saved.effect}{state.build_saved.does}")

    def test_a_stopped_build_says_no_parts(self):
        from core.native_builder.build_save import _stopped

        assert _stopped([], "Nothing was built: x.")["does"] == ""

    def test_the_answer_pack_carries_the_workflows_words_and_the_rule(self):
        from core.native_builder.answer_pack import WORKFLOW_RULE, build_pack
        from core.product_tools.roles import Role

        pack = build_pack(routed={"needs": "help"}, workspace_name="Acme", profile="", asker_name="",
                          asker_role=Role.OWNER, last="Built it.", capabilities="", help_hits=[], named=[],
                          workflows=['The workflow "Support" (orch_1), step by step:\n1. Look Up Order: ...'])
        assert WORKFLOW_RULE in pack and 'The workflow "Support" (orch_1)' in pack
        assert "never that it does exactly what was asked" in WORKFLOW_RULE

    def test_later_turns_know_what_the_last_turns_built(self):
        from core.native_builder.read_turn import built_in

        earlier = [("run_3", {"_run_writes": [{"kind": "schedule", "action": "created", "id": "s1"}]}),
                   ("run_2", {"_run_writes": [{"kind": "orchestration", "action": "created", "id": "orch_b"},
                                              {"kind": "agent", "action": "created", "id": "a1"}]}),
                   ("run_1", {"_run_writes": [{"kind": "orchestration", "action": "updated", "id": "orch_a"},
                                              {"kind": "orchestration", "action": "deleted", "id": "orch_x"},
                                              {"kind": "orchestration", "action": "created", "id": "orch_b"}]})]
        assert built_in(earlier) == ["orch_b", "orch_a"]

    def test_the_change_planner_never_says_what_a_workflow_does(self):
        prompt = json.loads((NB / "agents" / "change_planner.json").read_text("utf-8"))["system_prompt"]
        assert "Never say what an existing workflow does" in prompt
        assert "that it does what they described" in prompt


@pytest.mark.asyncio
async def test_fill_writes_the_words_from_the_hosts_own_catalogue(monkeypatch):
    """`fill` reads tool titles through the host's catalogue (cloud's, there), and a failure never stops the check."""
    from core import builder_tools

    class _Tool:
        def __init__(self, name, description):
            self.name, self.description = name, description

    async def catalogue(_server):
        return [_Tool("vault_read", "Read a file from the vault by its path.")]

    monkeypatch.setattr(builder_tools, "_workspace_catalogue", catalogue)
    assert (await PW.tool_titles(None))["vault_read"] == "read a file from the vault by its path"

    async def broken(_server):
        raise RuntimeError("no catalogue")

    monkeypatch.setattr(builder_tools, "_workspace_catalogue", broken)
    assert await PW.tool_titles(None) == {}


def test_the_panel_draws_the_same_words():
    """The panel's card is drawn from the Plan object (`plan.ts`, synced into cloud's web): each step's words, the
    plan's parts, and what is missing or off, under the labels Naveen chose (10-29); the planner's line is the
    request. A plan from before them, and its text, read as they did."""
    root = Path(__file__).resolve().parents[3] / "frontend/src/components/assistant"
    plan_ts, cards = (root / "plan.ts").read_text("utf-8"), (root / "cards.tsx").read_text("utf-8")
    assert "view.plan.step_words?.[s.id]" in plan_ts and "asks it to: " in plan_ts
    assert "const ASKED = new Set(['agent', 'llm', 'evaluator']);" in plan_ts  # render.py's `_ASKED`
    from core.native_builder.render import _ASKED

    assert _ASKED == {"agent", "llm", "evaluator"}
    assert "parts: bullets(view.plan.part_words)," in plan_ts and "off: bullets(view.plan.off)," in plan_ts
    assert "/^(?:overview|you asked for)\\b/" in plan_ts and "/^(?:missing here|missing or off)\\b/" in plan_ts
    for label in (">You asked for</span>", ">What it will do</span>", "Missing or off\n"):
        assert label in cards, label
    assert "{plan.off && <Markdown" in cards and "{plan.parts && <Markdown" in cards
