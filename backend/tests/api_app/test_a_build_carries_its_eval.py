"""F1255 / D340 F (BLD1; Naveen 10-30, "it should be able to create workflows with eval set"): a build carries its eval.

A plan's `eval` is a rubric (what a good result is, from what the workflow is for) and a golden set of test inputs.
Code holds it to the Evals screen's own rules; the card lists each criterion and every case in full, since the yes saves
exactly those (Naveen, 10-30); the save makes it after the workflow through the Evals tools (`save_rubric`,
`save_eval_cases`, then `attach_rubric` last, so a stop never leaves the workflow scored by a removed rubric), and a
stop removes what it made. The first finished run is then judged, as every run of a workflow with a rubric is.
"""
from __future__ import annotations

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.evals import service
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, build_save
from core.native_builder.build_save import EVAL_RUBRIC, save_calls
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import areas, evals as E, registry
from core.product_tools.caller import Caller
from core.product_tools.roles import Role

EVAL = {"name": "Post quality", "criteria": [{"name": "Curiosity", "weight": 2,
                                              "guidance": "It makes the reader want to know more."},
                                             {"name": "Accuracy"}],
        "floor": 6, "cases": [{"name": "Tides", "input": "Write a post about why tides happen",
                               "notes": "Opens with a question"},
                              {"input": "Write a post about owls at night"},
                              {"name": "Hard", "input": "Write a post about nothing in particular"}]}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    areas.ensure_loaded()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_eval", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()
    E.set_evals_host(None)


def _plan(**more) -> dict:
    return {"kind": "create", "title": "Curiosity posts", "summary": "Writes a curiosity post on a topic.",
            "steps": [{"id": "write", "type": "llm", "name": "Write the post", "does": "Writes the post",
                       "prompt": "Write a curiosity post about {state.input}.", "next": "end"}],
            "eval": EVAL, **more}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


class TestThePart:
    async def test_the_card_lists_each_criterion_and_every_case(self, client, run):
        found = await _examine(run, _plan())
        assert found.errors == [], found.errors
        card = render(found.plan)
        assert ("## Eval\n- its eval: the rubric “Post quality” (2 criteria, scored 0–10), which a judge scores each "
                "run that finishes by, and a golden set of 3 cases to run on Evals") in card
        assert "- Criterion “Curiosity” (weight 2): It makes the reader want to know more." in card
        assert "- A score below 6 is a regression" in card
        assert "- Case 1 “Tides”: Write a post about why tides happen — Opens with a question" in card
        assert "- Case 2: Write a post about owls at night" in card
        assert "- Every run that finishes is judged: one more model call per run" in card

    async def test_a_rubric_with_no_criterion_or_too_few_or_many_cases_goes_back(self, client, run):
        found = await _examine(run, _plan(eval={**EVAL, "criteria": []}))
        assert any(e.startswith("eval: a rubric needs at least one criterion") for e in found.errors)
        for cases in (EVAL["cases"][:2], [{"input": f"c{i}"} for i in range(11)]):
            found = await _examine(run, _plan(eval={**EVAL, "cases": cases}))
            assert any(e.startswith("eval.cases: give 3 to 10 cases") for e in found.errors), len(cases)

    async def test_a_workflow_with_no_input_is_scored_on_its_real_runs(self, client, run):
        no_input = [{"id": "write", "type": "llm", "name": "Write the digest", "does": "Writes today's digest",
                     "prompt": "Write today's digest.", "next": "end"}]
        found = await _examine(run, _plan(steps=no_input))
        assert any(e.startswith("eval.cases: this workflow takes no input") for e in found.errors)
        found = await _examine(run, _plan(steps=no_input, eval={**EVAL, "cases": []}))
        assert found.errors == [], found.errors
        assert "which a judge scores each run that finishes by; its scores come from its real runs" \
            in render(found.plan)
        assert [c["tool"] for c in save_calls(found.plan, {}, run.shared_state)][-2:] == ["save_rubric",
                                                                                          "attach_rubric"]

    async def _update(self, run, **part):
        old = await service.save_rubric({"name": "Old quality", "criteria": [{"name": "Tone"}]})
        orch = S.make_orchestration(id="orch_posts", name="Curiosity posts", rubric_id=old["id"])
        await S.seed_orchestrations([orch])
        run.shared_state.update({"intent": "update", build_plan.EXISTING_KEY: orch})
        return await _examine(run, {"kind": "update", "title": "Curiosity posts", "summary": "Adds its eval.",
                                    "edits": [], "eval": {**EVAL, **part}})

    async def test_an_update_keeps_the_rubric_it_is_scored_by(self, client, run):
        found = await self._update(run)
        assert found.plan.eval.keeps == "Old quality" and found.plan.eval.replaces == ""
        assert "its eval: it keeps its rubric “Old quality”" in render(found.plan)
        assert "save_rubric" not in [c["tool"] for c in save_calls(found.plan, {}, run.shared_state)]

    async def test_an_ask_about_its_judging_replaces_it_and_says_the_trend_restarts(self, client, run):
        found = await self._update(run, replace_rubric=True)
        assert found.plan.eval.replaces == "Old quality"
        assert "it replaces the rubric “Old quality”, so its score trend starts again" in render(found.plan)

    async def test_a_host_s_plan_counts_are_the_plan_s_room(self, client, run):
        async def room(rubrics, sets):
            return f"Your Solo plan includes 3 rubrics; this build makes {rubrics} more. Delete one, or upgrade."

        E.set_evals_host(E.EvalsHost(room=room))
        found = await _examine(run, _plan())
        assert found.plan.room == "Your Solo plan includes 3 rubrics; this build makes 1 more. Delete one, or upgrade."


class TestTheSave:
    async def test_it_is_made_after_the_workflow_and_attached_last(self, client, run):
        found = await _examine(run, _plan())
        calls = save_calls(found.plan, {}, run.shared_state)
        tools = [c["tool"] for c in calls]
        assert tools[-3:] == ["save_rubric", "save_eval_cases", "attach_rubric"]
        assert tools.index("create_orchestration") < tools.index("save_rubric")
        workflow = next(c for c in calls if c["tool"] == "create_orchestration")["makes"]
        assert calls[-3]["makes"] == EVAL_RUBRIC
        assert calls[-2]["args"]["workflow"] == workflow and len(calls[-2]["args"]["cases"]) == 3
        assert calls[-1]["args"] == {"workflow": workflow, "rubric": EVAL_RUBRIC}

    async def test_those_calls_make_the_eval_through_the_evals_tools(self, client, run):
        await S.seed_orchestrations([S.make_orchestration(id="orch_made", name="Curiosity posts")])
        found = await _examine(run, _plan())
        made = {"__MAIN__": "orch_made"}
        main = next(c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "create_orchestration")
        made[main["makes"]] = "orch_made"
        caller = Caller(role=Role.BUILDER, via="assistant")
        for call in save_calls(found.plan, {}, run.shared_state)[-3:]:
            result = await registry.invoke(call["tool"], caller, build_save._filled(call["args"], made))
            if call.get("makes"):
                made[call["makes"]] = build_save._id_of(call, result)
        from core.store.resources import get_orchestration

        rubric = await service.get_rubric((await get_orchestration("orch_made"))["rubric_id"])
        assert rubric["name"] == "Post quality" and [c["name"] for c in rubric["criteria"]] == ["Curiosity",
                                                                                                  "Accuracy"]
        [suite] = await service.list_suites()
        assert suite["orchestration_id"] == "orch_made" and [c["input"] for c in suite["cases"]] == [
            "Write a post about why tides happen", "Write a post about owls at night",
            "Write a post about nothing in particular"]

    async def test_a_stop_removes_what_it_made(self, client, run):
        rubric = await service.save_rubric({"name": "Post quality", "criteria": [{"name": "Curiosity"}]})
        suite = await service.save_suite({"name": "Set", "orchestration_id": "orch_x", "cases": [{"input": "a"}]})
        await build_save._undo([("rubric", rubric["id"], "Post quality"), ("eval_suite", suite["id"], "Set")],
                               run.shared_state, [], "a later call failed")
        assert await service.list_rubrics() == [] and await service.list_suites() == []

    def test_a_made_rubric_and_set_are_read_by_their_ids(self):
        assert build_save._id_of({"kind": "rubric", "args": {}}, {"rubric": {"id": "rub_1"}}) == "rub_1"
        assert build_save._id_of({"kind": "eval_suite", "args": {}}, {"golden_set": {"id": "set_1"}}) == "set_1"
