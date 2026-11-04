"""A reply to a build's plan never becomes a change on its own (F1384; CAP3's FF19 gate, cap-02 c3; eb 11-05).

cap-02's build showed its plan, which could not be built yet: nothing here provided its order lookup. The person
answered the review asking for that tool, and the planner finished with `submit_change`, one op making the tool.
`step_plan_finish` sent every change to the change route, so the change made the tool, its key card went up and the
run ended: the agent, its knowledge base and the workflow the person reviewed were never made, and their next yes
found nothing waiting.

While this run's build plan is under review and not yet saved, a change is sent back to the planner: their reply is
answered with the whole plan again. Twice, never a loop; after that the plan they reviewed is kept and asked about again
(`step_plan_same`), with what the change would have done said, so a yes still builds the whole plan. A change asked in
a manage turn, a change before any plan was shown, and the change route's own review revised go to the change check as
before.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import bundled_definitions, change_tools
from core.native_builder.build_plan import CHECKED_KEY, checked_in
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.orchestration.steps import _eval_state_expr
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


_N = {"n": 0}

# Neutral words, no brief's: a shipments help desk whose lookup nothing here provides yet.
MISSING = ("`track_shipment`: Shipments API: looks up a shipment by its id. Connect an app or service that provides it, "
           "or build without it.")
CARD = {"object": "plan", "outcome": "review", "digest": "d" * 64,
        "plan": {"kind": "create", "title": "Shipment Help", "steps": [{"id": "help", "type": "agent",
                                                                         "agent": "Help Agent"}],
                 "agents": [{"name": "Help Agent", "tools": ["track_shipment"]}],
                 "tools": [{"name": "track_shipment", "status": "missing", "via": "none"}]},
        "same_say": f"The plan above is the same. {MISSING}"}
REPLY = {"feedback": "add the track_shipment tool: it calls our shipments API with the shipment id"}
TOOL = {"op": "create_tool", "target": {"kind": "tool", "name": "track_shipment"},
        "fields": {"description": "Look up a shipment by its id", "method": "GET",
                   "url": "http://127.0.0.1:9/shipments/{{ shipment_id }}",
                   "input_schema": {"type": "object", "properties": {"shipment_id": {"type": "string"}},
                                    "required": ["shipment_id"]}}, "says": ""}
CHANGE = {"ops": [TOOL], "asked": [{"part": "add the track_shipment tool", "ops": [1]}]}
NIGHTLY = {"op": "schedule_off", "target": {"kind": "schedule", "name": "nightly"}, "says": "pause nightly"}


async def _run(**state):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_kept_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, **state})
    await SharedState(run).checkpoint()
    return run


async def _under_review(**more):
    """This run's build plan, shown for review and answered: cap-02's run at its reply."""
    return await _run(intent="create", **{CHECKED_KEY: json.dumps(CARD)}, review_feedback=REPLY, **more)


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


def _steps() -> dict:
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


def _goes_to(step: str, state: dict) -> str:
    route = _steps()[step]
    value, _ = _eval_state_expr(route["switch_expression"], state, default=None)
    return route["switch_cases"].get(str(value), route["switch_default_step_id"])


class TestAChangeWhileThePlanIsUnderReview:
    async def test_it_is_sent_back_for_the_whole_plan(self, client):
        out, _ = await _call("submit_change", CHANGE, await _under_review())
        assert "finish" not in out, out
        assert "submit_plan" in out["error"] and "the whole plan" in out["error"], out

    async def test_sent_back_twice_then_the_reviewed_plan_is_kept(self, client):
        run = await _under_review()
        for _ in range(change_tools.PLAN_SEND_BACKS):
            assert "error" in (await _call("submit_change", CHANGE, run))[0]
        out, _ = await _call("submit_change", CHANGE, run)
        assert out.get("finish") == "submit_change" and out.get("kept_plan"), out
        assert change_tools.PLAN_SEND_BACKS == 2
        card = checked_in(run.shared_state)
        # The plan and what its yes covers are as reviewed; what is asked again says what the change would have done,
        # and what still stands in the plan's way.
        assert card["plan"] == CARD["plan"] and card["digest"] == CARD["digest"] and card["outcome"] == "review"
        assert '"track_shipment"' in card["same_say"] and MISSING in card["same_say"], card["same_say"]
        assert card["same_say"].startswith(change_tools.KEPT_PLAN.split("{")[0]), card["same_say"]

    async def test_a_save_sent_back_after_the_yes_is_still_the_plans(self, client):
        """Approved and not yet saved (a refused save or a connection going back to the planner): the yes was for the
        whole plan, so a change alone is sent back too."""
        run = await _under_review(plan_approval={"approved": True}, build_saved={"outcome": "replan"})
        out, _ = await _call("submit_change", CHANGE, run)
        assert "submit_plan" in out.get("error", ""), out

    def test_the_kept_plan_is_asked_about_again_and_its_yes_builds_it(self):
        state = {"intent": "create", "plan_draft": {"finish": "submit_change", "kept_plan": "kept"}}
        assert _goes_to("step_plan_finish", state) == "step_plan_same"
        same = _steps()["step_plan_same"]
        assert same["human_prompt"] == "{{ state.plan_checked.same_say }}"
        assert same["next_step_id"] == "step_plan_answer", "its answer is read as the review's, on the plan as checked"


class TestAChangeGoesToItsCheckAsBefore:
    async def test_in_a_manage_turn_after_a_build_was_saved(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]},
                             await _run(intent="manage", last_built_orchestration_id="orch_built"))
        assert out.get("finish") == "submit_change" and "kept_plan" not in out, out
        assert _goes_to("step_plan_finish", {"intent": "manage", "plan_draft": out}) == "step_check_change"

    async def test_before_any_plan_was_shown(self, client):
        """Asked as a build, it is a change to what is here: the planner's first finish."""
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]}, await _run(intent="create"))
        assert out.get("finish") == "submit_change" and "kept_plan" not in out, out
        assert _goes_to("step_plan_finish", {"intent": "create", "plan_draft": out}) == "step_check_change"

    async def test_the_change_routes_own_review_revised(self, client):
        """REACH1's case: a change card under review that the person revises comes back as a change."""
        card = {"object": "change", "outcome": "review", "ops": [NIGHTLY], "calls": []}
        for intent in ("manage", "create"):
            run = await _run(intent=intent, change_checked=json.dumps(card),
                             manage_review={"feedback": "the weekly one too"})
            out, _ = await _call("submit_change", {"ops": [NIGHTLY]}, run)
            assert out.get("finish") == "submit_change" and "kept_plan" not in out, (intent, out)

    async def test_once_the_build_is_saved(self, client):
        run = await _under_review(plan_approval={"approved": True}, build_saved={"outcome": "saved"})
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]}, run)
        assert out.get("finish") == "submit_change" and "kept_plan" not in out, out
