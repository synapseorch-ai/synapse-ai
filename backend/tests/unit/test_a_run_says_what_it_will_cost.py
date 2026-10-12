"""CE1 (D261): what a run will cost, read from its definition before it runs.

`core.orchestration.estimate` walks a definition the way the engine will: the low
end takes the cheapest arm at every branch and the high end the dearest, so both
are runs that can happen. End is never charged. Whatever a definition cannot say
(a for-each loop's length, a route back, a nested workflow) is named in `open`,
never guessed, and a definition it cannot follow never raises.
"""
import pytest

from core.orchestration.estimate import (
    LOCAL,
    NESTED,
    OWN_KEYS,
    PER_ITEM,
    REPEAT,
    TOKEN_PRIOR,
    WALLET,
    ModelRate,
    Span,
    iterations,
    model_cost,
    models,
    runs_span,
    span,
    step_model,
)

#: Cloud's seeded step prices, in millicredits (`api/db/models/step_cost.py`).
RATES = {"agent": 1000, "llm": 1000, "evaluator": 1000, "extract_json": 1000, "tool": 1000,
         "transform": 500, "parallel": 500, "if_else": 100, "print": 100, "end": 100, "loop": 100,
         "merge": 100, "switch": 100, "human": 0}


def credits(step):
    mc = RATES.get(step.get("type"), 1000)
    return mc, mc


def _step(sid, kind, nxt=None, **kw):
    return {"id": sid, "name": sid, "type": kind, "next_step_id": nxt, **kw}


def _orch(steps, entry=None):
    return {"id": "o", "name": "o", "entry_step_id": entry or steps[0]["id"], "steps": steps}


def weekly_report():
    """The Weekly report template's shape: an approval, then one of two arms."""
    return _orch([
        _step("write", "llm", "review"),
        _step("review", "human", "gate"),
        _step("gate", "if_else", if_true_step_id="file", if_false_step_id="revise"),
        _step("revise", "llm", "file_revised"),
        _step("file", "agent", "end", agent_id="filer"),
        _step("file_revised", "agent", "end", agent_id="filer"),
        _step("end", "end"),
    ])


class TestTheRangeRunsFromTheCheapestPathToTheDearest:
    def test_a_straight_line_is_one_figure_and_end_is_free(self):
        got = span(_orch([_step("a", "llm", "b"), _step("b", "print", "c"), _step("c", "end")]), credits)
        assert (got.low, got.high, got.open) == (1100, 1100, frozenset())

    def test_an_approval_prices_each_arm_as_its_own_run(self):
        # 2.1 credits approved, 3.1 revised. The hold prices both arms (4.2), which no run costs.
        got = span(weekly_report(), credits)
        assert (got.low, got.high) == (2100, 3100)

    def test_an_arm_that_ends_the_run_is_the_cheapest_path(self):
        got = span(_orch([_step("gate", "if_else", if_true_step_id="work", if_false_step_id=None),
                          _step("work", "agent", None)]), credits)
        assert (got.low, got.high) == (100, 1100)

    def test_a_switch_takes_any_case_or_its_default(self):
        got = span(_orch([
            _step("s", "switch", switch_cases={"a": "cheap", "b": "dear"}, switch_default_step_id="mid"),
            _step("cheap", "print"), _step("mid", "llm"),
            _step("dear", "agent", "dear2"), _step("dear2", "agent"),
        ]), credits)
        assert (got.low, got.high) == (200, 2100)

    def test_an_evaluators_routes_are_its_branches(self):
        got = span(_orch([
            _step("judge", "evaluator", route_map={"good": "ship", "bad": None}),
            _step("ship", "print"),
        ]), credits)
        assert (got.low, got.high) == (1000, 1100)

    def test_inbox_triage_takes_two_branches(self):
        got = span(_orch([
            _step("classify", "llm", "extract"),
            _step("extract", "extract_json", "urgent"),
            _step("urgent", "if_else", if_true_step_id="escalate", if_false_step_id="reply"),
            _step("escalate", "llm", "reply"),
            _step("reply", "llm", "review"),
            _step("review", "human", "gate"),
            _step("gate", "if_else", if_true_step_id="publish", if_false_step_id="revise"),
            _step("revise", "llm", "publish_revised"),
            _step("publish", "print", "end"),
            _step("publish_revised", "print", "end"),
            _step("end", "end"),
        ]), credits)
        assert (got.low, got.high) == (3300, 5300)


class TestWhatADefinitionCannotSayIsNamed:
    def test_a_route_back_is_priced_once_and_marked(self):
        got = span(_orch([
            _step("write", "llm", "judge"),
            _step("judge", "evaluator", route_map={"retry": "write", "ok": "done"}),
            _step("done", "end"),
        ]), credits)
        assert (got.low, got.high) == (2000, 2000)
        assert got.open == {REPEAT}

    def test_a_count_loop_runs_its_body_its_count(self):
        got = span(_orch([
            _step("loop", "loop", "end", loop_count=4, loop_step_ids=["work", "say"]),
            _step("work", "agent", "say"), _step("say", "print"), _step("end", "end"),
        ]), credits)
        assert (got.low, got.high, got.open) == (100 + 4 * 1100, 100 + 4 * 1100, frozenset())

    def test_a_count_is_clamped_to_its_cap_as_the_engine_clamps_it(self):
        assert iterations({"loop_count": 500, "loop_max_iterations": 7}) == (7, False)
        assert iterations({}) == (3, False)

    @pytest.mark.parametrize("data", [{"loop_over": "state.items"}, {"loop_while_condition": "state.more"}])
    def test_a_for_each_or_while_loop_is_one_iteration_and_marked(self, data):
        got = span(_orch([_step("loop", "loop", None, loop_step_ids=["work"], **data), _step("work", "llm")]), credits)
        assert (got.low, got.high) == (1100, 1100)
        assert got.open == {PER_ITEM}

    def test_a_parallel_runs_every_branch_and_no_step_twice(self):
        got = span(_orch([
            _step("fan", "parallel", "merge", parallel_branches=[["a1", "a2"], ["b1"]]),
            _step("a1", "llm", "a2"), _step("a2", "print"), _step("b1", "agent"),
            _step("merge", "merge", "end"), _step("end", "end"),
        ]), credits)
        assert (got.low, got.high) == (500 + 1100 + 1000 + 100, 500 + 1100 + 1000 + 100)

    def test_a_loop_around_a_parallel_multiplies_its_branches(self):
        got = span(_orch([
            _step("loop", "loop", None, loop_count=2, loop_step_ids=["fan"]),
            _step("fan", "parallel", None, parallel_branches=[["a"], ["b"]]),
            _step("a", "llm"), _step("b", "llm"),
        ]), credits)
        assert got.low == 100 + 2 * (500 + 2000)

    def test_a_step_that_runs_another_workflow_is_marked(self):
        got = span(_orch([_step("sub", "agent", agent_id="wf")]), credits,
                   agents={"wf": {"id": "wf", "type": "orchestrator", "orchestration_id": "o2"}})
        assert got.open == {NESTED}


class TestADefinitionItCannotFollowNeverRaises:
    @pytest.mark.parametrize("definition", [None, {}, {"steps": None}, {"steps": []}, {"steps": ["junk", 3]}])
    def test_nothing_to_walk_costs_nothing(self, definition):
        assert span(definition, credits) == Span()

    def test_a_missing_entry_starts_at_the_first_step_outside_any_container(self):
        got = span({"steps": [_step("loop", "loop", None, loop_count=1, loop_step_ids=["w"]),
                              _step("w", "llm")], "entry_step_id": "gone"}, credits)
        assert got.low == 1100

    def test_a_step_naming_one_that_does_not_exist_ends_there(self):
        assert span(_orch([_step("a", "llm", "nowhere")]), credits).low == 1000

    def test_a_container_that_holds_itself_is_priced_once(self):
        got = span(_orch([_step("loop", "loop", None, loop_count=2, loop_step_ids=["loop", "w"]),
                          _step("w", "llm")]), credits)
        assert got.low == 100 + 2 * 1000

    def test_a_step_that_routes_to_itself_is_a_repeat(self):
        got = span(_orch([_step("a", "llm", "a")]), credits)
        assert (got.low, got.open) == (1000, {REPEAT})


AGENTS = {"filer": {"id": "filer", "type": "conversational", "model": None},
          "pinned": {"id": "pinned", "type": "conversational", "model": "claude-sonnet-4-5"}}


def priced(model):
    if model.startswith("synapse."):
        return ModelRate(WALLET, 1.0, 4.0)
    if model.startswith("ollama."):
        return ModelRate(LOCAL, 0.0, 0.0)
    if model.startswith("claude"):
        return ModelRate(OWN_KEYS, 3.0, 15.0)
    return None


class TestTheModelHalf:
    def test_a_steps_model_is_its_own_then_its_agents_then_the_workspaces(self):
        ws = "synapse.nova-pro"
        assert step_model(_step("s", "llm", model="gpt-4o"), AGENTS, ws) == "gpt-4o"
        assert step_model(_step("s", "llm", model="default"), AGENTS, ws) == ws
        assert step_model(_step("s", "agent", agent_id="pinned"), AGENTS, ws) == "claude-sonnet-4-5"
        assert step_model(_step("s", "agent", agent_id="filer"), AGENTS, ws) == ws
        assert step_model(_step("s", "agent", agent_id="pinned", model="ollama.qwen3"), AGENTS, ws) == "ollama.qwen3"

    @pytest.mark.parametrize("kind", ["extract_json", "transform", "human", "if_else", "print", "end"])
    def test_a_step_that_calls_no_model_has_none(self, kind):
        assert step_model(_step("s", kind), AGENTS, "synapse.nova-pro") is None

    def test_the_prior_prices_low_at_the_25th_and_high_at_the_75th(self):
        rate = ModelRate(WALLET, 1.0, 4.0)
        t = TOKEN_PRIOR["agent"]
        assert model_cost("agent", rate) == pytest.approx(
            ((t.input[0] + 4 * t.output[0]) / 1e6, (t.input[1] + 4 * t.output[1]) / 1e6))
        assert model_cost("evaluator", rate) == model_cost("llm", rate)

    def test_each_side_that_pays_is_its_own_span_never_summed(self):
        got = models(weekly_report(), agents={**AGENTS, "filer": AGENTS["pinned"]},
                     workspace_model="synapse.nova-pro", price_model=priced)
        llm_lo, llm_hi = model_cost("llm", ModelRate(WALLET, 1.0, 4.0))
        agent_lo, agent_hi = model_cost("agent", ModelRate(OWN_KEYS, 3.0, 15.0))
        assert set(got.sides) == {WALLET, OWN_KEYS}
        # Approved: write (wallet) + file (own key). Revised: write + revise (wallet) + file_revised (own key).
        assert got.sides[WALLET].low == pytest.approx(llm_lo)
        assert got.sides[WALLET].high == pytest.approx(2 * llm_hi)
        assert got.sides[OWN_KEYS].low == pytest.approx(agent_lo)
        assert got.sides[OWN_KEYS].high == pytest.approx(agent_hi)
        assert got.models == ["synapse.nova-pro", "claude-sonnet-4-5"]

    def test_a_model_nothing_prices_is_named_not_free(self):
        got = models(_orch([_step("a", "llm", model="mystery-model")]), agents={},
                     workspace_model="synapse.nova-pro", price_model=priced)
        assert got.unpriced == ["mystery-model"]
        assert got.sides == {}

    def test_a_local_model_costs_nothing_and_says_so(self):
        got = models(_orch([_step("a", "llm")]), agents={}, workspace_model="ollama.qwen3", price_model=priced)
        assert got.sides == {LOCAL: Span(0.0, 0.0)}
        assert got.unpriced == []


def test_runs_span_is_the_lowest_and_highest_finished_run():
    assert runs_span([0.05, None, 0.02, 0.09]) == Span(0.02, 0.09)
    assert runs_span([]) is None
