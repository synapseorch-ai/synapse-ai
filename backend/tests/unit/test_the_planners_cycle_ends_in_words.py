"""The planner's cycle ends in words, never in an engine error (F1296, TRI1; CAP3's prod-13 rerun, builder_aab3ff880165).

A long run — questions, the provider research, a research round, reviews the person couldn't approve yet — ended in
"'Route on the Finish' gave up after 8 attempts". D2 sent a research round's one-form ask back through
`step_plan_finish`, so that router counted the planner's finishes and the research asks: it hit its cap of 8 a pass
before `step_plan`'s own (8, then `step_plan_rounds`' words), and it had no exit of its own. Nine other steps on the
cycle had a cap and no exit either: whichever filled first ended the run in an engine error.

Now the research ask has its own route (`step_research_ask_route`, the split `step_plan_finish` makes for an ask), so
`step_plan_finish` counts exactly the planner's finishes; and every step on a cycle through `step_plan` has an exit —
`step_plan_rounds`' words before anything is saved, `step_connect_give_up` (which removes the build's made tools and
says so) after. What the person reads is `step_plan_rounds`' sentence, not an engine error; the replay asserts it word
for word. The cycle is computed from the graph, so a step added to it later is held too. No cap was raised.
"""
from __future__ import annotations

import pytest

from core.expressions import evaluate_expression
from core.native_builder.seed import bundled_definitions
from test_an_exhausted_route_fails_the_run import _builder_graph, _run, driver, worker  # noqa: F401 — fixtures

WORDS = ("Nothing was built or changed: we went back and forth on this several times without settling it, so I "
         "stopped here. Tell me again in one message what you want, and I'll start fresh.")
#: After the yes, the build's own tools may be made: their exit removes them and says so.
AFTER_THE_YES = {"step_make_tools", "step_make_route", "step_connect_answer", "step_connect_route"}


@pytest.fixture(scope="module")
def steps():
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


def _next(step: dict) -> set[str]:
    out = [step.get(k) for k in ("next_step_id", "if_true_step_id", "if_false_step_id", "switch_default_step_id")]
    return {t for t in [*out, *(step.get("switch_cases") or {}).values()] if t}


def _cycle(steps: dict) -> set[str]:
    """The steps that step_plan reaches and that reach step_plan: every step a planner turn can come round through."""
    fwd = {k: _next(v) & set(steps) for k, v in steps.items()}
    rev: dict[str, set[str]] = {k: set() for k in steps}
    for k, vs in fwd.items():
        for v in vs:
            rev[v].add(k)

    def reach(graph):
        seen, todo = {"step_plan"}, ["step_plan"]
        while todo:
            for m in graph[todo.pop()]:
                if m not in seen:
                    seen.add(m)
                    todo.append(m)
        return seen
    return reach(fwd) & reach(rev)


def test_every_step_on_the_cycle_has_an_exit(steps):
    cycle = _cycle(steps)
    assert {"step_plan", "step_plan_finish", "step_research_ask", "step_human_review", "step_make_tools"} <= cycle
    missing = sorted(k for k in cycle if not steps[k].get("on_exhausted_step_id"))
    assert missing == [], f"a cap with no exit ends the run in an engine error: {missing}"
    assert all(steps[k]["on_exhausted_step_id"] in steps for k in cycle)


def test_before_the_yes_the_exit_is_the_words_and_after_it_the_tools_are_removed(steps):
    assert steps["step_plan_rounds"]["print_content"] == WORDS
    for sid in ("step_plan_finish", "step_plan_route", "step_research_ask", "step_research_ask_route",
                "step_approve_route", "step_check_change"):
        assert steps[sid]["on_exhausted_step_id"] == "step_plan_rounds", sid
    for sid in AFTER_THE_YES:
        assert steps[sid]["on_exhausted_step_id"] == "step_connect_give_up", sid


def test_only_the_planner_finishes_through_its_route(steps):
    into = sorted(k for k, s in steps.items() if "step_plan_finish" in _next(s))
    assert into == ["step_plan"], into


@pytest.mark.parametrize("intent, check", [("create", "step_check_plan"), ("manage", "step_check_change"),
                                           (None, "step_check_plan")])
def test_a_research_ask_reaches_the_check_its_request_is_on(steps, intent, check):
    assert steps["step_research_ask"]["next_step_id"] == "step_research_ask_route"
    route = steps["step_research_ask_route"]
    value = evaluate_expression(route["switch_expression"], {"intent": intent}, None).value
    assert route["switch_cases"].get(value, route["switch_default_step_id"]) == check


class TestALongRunEndsInTheWords:
    """prod-13's shape, routing only: a research round, then three questions and four reviews — eight planner passes
    and one research ask. Before F1296 `step_plan_finish` took its ninth visit on the eighth pass and the engine gave
    up; now the ninth pass meets `step_plan`'s own cap and the person reads the words."""

    def _graph(self):
        graph = _builder_graph()
        for s in graph["steps"]:
            if s["id"] == "step_plan":
                s["print_content"] = '{"finish": "{state.human_response.finish}"}'
            elif s["id"] == "step_research_answer":
                s["print_content"] = '{"next": "person"}'
        return graph

    async def test_it_ends_with_step_plan_rounds_words(self, driver, fake_llm):
        run_id = "run_f1296"
        assert await driver.start(self._graph(), run_id, initial_state={
            "plan_room": {"orchestrations_left": 1},
            "human_response": {"next": "ask", "finish": "research_questions"}}) == "paused"
        for _ in range(3):
            assert await driver.answer(run_id, {"answer": "more", "next": "ask"}) == "paused"
        for _ in range(3):
            assert await driver.answer(run_id, {"answer": "more", "next": "review"}) == "paused"
        for _ in range(3):
            status = await driver.answer(run_id, {"feedback": "change it", "next": "review"})
            if status != "paused":
                break
        assert status == "completed", status
        run = await _run(run_id)
        ran = [h["step_id"] for h in run.step_history]
        assert "step_research_ask" in ran
        assert ran.count("step_plan_finish") <= ran.count("step_plan"), "the router counts only the planner's finishes"
        assert run.shared_state.get("summary") == WORDS, "the words, not an engine error"
