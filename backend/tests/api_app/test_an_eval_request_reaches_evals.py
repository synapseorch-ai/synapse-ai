"""F1255 (BLD1; commit 3): an eval request reaches Evals, never a hand-built copy of it.

"Create an eval for my curiosity post generator" was classified a build and made a workflow that ran the generator and
had an agent judge it (TRI1's triage part 3). Now classify has an `eval` kind that goes to the change route; its ops
(eval_rubric, eval_cases, eval_run, eval_cancel) are cards whose buttons are the Evals screen's own tools; a case a
model proposes is shown in full and saved only by the press; an agent named is told that evals score workflows, with
the workflows that use it; `read_workspace` reads Evals (topic `evals`); and a create plan that runs a workflow here
and has a model judge what it wrote is said, pointing to Evals. `judge_once` scores one input and output with the
product's own judge, for D340's gate.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.evals import generate, service
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, change_ops, classify, objects as O
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks

CRITERIA = [{"name": "Curiosity", "weight": 2}, {"name": "Accuracy"}]


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def assistant_run(client):
    from core.store import get_store

    await get_store()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_evals", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    await S.seed_agents([S.make_agent(id="agent_writer", name="Post writer"),
                         S.make_agent(id="agent_lonely", name="Lonely")])
    await S.seed_orchestrations([S.make_orchestration(
        id="orch_posts", name="Curiosity post generator",
        steps=[{"id": "write", "name": "Write", "type": "agent", "agent_id": "agent_writer",
                "prompt_template": "Write about {state.user_input}.", "next_step_id": None}],
        entry_step_id="write")])
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_evals")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


class TestTheCards:
    async def test_a_rubric_is_saved_and_attached_by_the_press(self, client):
        said, [card] = await _prepare({"action": "eval_rubric", "workflow": "Curiosity post generator",
                                       "name": "Post quality", "criteria": CRITERIA})
        assert card["title"] == 'Score "Curiosity post generator" with the rubric "Post quality"'
        assert card["detail"].startswith("Curiosity (weight 2); Accuracy (weight 1); scored 0–10")
        assert "Nothing has been done yet" in said["say"] and await service.list_rubrics() == []
        await _press(client, card)
        from core.store.resources import get_orchestration

        [rubric] = await service.list_rubrics()
        assert (await get_orchestration("orch_posts"))["rubric_id"] == rubric["id"]

    async def test_proposed_cases_are_shown_in_full_and_saved_only_by_the_press(self, client, monkeypatch):
        async def propose(orch, rubric, settings, *, count, steer):
            return [{"name": "Tides", "input": "Why do tides happen?", "notes": "Asks a question first"},
                    {"name": "", "input": "Owls at night", "notes": ""}][:count]

        monkeypatch.setattr(generate, "propose", propose)
        _, [card] = await _prepare({"action": "eval_cases", "workflow": "orch_posts", "count": 2})
        assert card["title"] == 'Add 2 test cases to "Curiosity post generator"\'s golden set'
        assert card["detail"].splitlines() == ["Tides: Why do tides happen?", "Case: Owls at night"]
        assert await service.list_suites() == []
        await _press(client, card)
        [suite] = await service.list_suites()
        assert [c["input"] for c in suite["cases"]] == ["Why do tides happen?", "Owls at night"]

    async def test_a_run_says_what_it_costs_and_a_stop_is_a_danger_card(self, client):
        await service.save_suite({"name": "Posts set", "orchestration_id": "orch_posts",
                                  "cases": [{"input": "a"}, {"input": "b"}]})
        _, [card] = await _prepare({"action": "eval_run", "workflow": "orch_posts"})
        assert card["title"] == 'Run the eval of "Curiosity post generator"'
        assert '2 runs of the workflow and 2 judge calls' in card["detail"]
        assert card["buttons"][0]["tool"] == "start_eval_suite"
        _, [stop] = await _prepare({"action": "eval_cancel", "workflow": "orch_posts"})
        assert stop["danger"] is True and stop["buttons"][0]["tool"] == "cancel_eval_suite"

    async def test_an_agent_named_is_told_evals_score_workflows(self, client):
        said, cards = await _prepare({"action": "eval_rubric", "workflow": "Post writer", "criteria": CRITERIA})
        assert cards == [] and said["error"].startswith(
            "Evals score a workflow's runs, not an agent on its own: \"Post writer\" is an agent. Workflows that use "
            "it: \"Curiosity post generator\". Ask which one.")
        said, _ = await _prepare({"action": "eval_run", "workflow": "Lonely"})
        assert "No workflow uses it yet: offer to make a one-step workflow that runs it" in said["error"]

    def test_the_ops_hold_their_fields(self):
        assert change_ops.issues("eval_rubric", {"kind": "workflow", "name": "x"},
                                 {"criteria": [{"weight": 2}]}) != []
        assert change_ops.issues("eval_cases", {"kind": "workflow", "name": "x"}, {"count": 11}) != []
        assert change_ops.issues("eval_cases", {"kind": "workflow", "name": "x"},
                                 {"cases": [{"input": "hi"}]}) == []


class TestReadingIt:
    async def test_the_topic_lists_what_is_scored_and_reads_one_whole(self, client):
        raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "evals"}, _Server(),
                                                        run_id="builder_evals")
        assert json.loads(raw)["items"] == []
        rubric = await service.save_rubric({"name": "Post quality", "criteria": CRITERIA})
        await service.attach_rubric("orch_posts", rubric["id"])
        raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "evals"}, _Server(),
                                                        run_id="builder_evals")
        [item] = json.loads(raw)["items"]
        assert item["workflow"] == "Curiosity post generator" and item["rubric"] == "Post quality"
        raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "evals",
                                                                           "name": "Curiosity post generator"},
                                                        _Server(), run_id="builder_evals")
        assert json.loads(raw)["rubric"]["name"] == "Post quality"


class TestTheRoute:
    """Classify's typed `feature` (TRI1, D340: one field for every feature; D0 maps it onto its act route), never
    the message's words."""

    def _routed(self, message, kind="build", feature="evals", **state):
        return classify.override(O.Request(kind=kind, feature=feature), message=message, state=state, inventory={},
                                 connectable=set())

    def test_an_eval_goes_to_the_change_route_as_a_change_s_role_even_typed_a_build(self):
        routed = self._routed("create an eval for my curiosity post generator")
        assert (routed.kind, routed.route, routed.overridden_by) == ("change", "step_change_context", "evals")
        assert classify.intent_of(routed) == "manage"

    def test_the_words_alone_route_nothing(self):
        routed = self._routed("create an eval for my curiosity post generator", feature=None)
        assert routed.kind == "build", "no word heuristic: the field decides"

    def test_after_findings_it_stays_evals(self):
        routed = self._routed("yes, build it", prior_findings={"options": [{"provider": "X"}]})
        assert routed.kind == "change"

    def test_a_question_about_evals_is_an_answer(self):
        # As for a change: the question rule's own words, the same for every kind that acts.
        assert self._routed("how do I add an eval?").kind == "answer"

    def test_an_approver_reaches_it_as_any_change_and_its_tools_ask_a_builder(self):
        # The change route, as for every change an approver may ask about; Evals' tools need a builder (commit 1).
        from core.product_tools import areas, registry
        from core.product_tools.roles import Role

        assert self._routed("add an eval", may_change=False).route == "step_change_context"
        areas.ensure_loaded()
        assert registry.get("save_rubric").min_role == Role.BUILDER


class TestImitation:
    async def test_a_plan_that_runs_one_and_judges_it_is_said_to_be_an_eval(self, client):
        ws = await build_plan.workspace(_Server())
        plan = O.Plan.from_draft({"kind": "create", "title": "Post Generator Eval", "summary": "Scores the posts.",
                                  "steps": [{"id": "gen", "type": "workflow", "name": "Run it", "does": "Runs it",
                                             "workflow": "orch_posts", "next": "judge"},
                                            {"id": "judge", "type": "llm", "name": "Judge", "does": "Scores it",
                                             "prompt": "Score {{state.gen}} from 1 to 10.", "next": "end"}]})
        said = build_plan.imitates_eval(plan, ws)
        assert said.startswith('That is an eval of "Curiosity post generator": Evals scores its runs by a rubric')

    async def test_running_one_and_then_sending_it_is_a_workflow(self, client):
        ws = await build_plan.workspace(_Server())
        plan = O.Plan.from_draft({"kind": "create", "title": "Post and send", "summary": "Writes and sends.",
                                  "steps": [{"id": "gen", "type": "workflow", "name": "Run it", "does": "Runs it",
                                             "workflow": "orch_posts", "next": "tidy"},
                                            {"id": "tidy", "type": "llm", "name": "Tidy", "does": "Tidies it",
                                             "prompt": "Tidy {{state.gen}}.", "next": "send"},
                                            {"id": "send", "type": "tool", "name": "Send", "does": "Sends it",
                                             "tool": "send_email", "next": "end"}]})
        assert build_plan.imitates_eval(plan, ws) is None


class TestJudgeOnce:
    async def test_one_input_and_output_are_scored_by_the_product_s_judge(self, monkeypatch):
        from core import llm_providers

        seen = {}

        async def answer(**call):
            seen.update(call)
            return json.dumps({"score": 8, "reasoning": "Good. Curious.",
                               "criteria": {"curiosity": {"score": 9, "reasoning": "Yes."}}})

        monkeypatch.setattr(llm_providers, "generate_response", answer)
        verdict = await service.judge_once({"name": "Post quality", "criteria": CRITERIA}, "Tides",
                                           "Why does the sea breathe twice a day?", {"model": "claude-haiku-4-5"})
        assert verdict["score"] == 8 and verdict["judge_model"] == "claude-haiku-4-5"
        assert "Why does the sea breathe twice a day?" in seen["prompt_msg"] and seen["source"] == "orchestration:judge"
