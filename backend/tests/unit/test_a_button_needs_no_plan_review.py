"""A button's act that reaches the change route is a card, not a plan to review (F755, ASSIST1, D291; re-pointed at
the Change's op kinds by SAI2-P2, D308).

BWA1's assist batch on dev (Nova Pro, 2026-10-17): "Pause the Monday invoices schedule", which was already off, went
to the change route; its planner, which read nothing, drafted `set_schedule_enabled — disables …`; the person
approved; the operator found nothing to do. A plan review spent on one thing the app does with a button, and on a
change that changed nothing. Naveen: a card, no review.

The gate that decided it was a substring test on the plan's markdown (`step_manage_cards`). Now the change planner
proposes typed ops, and whether each is a card or goes to the review is the op's kind (`change_ops.OPS`), decided in
code by `check_change`: the button acts (`ACTIONS`) are cards, through `prepare_action`'s own code, so a schedule
already off is said to be, with no card; a change of acts only never reaches the review or the apply. Turning an
existing schedule on or off is a button act; the Schedules screen's toggle is not an op a model can write.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_actions import ACTIONS
from core.models_orchestration import Orchestration, OrchestrationRun
from core.native_builder import change_ops
from core.native_builder.objects import Change
from core.native_builder.seed import bundled_definitions
from core.orchestration.engine import OrchestrationEngine


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
def engine() -> OrchestrationEngine:
    return OrchestrationEngine(Orchestration.model_validate(bundled_definitions()["orchestration"]), _Server())


def _steps() -> dict:
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


def _agents() -> dict:
    return {a["id"]: a for a in bundled_definitions()["agents"]}


def _op(op, kind, target, **fields):
    return {"op": op, "target": {"kind": kind, "name": target}, "fields": fields}


async def _next(engine, step_id: str, state: dict) -> str | None:
    step = engine.step_map[step_id]
    run = OrchestrationRun(run_id="r", orchestration_id="orch_native_builder", shared_state=dict(state))
    async for _ in engine.executors[step.type].execute(step, run, engine):
        pass
    target, _ = engine._resolve_next(step, run)
    return target


#: A button's acts, as the change planner writes them.
CARDS = [
    [_op("schedule_off", "schedule", "Monday invoices")],
    [_op("schedule_on", "schedule", "Monday invoices")],
    [_op("run_workflow", "workflow", "Invoice chaser"), _op("cancel_run", "run", "run_8f2")],
    [_op("board_delete", "board", "Bugs")],
    [_op("knowledge_delete_document", "document", "pricing.pdf")],
]

#: Changes the person must review: any write, alone or beside a card.
REVIEWED = [
    [_op("set_schedule", "schedule", "Every night at 2", cron_expression="0 3 * * *"),
     _op("run_schedule_now", "schedule", "Every night at 2")],
    [_op("create_schedule", "schedule", "Mondays", runs={"kind": "workflow", "name": "Invoice sync"}, prompt="Go",
         cron_expression="0 9 * * 1")],
    [_op("rename_schedule", "schedule", "Monday invoices", name="Weekly invoices"),
     _op("schedule_off", "schedule", "Weekly invoices")],
    [_op("delete_agent", "agent", "Old bot")],
]


def test_the_split_is_code_between_the_planner_and_its_review():
    steps = _steps()
    # The one planner's change goes through its finish switch to the change check (D340 D0), still code.
    assert steps["step_plan"]["next_step_id"] == "step_plan_finish"
    route = steps["step_plan_finish"]
    assert route["type"] == "switch" and route["switch_cases"]["submit_change"] == "step_check_change"
    check = steps["step_check_change"]
    assert check["type"] == "tool" and check["forced_tool"] == "check_change"
    assert check["tool_arguments"] == {"draft": "{{ state.change_draft }}"}  # set: no model reads the draft
    route = steps["step_check_route"]
    assert route["type"] == "switch" and route["switch_expression"] == 'state.change_checked.outcome or "error"'
    assert route["switch_cases"]["review"] == "step_manage_review"
    assert route["switch_cases"]["said"] == "step_change_said"


@pytest.mark.parametrize("ops", CARDS)
def test_a_change_of_buttons_only_has_nothing_to_review(ops):
    change = Change.model_validate({"ops": ops})
    assert all(op.is_act for op in change.ops) and not change.has_writes


@pytest.mark.parametrize("ops", REVIEWED)
def test_any_change_with_a_write_goes_on_to_its_review(ops):
    assert Change.model_validate({"ops": ops}).has_writes


def test_the_schedule_toggle_is_a_button_not_an_op():
    assert "set_schedule_enabled" not in change_ops.OPS
    assert {"schedule_on", "schedule_off"} <= {n for n, op in change_ops.OPS.items() if op.is_act}
    assert set(ACTIONS) == {n for n, op in change_ops.OPS.items() if op.is_act}


async def test_the_engine_takes_the_split_as_a_run_does(engine):
    said = json.dumps({"object": "change", "outcome": "said", "say": "A card is up."})
    review = json.dumps({"object": "change", "outcome": "review", "review": "- Rename"})
    assert await _next(engine, "step_check_route", {"change_checked": said}) == "step_change_said"
    assert await _next(engine, "step_check_route", {"change_checked": review}) == "step_manage_review"
    assert await _next(engine, "step_check_route", {"change_checked": "not json"}) == "step_change_said"
    assert await _next(engine, "step_check_route", {}) == "step_change_said"


def test_every_write_a_change_makes_is_a_write_the_role_door_knows():
    """A write the door did not know would count as one anyway (`KINDS.get(…, "write")`), but a new op must be
    classified on purpose."""
    tools = {op.tool for op in change_ops.OPS.values() if op.is_write}
    assert tools and {t for t in tools if builder_access.KINDS.get(t) != "write"} == set()


def test_the_cards_side_never_reaches_a_write():
    """Everything reachable from the check's `said` side, before the run ends: no review it skipped, no apply, no
    agent holding a write tool and no step forcing one."""
    steps, agents = _steps(), _agents()
    seen, todo = set(), [steps["step_check_route"]["switch_cases"]["said"]]
    while todo:
        sid = todo.pop()
        if not sid or sid in seen or sid == "step_end":
            continue
        seen.add(sid)
        s = steps[sid]
        todo += [s.get("next_step_id"), *(s.get("route_map") or {}).values(), s.get("if_true_step_id"),
                 s.get("if_false_step_id"), *(s.get("switch_cases") or {}).values(), s.get("switch_default_step_id")]
    assert not {"step_manage_review", "step_change_approve", "step_apply"} & seen
    for sid in seen:
        s = steps[sid]
        agent = agents.get(s.get("agent_id") or "")
        held = {t for t in (agent or {}).get("tools") or [] if builder_access.KINDS.get(t) == "write"}
        assert not held, (sid, held)
        assert builder_access.KINDS.get(s.get("forced_tool") or "") != "write", sid


def test_the_planner_plans_a_schedule_on_or_off_as_a_button():
    prompt = _agents()["agent_native_builder_planner"]["system_prompt"].split("# Changing", 1)[1]
    button = prompt.split("Button acts", 1)[1].split("\n", 1)[0]
    assert "turn a schedule on or off" in button and "they get a card to press" in button


def test_the_answer_still_puts_up_a_card_for_a_button_it_is_asked():
    """A button asked for as a question still reaches the reporter, which puts the card up; no change plan reaches
    it any more."""
    answer = _steps()["step_answer_workspace"]  # the answer that reads the workspace (SAI2-P4)
    assert "put the card up with `prepare_action`" in answer["prompt_template"]
    assert "manage_plan" not in answer["input_keys"] and "manage_plan" not in answer["prompt_template"]


@pytest.mark.parametrize("state, step, then", [
    ({"change_checked": json.dumps({"error": "Tool execution error: boom"})}, "step_check_route", "step_change_failed"),
    ({"change_approval": json.dumps({"error": "Not done: you are no longer a member"})}, "step_change_recorded",
     "step_change_failed"),
    ({"change_approval": json.dumps({"approved": 1})}, "step_change_recorded", "step_apply"),
    ({"change_applied": json.dumps({"error": "Not done: building needs the builder role"})}, "step_apply_route",
     "step_change_failed"),
    ({"change_applied": json.dumps({"object": "change_applied", "applied": ["rename_agent"], "failed": [],
                                    "effect": ""})}, "step_apply_route", "step_change_summary"),
])
async def test_a_code_step_that_answers_only_an_error_says_why(engine, state, step, then):
    """A check, an approval or an apply that the role door refused, or that raised, answers `{"error": …}` and none of
    the fields the next Print reads; it goes to the one Print that says the run's own record, which names the last
    thing that failed (`run_writes`), never a reference as text (the branch review's finding 4)."""
    assert await _next(engine, step, state) == then


def test_the_failure_print_says_the_run_s_own_record():
    failed = _steps()["step_change_failed"]
    assert failed["type"] == "print" and failed["print_content"] == "{{ state.run_changes }}"
    assert failed["output_key"] == "summary" and failed["next_step_id"] == "step_end"


def test_a_change_that_goes_round_too_often_ends_in_words():
    """Five rounds of questions or "change something" and the planner's step is spent: the run used to fail there with
    no summary ("'Plan the Change' gave up after 5 attempts"). It goes to a Print that says nothing was changed and
    how to start again (the branch review's finding 12)."""
    steps = _steps()
    planner = steps["step_plan"]
    assert planner["max_iterations"] >= 5
    spent = steps[planner["on_exhausted_step_id"]]
    assert spent["type"] == "print" and spent["output_key"] == "summary" and spent["next_step_id"] == "step_end"
    assert "Nothing was built or changed" in spent["print_content"] and "{state." not in spent["print_content"]


def test_a_new_thing_is_made_only_after_the_yes_and_only_with_the_four_create_tools():
    steps, agents = _steps(), _agents()
    assert steps["step_check_route"]["switch_cases"]["create"] == "step_manage_review"
    after = steps["step_manage_route"]["if_true_step_id"]
    assert steps[after]["type"] == "switch" and steps[after]["switch_cases"] == {"create": "step_make_new"}
    assert steps[after]["switch_default_step_id"] == "step_change_approve"
    make = steps["step_make_new"]
    maker = agents[make["agent_id"]]
    writes = {t for t in maker["tools"] if builder_access.KINDS.get(t) == "write"}
    # F1198 (BLD1, Naveen 10-29): a Python tool too, made then proved by its test.
    assert writes == {"create_agent", "create_skill", "create_http_tool", "import_openapi", "create_python_tool",
                      "test_python_tool"}
    assert make["next_step_id"] == "step_check_made"  # what was made, held to what was approved (MCP2's note 5)
    # One thing, once: the step ends at its first create that worked (E2). On Gemini 3 Flash the maker called
    # create_agent seven times for one approved agent (P2's after-batch, cnt-05) when only its prompt said "one call".
    # A Python tool's step ends at its passing test, not at its create (F1198): a failed test is an error to fix.
    assert set(make["finish_tools"]) == {"create_agent", "create_skill", "create_http_tool", "import_openapi", "cannot",
                                         "test_python_tool"}
    assert "create_python_tool" not in make["finish_tools"]
    reaching = [s["id"] for s in steps.values() if "step_make_new" in json.dumps(
        {k: s.get(k) for k in ("next_step_id", "switch_cases", "switch_default_step_id", "if_true_step_id",
                              "if_false_step_id", "route_map")})]
    assert reaching == [after]


def test_what_the_maker_made_is_checked_then_said():
    steps = _steps()
    assert steps["step_make_new"]["next_step_id"] == "step_check_made"
    check = steps["step_check_made"]
    assert check["type"] == "tool" and check["forced_tool"] == "check_made" and check["tool_arguments"] == {}
    said = steps[check["next_step_id"]]
    assert said["type"] == "print" and said["print_content"] == "{{ state.run_changes }}{{ state.change_made_check.note }}"
    assert said["output_key"] == "summary" and said["next_step_id"] == "step_end"
