"""A button's act that reaches the change route is a card, not a plan to review (F755, ASSIST1, D291).

BWA1's assist batch on dev (Nova Pro, 2026-10-17): "Pause the Monday invoices schedule", which was already off, went
to the change route; its planner, which reads nothing, drafted `set_schedule_enabled — disables …`; the person
approved; the operator found nothing to do. A plan review spent on one thing the app does with a button, and on a
change that changed nothing. Naveen: a card, no review.

The planner plans a button's act (turning an existing schedule on or off among them) as `prepare_action`. In code,
between the plan and its review, `step_manage_cards` (an if-else) sends a plan that names only buttons to `step_answer`:
`prepare_action`, or `set_schedule_enabled` on its own (the Schedules screen's toggle, which the planner on Gemini
Flash still wrote for "pause it" 5 times in 8 when told otherwise), and none of the operator's other write tools. the reporter puts each card up, and `prepare_action` resolves it against
the workspace as it is, so a schedule already off is said to be, with no card. Every other plan goes on to the review
as before. The reporter holds no write tool, so a plan read wrongly either way writes nothing nobody pressed.
"""
from __future__ import annotations

import pytest

from core import builder_access
from core.models_orchestration import Orchestration, OrchestrationRun
from core.native_builder.seed import bundled_definitions
from core.orchestration.engine import OrchestrationEngine
from core.orchestration.steps import _eval_state_expr


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


def _cards_only(plan) -> bool:
    value, error = _eval_state_expr(_steps()["step_manage_cards"]["if_condition"], {"manage_plan": plan}, default=False)
    assert error is None, error
    return value


async def _next(engine, step_id: str, state: dict) -> str | None:
    step = engine.step_map[step_id]
    run = OrchestrationRun(run_id="r", orchestration_id="orch_native_builder", shared_state=dict(state))
    async for _ in engine.executors[step.type].execute(step, run, engine):
        pass
    target, _ = engine._resolve_next(step, run)
    return target


#: How the planner writes a button's act, as its prompt asks.
CARDS = [
    '- `prepare_action — schedule_off "Monday invoices"`',
    '- `prepare_action` — schedule_on: the "Monday invoices" schedule',
    '1. `prepare_action — run_workflow "Invoice chaser"`\n2. `prepare_action — cancel_run run_8f2`',
    '⚠️ DELETES: the board "Bugs" and its 12 cards.\n- `prepare_action — board_delete "Bugs"`',
    '- `prepare_action — knowledge_delete_document "pricing.pdf" from "Support docs"`',
    '- `set_schedule_enabled — Disables the "Monday invoices" schedule`',   # what the planner wrote on dev
    '* set_schedule_enabled — Turn on the Monday report schedule',
]

#: Plans the person must review: any of the operator's writes, alone or beside a card; a read; nothing.
REVIEWED = [
    '- `update_schedule — move "Every night at 2" to 3am`\n- `prepare_action — run_schedule_now "Every night at 2"`',
    '- `create_schedule — every Monday 9am`\n- `set_schedule_enabled — turn the new schedule on`',
    '- `update_schedule — rename it "Weekly invoices"`\n- `set_schedule_enabled — pause it`',
    '⚠️ DELETES: the agent "Old bot".\n- `delete_agent — agent_old`',
    '- `list_schedules — see what runs`',
    "",
    None,
]


def test_the_gate_is_code_between_the_plan_and_its_review():
    steps = _steps()
    gate = steps["step_manage_cards"]
    assert steps["step_manage_plan"]["next_step_id"] == "step_manage_cards"
    assert gate["type"] == "if_else" and "manage_plan" in gate["input_keys"]
    assert (gate["if_true_step_id"], gate["if_false_step_id"]) == ("step_answer", "step_manage_writes")


@pytest.mark.parametrize("plan", CARDS)
def test_a_plan_of_cards_only_goes_straight_to_the_cards(plan):
    assert _cards_only(plan) is True


@pytest.mark.parametrize("plan", REVIEWED)
def test_any_other_plan_goes_on_to_its_review(plan):
    assert _cards_only(plan) is False


async def test_the_engine_takes_the_gate_as_a_run_does(engine):
    assert await _next(engine, "step_manage_cards", {"manage_plan": CARDS[0]}) == "step_answer"
    assert await _next(engine, "step_manage_cards", {"manage_plan": REVIEWED[0]}) == "step_manage_writes"
    assert await _next(engine, "step_manage_cards", {"manage_plan": CARDS[-2]}) == "step_answer"
    assert await _next(engine, "step_manage_cards", {}) == "step_manage_writes"


def test_the_gate_names_every_write_the_operator_holds():
    """The planner names the operator's tools; one the gate did not know would read as a plan of cards. A write tool
    given to the operator later fails here until the gate names it. `set_schedule_enabled` is named as a button."""
    operator = _agents()["agent_native_builder_operator"]
    writes = {t for t in operator["tools"] if builder_access.KINDS.get(t) == "write"}
    assert writes, "the operator holds no write tool?"
    condition = _steps()["step_manage_cards"]["if_condition"]
    assert {t for t in writes if f'"{t}"' not in condition} == set()


def test_the_cards_side_never_reaches_a_write():
    """Everything reachable from the gate's yes side, before the run ends: no operator, no human review it skipped,
    and no agent holding a write tool."""
    steps, agents = _steps(), _agents()
    seen, todo = set(), [_steps()["step_manage_cards"]["if_true_step_id"]]
    while todo:
        sid = todo.pop()
        if not sid or sid in seen or sid == "step_end":
            continue
        seen.add(sid)
        s = steps[sid]
        todo += [s.get("next_step_id"), *(s.get("route_map") or {}).values(), s.get("if_true_step_id"),
                 s.get("if_false_step_id")]
    assert "step_manage_apply" not in seen
    for sid in seen:
        agent = agents.get(steps[sid].get("agent_id") or "")
        held = {t for t in (agent or {}).get("tools") or [] if builder_access.KINDS.get(t) == "write"}
        assert not held, (sid, held)


def test_the_planner_plans_a_schedule_on_or_off_as_a_button():
    prompt = _steps()["step_manage_plan"]["prompt_template"]
    button = prompt.split("one thing a button does", 1)[1].split("`prepare_action", 1)[0]
    assert "turn an existing schedule on or off" in button
    assert "Plan `set_schedule_enabled` only for a schedule this same plan creates" in prompt


def test_the_answer_puts_up_the_cards_of_a_plan_that_reached_it():
    prompt = _steps()["step_answer"]["prompt_template"]
    assert "put up a card for each button it names" in prompt and "`schedule_on` or `schedule_off`" in prompt

