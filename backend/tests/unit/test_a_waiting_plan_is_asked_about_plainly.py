"""While a plan waits at its review, classify must say whether the message changes it (F1241's follow-up; 8a 10-30).

F1241 carries a waiting plan only on classify's own `continues=True`, and `continues` was optional. CAP3's probe E
asked prod's quick model (nova-pro) to classify Naveen's turn 2, "lets make it automatically post in twitter too", at
a waiting plan three times: it said `change`, `change`, `build`, and never said `continues` (0/3). So on prod the carry
never fired, and every such turn got "Your plan above is still waiting".

* `read_turn` names the waiting plan (`plan_waiting`, its title), and the door `step_classify_door` sends that turn to
  `step_classify_at_plan`, whose one call, `classify_at_plan`, requires `continues` (a boolean, never null) and whose
  prompt names the plan and asks plainly. With no plan waiting the turn goes to `step_classify` as before, and
  `classify_request` keeps `continues` optional.
* The safe default holds: a call that still leaves `continues` out, or says null, is read as unsure, so the plan is
  not carried and the notice says it waits.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import builder_tools  # noqa: F401 — the builder's tool modules load in their own order first
from core.native_builder import change_tools as T
from core.native_builder import classify as C
from core.native_builder.build_plan import CHECKED_KEY
from core.orchestration.steps import _route_expr
from tests.unit.test_a_plan_waiting_at_its_review_is_changed_not_lost import CARD, PLAN, TWITTER, _staged
from tests.unit.test_the_change_route_decides_in_code import _call, _run

ORCH = json.loads((Path(T.__file__).parent / "orchestration.json").read_text())
STEPS = {s["id"]: s for s in ORCH["steps"]}


def _params(schema: dict) -> dict:
    return schema["function"]["parameters"]


# ── the schema ───────────────────────────────────────────────────────────────────────────────────────────────────
class TestTheSchema:
    def test_at_a_waiting_plan_continues_is_required_and_a_boolean(self):
        params = _params(C.SCHEMA_AT_PLAN)
        assert "continues" in params["required"] and "kind" in params["required"]
        assert params["properties"]["continues"]["type"] == "boolean", "true or false, never null"
        assert "anyOf" not in params["properties"]["continues"]

    def test_with_no_plan_waiting_it_stays_optional(self):
        params = _params(C.SCHEMA)
        assert "continues" not in params.get("required", [])

    def test_the_two_calls_differ_only_in_continues(self):
        at, plain = _params(C.SCHEMA_AT_PLAN), _params(C.SCHEMA)
        assert set(at["properties"]) == set(plain["properties"])
        assert {k: v for k, v in at["properties"].items() if k != "continues"} == \
            {k: v for k, v in plain["properties"].items() if k != "continues"}

    def test_the_question_is_asked_plainly(self):
        said = _params(C.SCHEMA_AT_PLAN)["properties"]["continues"]["description"]
        assert said == ("Does this message change or add to the waiting plan (true), or ask for something else "
                        "(false)?")


# ── the door ─────────────────────────────────────────────────────────────────────────────────────────────────────
class TestTheDoor:
    def test_the_turn_goes_through_the_door(self):
        route = STEPS["step_turn_route"]
        assert route["switch_cases"]["classify"] == "step_classify_door"
        assert route["switch_default_step_id"] == "step_classify_door"

    @pytest.mark.parametrize("turn, at_plan", [({"plan_waiting": CARD["title"]}, True), ({"plan_waiting": ""}, False),
                                               ({}, False)])
    def test_a_waiting_plan_takes_the_call_that_must_say(self, turn, at_plan):
        door = STEPS["step_classify_door"]
        raw, _ = _route_expr(door["if_condition"], {"turn": turn}, "The If/Else condition", default=False)
        assert bool(raw) is at_plan
        assert (door["if_true_step_id"], door["if_false_step_id"]) == ("step_classify_at_plan", "step_classify")

    def test_the_at_plan_step_names_the_plan_and_finishes_on_its_call(self):
        step, plain = STEPS["step_classify_at_plan"], STEPS["step_classify"]
        assert step["finish_tools"] == ["classify_at_plan"] and plain["finish_tools"] == ["classify_request"]
        assert "{{ state.turn.plan_waiting }}" in step["prompt_template"], "the one form (F1252)"
        assert "Does this message change or add to the waiting plan (true)" in step["prompt_template"]
        assert step["next_step_id"] == plain["next_step_id"] == "step_classify_route"
        assert (step["output_key"], step["model_role"], step["finish_fallback"]) == \
            (plain["output_key"], plain["model_role"], plain["finish_fallback"])

    def test_the_at_plan_agent_holds_only_its_call(self):
        agent = json.loads((Path(T.__file__).parent / "agents" / "classifier_at_plan.json").read_text())
        assert agent["id"] == STEPS["step_classify_at_plan"]["agent_id"]
        assert agent["tools"] == ["classify_at_plan"]


# ── read_turn names the plan ─────────────────────────────────────────────────────────────────────────────────────
class TestThePlanIsNamed:
    def test_a_waiting_plan_is_named_by_its_title(self):
        assert T.waiting_plan_title(_staged()) == CARD["title"]

    def test_no_plan_waiting_names_none(self):
        assert T.waiting_plan_title(_staged(history=[{"step_id": "step_build_done"}])) == ""
        assert T.waiting_plan_title({}) == ""

    def test_a_plan_with_no_title_is_still_named(self):
        untitled = {**PLAN, CHECKED_KEY: json.dumps({"outcome": "review", "plan": {"title": ""}}), "plan_draft": {}}
        assert T.waiting_plan_title(_staged(previous=untitled)) == "the plan above"


# ── the call ─────────────────────────────────────────────────────────────────────────────────────────────────────
async def _at_plan(run_id: str, request: dict):
    run = await _run(run_id=run_id, user_message=TWITTER, may_change=True, may_act=True, **_staged())
    out, events = await _call("classify_at_plan", request, run)
    return out, [e.get("message") for e in events if e.get("type") == "builder_notice"], run


class TestTheCallAtAWaitingPlan:
    async def test_naveen_s_turn_two_said_as_asked_changes_the_plan(self, client):
        """Naveen's turn-2 shape: the kind nova-pro gave (`change`), and now the yes it must give."""
        out, notices, run = await _at_plan("builder_f1241f_a", {"kind": "change", "continues": True})
        assert (out["kind"], out["route"], out["overridden_by"]) == ("build", T.REVISE_ROUTE, "waiting_plan")
        assert run.shared_state["review_feedback"] == {"feedback": TWITTER}
        assert T.TAKEN_AS_A_CHANGE in notices

    async def test_a_no_starts_afresh_and_says_nothing(self, client):
        out, notices, run = await _at_plan("builder_f1241f_b", {"kind": "build", "continues": False})
        assert out["route"] != T.REVISE_ROUTE and CHECKED_KEY not in run.shared_state and not notices

    @pytest.mark.parametrize("request_", [{"kind": "change"}, {"kind": "build", "continues": None}],
                             ids=["left_out", "null"])
    async def test_still_unsaid_is_never_carried_and_says_the_plan_waits(self, client, request_):
        out, notices, run = await _at_plan(f"builder_f1241f_{request_.get('kind')}_{len(request_)}", request_)
        assert out["route"] != T.REVISE_ROUTE and CHECKED_KEY not in run.shared_state
        assert T.STILL_WAITING in notices and run.shared_state[T.PARKED_PLAN_KEY]["run_id"] == "builder_turn1"
