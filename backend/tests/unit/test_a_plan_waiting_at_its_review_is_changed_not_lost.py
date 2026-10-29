"""A plan waiting at its review is changed by the message that continues it, never lost or remade (F1241, F1242;
TRI1's triage of Naveen's prod chat, 10-29, classes 1 and 2).

Naveen typed "lets make it automatically post in twitter too" at a waiting plan. The panel sets a waiting plan aside
when anything but its yes is typed (F759), and the next turn started from the chat alone: F1226's carry held the
request's questions, never the plan. Classify called it a *change*; the change route refused `create` of a workflow
("makes a new agent, skill, tool…"), the model sent it again as an agent, the review said "Make the agent …", and the
maker called create_agent twice in one reply, of which the engine made the first and said nothing of the second.

* The plan comes with the carry (`WAITING_PLAN`), and when classify says the message continues the request it goes
  back to the plan's own review with the message as the person's reply, as the card's Change something does. Only
  on the model's own `continues=True`: unsaid, the run starts afresh and says the plan still waits, and parks it for
  the turn after; a wrong carry would change a waiting plan.
* The change route hands a new workflow to the build; a new workflow sent again as another kind is sent back.
* A reply's calls after its finish tool are recorded as not made, and the maker's check says each.
"""
from __future__ import annotations

import json
import types

import pytest

from core import builder_tools  # noqa: F401 — the builder's tool modules load in their own order first
from core.native_builder import change_tools as T
from core.native_builder import build_plan as BP
from core.native_builder.build_plan import CHECKED_KEY, DRAFT_KEY
from core.orchestration.run_writes import WRITES_KEY
from tests.unit.test_the_change_route_decides_in_code import _call, _run

CARD = {"title": "SynapseOrch curiosity post", "summary": "Scrape synapseorch.com and write a post.", "steps": []}
CHECKED = {"object": "plan_checked", "outcome": "review", "plan": CARD, "draft": "d1", "say": "",
           "asked": [{"question": "Which site?", "answer": "synapseorch.com"}]}
PLAN = {DRAFT_KEY: {"finish": "submit_plan", "title": CARD["title"]}, CHECKED_KEY: json.dumps(CHECKED),
        "intent": "create", "current_orchestration_id": ""}
AT_REVIEW = [{"step_id": "step_plan_route", "status": "completed"}, {"step_id": "step_human_review", "status": "paused"}]
TWITTER = "lets make it automatically post in twitter too"


def _staged(previous=None, history=AT_REVIEW) -> dict:
    return {T.CARRY_KEY: T.carry_request(PLAN if previous is None else previous, history, "builder_turn1")}


# ── the carry ────────────────────────────────────────────────────────────────────────────────────────────────────
class TestTheWaitingPlanIsCarried:
    def test_a_run_that_stopped_at_its_review_stages_its_plan(self):
        plan = T.carry_request(PLAN, AT_REVIEW, "builder_turn1")[T.WAITING_PLAN]
        assert plan["run_id"] == "builder_turn1"
        assert plan["state"][CHECKED_KEY] == PLAN[CHECKED_KEY] and plan["state"][DRAFT_KEY] == PLAN[DRAFT_KEY]

    def test_continues_puts_the_plan_back_as_the_reply_to_its_review(self):
        state = {"user_message": TWITTER, **_staged()}
        assert T.adopt_carry(state, True) is True
        assert state[CHECKED_KEY] == PLAN[CHECKED_KEY] and state[DRAFT_KEY] == PLAN[DRAFT_KEY]
        assert state["review_feedback"] == {"feedback": TWITTER}
        assert state[BP.PLAN_BEFORE_KEY] == {"plan": CARD, "draft": "d1", "run_id": "builder_turn1"}

    @pytest.mark.parametrize("continues", [None, False])
    def test_not_continued_it_is_parked_for_the_turn_after(self, continues):
        state = {"user_message": "what's on my board?", **_staged()}
        assert T.adopt_carry(state, continues) is False
        assert CHECKED_KEY not in state and "review_feedback" not in state
        later = T.carry_request(state, [{"step_id": "step_answer", "status": "completed"}], "builder_turn2")
        assert later[T.WAITING_PLAN]["run_id"] == "builder_turn1", "the plan is still there to come back to"

    def test_a_run_that_saved_carries_nothing(self):
        """F1226's rule holds: a saved build ends the request."""
        assert T.carry_request({**PLAN, WRITES_KEY: [{"kind": "orchestration", "id": "o1"}]}, AT_REVIEW) == {}

    def test_a_run_that_ended_elsewhere_carries_no_plan(self):
        assert T.WAITING_PLAN not in T.carry_request(PLAN, [{"step_id": "step_plan_said", "status": "completed"}])

    def test_the_router_has_the_review_as_a_route(self):
        from pathlib import Path

        orch = json.loads((Path(T.__file__).parent / "orchestration.json").read_text())
        route = next(s for s in orch["steps"] if s["id"] == "step_classify_route")
        assert route["switch_cases"][T.REVISE_ROUTE] == "step_review_research"


# ── classify ─────────────────────────────────────────────────────────────────────────────────────────────────────
async def _classify(run_id: str, request: dict, staged: dict | None = None):
    run = await _run(run_id=run_id, user_message=TWITTER, may_change=True, may_act=True,
                     **(_staged() if staged is None else staged))
    out, events = await _call("classify_request", request, run)
    return out, [e.get("message") for e in events if e.get("type") == "builder_notice"], run


class TestClassify:
    async def test_naveen_s_turn_two_changes_the_waiting_plan(self, client):
        """The model said `change` and that it continues: the plan's review takes it, not the change route."""
        out, notices, run = await _classify("builder_f1241_a", {"kind": "change", "continues": True})
        assert (out["kind"], out["route"], out["overridden_by"]) == ("build", T.REVISE_ROUTE, "waiting_plan")
        assert run.shared_state["review_feedback"] == {"feedback": TWITTER}
        assert run.shared_state[CHECKED_KEY] == PLAN[CHECKED_KEY] and T.TAKEN_AS_A_CHANGE in notices

    async def test_unsaid_starts_afresh_and_says_the_plan_waits(self, client):
        out, notices, run = await _classify("builder_f1241_b", {"kind": "build"})
        assert out["route"] != T.REVISE_ROUTE and CHECKED_KEY not in run.shared_state
        assert T.STILL_WAITING in notices and run.shared_state[T.PARKED_PLAN_KEY]["run_id"] == "builder_turn1"

    async def test_a_new_request_starts_afresh_and_says_nothing(self, client):
        """ASSIST1's case (F759): unrelated requests typed at a waiting plan are never folded into it."""
        out, notices, run = await _classify("builder_f1241_c", {"kind": "build", "continues": False})
        assert out["route"] != T.REVISE_ROUTE and CHECKED_KEY not in run.shared_state and not notices

    async def test_a_question_at_a_waiting_plan_is_answered(self, client):
        out, notices, _ = await _classify("builder_f1241_d", {"kind": "answer", "continues": True})
        assert out["route"] == "step_answer" and T.TAKEN_AS_A_CHANGE not in notices

    async def test_no_plan_waiting_routes_as_before(self, client):
        out, notices, _ = await _classify("builder_f1241_e", {"kind": "build", "continues": True},
                                          staged=_staged(history=[{"step_id": "step_build_done"}]))
        assert out["route"] == "step_plan_room" and not notices


# ── the change route ─────────────────────────────────────────────────────────────────────────────────────────────
NEW_WORKFLOW = {"op": "create", "target": {"kind": "workflow", "name": "SynapseOrch Curiosity Post Generator"},
                "fields": {"what": "a workflow that writes the post and posts it on Twitter"}}


class TestTheChangeRouteHandsANewWorkflowToTheBuild:
    async def test_a_lone_new_workflow_goes_to_the_build(self, client):
        run = await _run(run_id="builder_f1242_a", intent="manage")
        out, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": [NEW_WORKFLOW]}}, run)
        assert out["outcome"] == "steps", out
        assert run.shared_state["intent"] == "create" and run.shared_state["current_orchestration_id"] == ""

    async def test_with_other_ops_it_is_said_as_its_own_request(self, client):
        run = await _run(run_id="builder_f1242_b", intent="manage")
        rename = {"op": "rename_agent", "target": {"kind": "agent", "name": "Front desk"}, "fields": {"name": "Desk"}}
        out, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": [NEW_WORKFLOW, rename]}},
                             run)
        assert out["outcome"] == "said" and "build of its own" in out["say"]

    async def test_a_new_workflow_sent_again_as_an_agent_is_sent_back(self, client):
        run = await _run(run_id="builder_f1242_c")
        first, _ = await _call("submit_change", {"ops": [NEW_WORKFLOW]}, run)
        assert first.get("finish") == "submit_change", first
        as_agent = {**NEW_WORKFLOW, "target": {**NEW_WORKFLOW["target"], "kind": "agent"}}
        again, _ = await _call("submit_change", {"ops": [as_agent]}, run)
        assert "a new workflow is a build" in again.get("error", ""), again

    async def test_another_kind_may_be_corrected(self, client):
        run = await _run(run_id="builder_f1242_d")
        tool = {"op": "create", "target": {"kind": "tool", "name": "Poster"}, "fields": {"what": "posts"}}
        await _call("submit_change", {"ops": [tool]}, run)
        out, _ = await _call("submit_change", {"ops": [{**tool, "target": {"kind": "python_tool", "name": "Poster"}}]},
                             run)
        assert out.get("finish") == "submit_change", out


# ── what was not made ────────────────────────────────────────────────────────────────────────────────────────────
class TestWhatWasNotMadeIsSaid:
    async def test_a_reply_s_calls_after_its_finish_are_recorded(self, fake_llm):
        from _fakes import seed as S
        from core.react_engine import run_agent_step

        make = {"type": "function", "function": {"name": "create_agent", "description": "c", "parameters": {
            "type": "object", "properties": {"name": {"type": "string"}}}}}

        async def tools(name, args):
            return json.dumps({"id": "agent_1", "name": args.get("name")})
        fake_llm.script([json.dumps({"tool": "create_agent", "arguments": {"name": "Product Feature Picker"}}) + "\n"
                         + json.dumps({"tool": "create_agent", "arguments": {"name": "Curiosity Post Writer"}})])
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        events = [e async for e in run_agent_step(
            message="Make it now, with one call.", agent_id=agent["id"], session_id="s_f1242",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, tools_override=[make], tool_executor=tools, max_turns=3,
            source="orchestration_step", finish_tools=["create_agent"])]
        final = next(e for e in reversed(events) if e.get("type") == "final")
        assert final["finish"]["not_made"] == [{"tool": "create_agent", "arguments": {"name": "Curiosity Post Writer"}}]

    async def test_check_made_says_the_second_and_the_review_s_unmade_ops(self, client):
        from core.orchestration.steps import NOT_MADE_PREFIX

        run = await _run(run_id="builder_f1242_e")
        run.shared_state["change_checked"] = json.dumps({"object": "change", "outcome": "create", "ops": [
            {"op": "create", "kind": "create", "status": "ready", "target": {"kind": "agent", "name": "Picker"}},
            {"op": "rename_agent", "status": "refused", "say": "No agent is called Front desk."},
            {"op": "set_agent_tools", "status": "left_out", "say": ""}]})
        run.shared_state[WRITES_KEY] = [{"kind": "agent", "action": "created", "id": "agent_1", "name": "Picker"}]
        run.shared_state[f"{NOT_MADE_PREFIX}step_make_new"] = [
            {"tool": "create_agent", "arguments": {"name": "Curiosity Post Writer"}},
            {"tool": "read_workspace", "arguments": {"topic": "agents"}}]
        got, _ = await _call("check_made", {}, run)
        assert got["note"] == (" It didn't also make the agent \"Curiosity Post Writer\": one thing is made for each "
                               "yes. Ask for it next. No agent is called Front desk.")

    def test_the_record_is_the_engine_s(self):
        from core.events_public import public_state

        assert public_state({"_not_made_step_make_new": [1], "_change_kinds": {}, "x": 1}) == {"x": 1}
