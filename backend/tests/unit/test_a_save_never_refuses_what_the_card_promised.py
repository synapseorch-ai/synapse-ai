"""A save never refuses what the card promised, a refusal after the yes plans again once, and a stopped save's plan
is never lost (F1375; REACH1's read of CAP3's FF18 prod-13, eb 11-04).

prod-13: the card listed the new agent's tools under "Asks you first" and the person said yes. The tools are this
build's own to make (F944), so at the yes there were none to decide a gate for; once made, the save read their asks
as new ("now asks you before …") and stopped. That refusal never went back to the planner (F1295 covered a failed
check only), and since the run had made the tools, the next turn carried nothing: "Ask again to see the plan" found
no plan. These pin the class:

- A: an ask the card promised (a confirm, for a tool this build makes or connects after the yes, listed under "Asks
  you first") is what was approved; a block, or an ask the card did not list, still stops the save (F964);
- B: every refusal after the yes with nothing of the workflow saved (an ask or a model moved, a tool gone, a failed
  check) goes back to the planner once, in the save's own words; a second stops and says so;
- C: a save that stopped having made only its tools leaves the request open: its plan comes with the next turn, to
  be shown or changed. A run that saved its workflow still carries nothing.
"""
from __future__ import annotations

from core.native_builder import build_plan, build_save
from core.native_builder import change_tools as T
from core.orchestration.run_writes import WRITES_KEY
from test_a_build_makes_the_tools_it_found import ASK, FINDINGS, _call, _tool, workspace  # noqa: F401

TOOL = "mailchimp_send_campaign"
SENDER = {"name": "Sender", "purpose": "Sends the newsletter", "tools": [TOOL]}
SEND = {"id": "send", "type": "agent", "name": "Send it", "does": "Sends the newsletter", "agent": "Sender",
        "prompt": "Send the newsletter."}
#: A tool the workspace already has, which the plan reuses: there at the yes, so it can be gone after it.
WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}
CHECK = {"id": "check", "type": "tool", "name": "Check the weather", "does": "Reads today's weather",
         "tool": "rotterdam_weather"}


async def _approved(*steps, **state):
    """A plan whose new agent holds a tool this build makes, put to the person and approved, as prod-13's was."""
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_promised", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: FINDINGS, **state})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Newsletter", "summary": "Sends the newsletter.",
             "steps": [*steps, ASK, SEND], "agents": [SENDER]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    if checked["outcome"] == "prompts":
        written, _ = await _call("submit_prompts", {"prompts": [{"agent": "Sender",
                                                                 "instructions": "Send the newsletter."}]}, run)
        run.shared_state[build_plan.PROMPTS_KEY] = written
        checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    assert "build" in checked["offers"], checked["say"]
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    made, _ = await _call("save_build", {"only": "tools"}, run)
    assert made["outcome"] == "tools_made", made
    return run, checked


async def _workspace_policy(decision: str) -> None:
    from core.routes.settings import save_settings

    await save_settings({"tool_policy_defaults": {f"tool:{TOOL}": decision}})


class TestWhatTheCardPromisedIsApproved:
    async def test_an_ask_it_listed_for_a_tool_this_build_made_saves(self, client):
        run, checked = await _approved()
        assert TOOL in checked["plan"]["approvals"]["asks_first"], "the card says it asks first"
        assert not [g for g in checked["plan"]["gates"] if g["tool"] == TOOL], "no gate could be decided at the yes"
        saved, _ = await _call("save_build", {}, run)
        assert saved["outcome"] == "saved", saved

    async def test_an_ask_it_did_not_list_is_still_refused(self, client):
        run, checked = await _approved(**{build_plan.ANSWER_KEY: {"outcome": "alone", "alone": [TOOL]}})
        assert TOOL in checked["plan"]["approvals"]["acts_without_asking"]
        await _workspace_policy("confirm")
        refused, _ = await _call("save_build", {}, run)
        assert refused["outcome"] == "replan" and "Sender and `mailchimp_send_campaign`" in refused["refused"], refused

    async def test_a_block_is_still_refused(self, client):
        run, _ = await _approved()
        await _workspace_policy("deny")
        refused, _ = await _call("save_build", {}, run)
        assert refused["outcome"] == "replan" and "Sender and `mailchimp_send_campaign`" in refused["refused"], refused


class TestARefusalAfterTheYesPlansAgainOnce:
    async def test_a_moved_ask_goes_back_to_the_planner_in_the_saves_words(self, client):
        from core.store.resources import load_orchestrations

        run, _ = await _approved()
        await _workspace_policy("deny")
        first, _ = await _call("save_build", {}, run)
        assert first["outcome"] == "replan", first
        assert first["refused"].startswith("what the run asks you for changed since your yes — "), first
        assert run.shared_state[build_save.PLAN_ANSWER_KEY] == {"outcome": "replan", "refused": first["refused"]}
        assert await _tool() is not None, "the tools it made stay"
        assert not [o for o in await load_orchestrations() if o.get("name") == "Newsletter"]

    async def test_a_tool_gone_goes_back_to_the_planner(self, client):
        from core.store.resources import delete_tool_named, save_tool

        await save_tool(dict(WEATHER))
        run, _ = await _approved(CHECK)
        await delete_tool_named("rotterdam_weather")
        first, _ = await _call("save_build", {}, run)
        assert first["outcome"] == "replan", first
        assert first["refused"].startswith("this plan uses `rotterdam_weather`, which nothing in this workspace "
                                           "provides now"), first

    async def test_a_second_stops_and_says_so(self, client):
        run, _ = await _approved()
        await _workspace_policy("deny")
        await _call("save_build", {}, run)
        again, _ = await _call("save_build", {}, run)
        assert again["outcome"] == "stopped", again
        assert again["reason"].startswith("Nothing was built: what the run asks you for changed since your yes — ")
        assert "I planned it again once already. The tools I made stay" in again["reason"]


class TestAStoppedSavesPlanIsNeverLost:
    async def test_its_plan_comes_with_the_next_turn_to_be_shown(self, client):
        run, _ = await _approved()
        await _workspace_policy("deny")
        await _call("save_build", {}, run)
        stopped, _ = await _call("save_build", {}, run)
        assert stopped["outcome"] == "stopped" and run.shared_state.get(WRITES_KEY), "it made its tools, nothing else"
        previous = {**run.shared_state, "build_saved": stopped}
        staged = {T.CARRY_KEY: T.carry_request(previous, [{"step_id": "step_build_done", "status": "completed"}],
                                               run.run_id), "user_message": "Can you show me the updated plan?"}
        assert T.waiting_plan_title(staged) == "Newsletter", "the next turn is told a plan waits"
        assert T.adopt_carry(staged, True) is True
        assert staged[build_plan.DRAFT_KEY]["title"] == "Newsletter"
        assert staged["review_feedback"] == {"feedback": "Can you show me the updated plan?"}

    def test_a_run_that_saved_its_workflow_still_carries_nothing(self):
        saved = {WRITES_KEY: [{"kind": "tool", "id": "t1"}, {"kind": "orchestration", "id": "o1"}],
                 "build_saved": {"outcome": "saved"}, build_plan.DRAFT_KEY: {"title": "Newsletter"}}
        assert T.carry_request(saved, [{"step_id": "step_build_done", "status": "completed"}]) == {}
