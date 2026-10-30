"""Synapse AI has one planner: it makes what is new and changes what exists, and the route can no longer be wrong
(D340 D0; Naveen 10-30: "merge into one in D340", 10-31: the plan as restated).

Two planners — one for builds, one for changes — and a classifier that had to pick between them before anything was
thought through: "change" for a plan never saved made only an agent (F1242), and "create an eval" built a workflow
copy of Evals. Now the classifier's labels stay as they are (Naveen 10-31: kept as hints), and build, update and
manage all reach the one planner, after the same door; it holds both planners' tools and finishes with a plan, a
change, a plain reply, a question or "can't", and code sends each to the check that was already there. Everything
after the checks — review, approve, save, apply, the maker, the Connect cards, undo — is as it was.
"""
from __future__ import annotations

import json
import types

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order (change_tools after it)
from core.expressions import evaluate_expression
from core.native_builder.seed import bundled_definitions


@pytest.fixture(scope="module")
def bundle():
    defs = bundled_definitions()
    orch = defs["orchestration"]
    return {
        "steps": {s["id"]: s for s in orch["steps"]},
        "agents": {a["id"]: a for a in defs["agents"]},
        "orch": orch,
    }


def _targets(step: dict) -> set[str]:
    out = {step.get(k) for k in ("next_step_id", "if_true_step_id", "if_false_step_id", "switch_default_step_id",
                                 "on_exhausted_step_id", "true_step_id", "false_step_id")}
    out |= set((step.get("switch_cases") or {}).values())
    return {t for t in out if t}


class TestThereIsOnePlanner:
    def test_the_change_planner_and_its_step_are_gone(self, bundle):
        assert "agent_native_builder_change_planner" not in bundle["agents"]
        assert "step_change" not in bundle["steps"]
        planners = [s for s in bundle["steps"].values() if s.get("agent_id") == "agent_native_builder_planner"]
        assert [s["id"] for s in planners] == ["step_plan"]

    def test_it_holds_both_planners_tools_and_six_finishes(self, bundle):
        agent = bundle["agents"]["agent_native_builder_planner"]
        for tool in ("read_workspace", "read_api_spec", "list_agents", "get_agent", "list_orchestrations",
                     "get_orchestration", "list_schedules", "list_custom_tools", "list_skills",
                     "product_capabilities"):
            assert tool in agent["tools"], tool
        # D2 adds `research_questions`: the planner asks, code and the researcher answer — still no web on it.
        finishes = ["submit_plan", "submit_change", "say", "research_questions", "ask_person", "cannot"]
        assert bundle["steps"]["step_plan"]["finish_tools"] == finishes
        assert all(f in agent["tools"] for f in finishes)
        assert not any(t in agent["tools"] for t in ("search_the_web", "read_web_page")), "no web on the planner"

    def test_its_instructions_keep_every_rule_in_its_section(self, bundle):
        prompt = bundle["agents"]["agent_native_builder_planner"]["system_prompt"]
        for section in ("# Role", "# Choose what the ask needs", "# Building", "# Changing", "# Finishing"):
            assert section in prompt, section
        building, changing = prompt.split("# Building", 1)[1], prompt.split("# Changing", 1)[1]
        assert "8. What the pack's `not_here` lists is done on its screen" in building
        for n in range(1, 12):
            assert f"\n{n}. " in changing, f"the change rules keep their numbers: {n}"
        assert "never delete it and make a new one" in changing  # rule 6, as it was
        assert "11. Evals (topic `evals`) score a workflow's runs" in changing  # F1255's, folded in under its number
        assert changing.count("recordings, evals)") == 1 and prompt.count("recordings, evals)") == 2

    def test_its_template_has_both_contexts_and_both_second_passes(self, bundle):
        template = bundle["steps"]["step_plan"]["prompt_template"]
        for ref in ("state.plan_context", "state.change_context", "state.plan_findings", "state.plan_checked.say",
                    "state.change_checked.review", "state.manage_review.feedback", "state.review_feedback.feedback"):
            assert "{{ " + ref in template, ref


class TestEveryActReachesIt:
    def test_build_update_and_manage_all_reach_the_one_planner(self, bundle):
        steps = bundle["steps"]
        cases = steps["step_change_route"]["switch_cases"]
        assert cases["create"] == cases["update"] == "step_effort", "a build still meets the effort check first"
        assert cases["manage"] == "step_plan_context", "a change skips effort and plan room, as before"
        assert steps["step_fetch_existing"]["next_step_id"] == "step_plan_context"
        assert steps["step_plan_context"]["next_step_id"] == "step_change_context"
        assert steps["step_change_context"]["next_step_id"] == "step_plan"

    def test_the_door_is_as_it_was(self, bundle):
        steps = bundle["steps"]
        for sid in ("step_may_change", "step_not_yours", "step_effort", "step_plan_room", "step_classify_door",
                    "step_classify_at_plan"):
            assert sid in steps, sid
        assert steps["step_classify"]["finish_tools"] == ["classify_request"], "the classifier is unchanged"

    def test_every_way_back_to_a_planner_reaches_the_one_planner(self, bundle):
        steps = bundle["steps"]
        for sid in ("step_change_clarify", "step_human_clarify", "step_plan_found", "step_plan_research"):
            assert steps[sid]["next_step_id"] == "step_plan", sid

    def test_no_edge_points_nowhere(self, bundle):
        steps = bundle["steps"]
        dangling = [(s["id"], t) for s in steps.values() for t in _targets(s) if t not in steps]
        assert dangling == []


class TestEachFinishReachesItsCheck:
    @pytest.mark.parametrize("intent, finish, step", [
        # The planner's own choice decides a plan or a change, whatever the classifier said (F1242's class).
        ("create", "submit_plan", "step_check_plan"), ("manage", "submit_plan", "step_check_plan"),
        ("manage", "submit_change", "step_check_change"), ("create", "submit_change", "step_check_change"),
        ("create", "say", "step_say"), ("manage", "say", "step_say"),
        # A question or a "can't" follows the side the run is on, so its answer is kept where that check reads it
        # (F960's answer across the pauses on a change; F1226's on a plan).
        ("create", "ask_person", "step_check_plan"), ("update", "cannot", "step_check_plan"),
        ("manage", "ask_person", "step_check_change"), ("manage", "cannot", "step_check_change"),
    ])
    def test_the_finish_switch(self, bundle, intent, finish, step):
        route = bundle["steps"]["step_plan_finish"]
        assert bundle["steps"]["step_plan"]["next_step_id"] == "step_plan_finish"
        state = {"plan_draft": {"finish": finish}, "intent": intent}
        value = evaluate_expression(route["switch_expression"], state, None).value
        assert route["switch_cases"].get(value, route["switch_default_step_id"]) == step

    def test_a_planner_that_never_finishes_says_so_plainly(self, bundle):
        """The change planner's fallback, now the one planner's: no finish ends in the change check's plain "I couldn't
        turn that into something I can make or change here", never a failed run (a build's planner had none)."""
        plan, route = bundle["steps"]["step_plan"], bundle["steps"]["step_plan_finish"]
        assert plan["finish_fallback"] == {"finish": "cannot"}
        state = {"plan_draft": {**plan["finish_fallback"], "fallback": True}}
        value = evaluate_expression(route["switch_expression"], state, None).value
        assert route["switch_cases"].get(value, route["switch_default_step_id"]) == "step_check_change"

    def test_a_finish_that_names_nothing_is_a_plans_check(self, bundle):
        route = bundle["steps"]["step_plan_finish"]
        value = evaluate_expression(route["switch_expression"], {"plan_draft": {}}, None).value
        assert route["switch_cases"].get(value, route["switch_default_step_id"]) == "step_check_plan"

    def test_the_change_check_reads_the_one_planners_finish(self, bundle):
        check = bundle["steps"]["step_check_change"]
        assert check["tool_arguments"] == {"draft": "{{ state.plan_draft }}"}

    def test_a_reply_is_said_through_the_answer_routes_own_check(self, bundle):
        say = bundle["steps"]["step_say"]
        assert say["type"] == "print" and say["print_content"] == "{{ state.plan_draft.text }}"
        assert say["output_key"] == "summary" and say["next_step_id"] == "step_answer_checked"


class TestSay:
    async def test_it_is_the_reply(self):
        from core.native_builder.change_tools import say

        assert await say({"text": " You have three workflows. "}, None, []) == {
            "finish": "say", "text": "You have three workflows."}

    async def test_an_empty_reply_goes_back_to_the_model(self):
        from core.native_builder.change_tools import say

        assert "error" in await say({"text": "  "}, None, [])

    async def test_a_reply_never_asks_for_a_key(self):
        from core.native_builder.change_tools import say

        out = await say({"text": "Paste your OpenAI API key here and I'll connect it."}, None, [])
        assert "error" in out

    def test_it_is_a_builder_tool(self):
        import core.native_builder.change_tools  # noqa: F401 — registers its tools
        from core.builder_tools import BUILDER_TOOL_NAMES, OPT_IN_BUILDER_TOOLS

        assert "say" in BUILDER_TOOL_NAMES and "say" in OPT_IN_BUILDER_TOOLS, "offered only to agents that name it"
