"""A build's plan code checked but not yet shown is never left for a change that makes one part (F1431; CAP3's FF19
gate, cap-02 c1; was F1389).

cap-02's person asked for a tool and an agent. The planner's plan went to the check, which sent it to research (the
card carries the plan, outcome "research"); after the lookup and research the planner finished with `submit_change`,
one op making the tool. F1384's guard only knew a plan shown for review, so the change went to the change route: it
made the tool, its key card went up and the run ended, the agent and the workflow never made and the plan never
shown. Their next yes found nothing waiting.

While this run's build plan is checked, unsaved and not yet shown (research, prompts), a change is sent back to the
planner for the whole plan: twice, never a loop; after that the plan last submitted goes to its check again
(`submit_plan`, `step_plan_finish`'s default), once a run, so the person sees the whole plan at its review. A plan
shown for review is F1384's, as before; a change with no plan checked, in a manage turn or once the build is saved
goes to the change check as before.

And a yes with nothing waiting never says "The last thing I did: Nothing is waiting for a yes."
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.events_public import public_state
from core.models_orchestration import OrchestrationRun
from core.native_builder import bundled_definitions, change_tools
from core.native_builder.build_plan import CHECKED_KEY, checked_in
from core.native_builder.read_turn import decide
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

#: The engine's own keys this keeps (`build_plan.SUBMITTED_KEY`, `change_tools._UNSHOWN_SENT`), named as
#: `events_public.PRIVATE_STATE_PREFIXES` names them.
SUBMITTED_KEY = "_plan_submitted"
UNSHOWN_SENT = "_plan_unshown_sent_back"
#: What the planner is told: the plan is not yet shown, and the whole plan is its finish.
UNSHOWN_WORDS = ("hasn't seen it", "submit_plan", "the whole plan", "never a change on its own")


def _unshown_said(out: dict) -> bool:
    return "finish" not in out and all(w in str(out.get("error") or "") for w in UNSHOWN_WORDS)

# Neutral words, no brief's: a shipments help desk whose lookup nothing here provides yet.
PLAN = {"kind": "create", "title": "Shipment Help", "steps": [{"id": "help", "type": "agent", "agent": "Help Agent"}],
        "agents": [{"name": "Help Agent", "tools": ["track_shipment"]}],
        "tools": [{"name": "track_shipment", "status": "missing", "via": "none"}]}
RESEARCH = {"object": "plan", "outcome": "research", "research": ["track_shipment"], "plan": PLAN}
PROMPTS = {"object": "plan", "outcome": "prompts", "research": ["track_shipment"], "plan": PLAN}
#: What the planner last submitted, as `submit_plan` returned it.
SUBMITTED = {"finish": "submit_plan", "title": "Shipment Help", "steps": [{"id": "help", "type": "agent",
                                                                          "agent": "Help Agent"}]}
TOOL = {"op": "create_tool", "target": {"kind": "tool", "name": "track_shipment"},
        "fields": {"description": "Look up a shipment by its id", "method": "GET",
                   "url": "http://127.0.0.1:9/shipments/{{ shipment_id }}",
                   "input_schema": {"type": "object", "properties": {"shipment_id": {"type": "string"}},
                                    "required": ["shipment_id"]}}, "says": ""}
CHANGE = {"ops": [TOOL], "asked": [{"part": "the track_shipment tool", "ops": [1]}]}
NIGHTLY = {"op": "schedule_off", "target": {"kind": "schedule", "name": "nightly"}, "says": "pause nightly"}


async def _run(**state):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_unshown_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, **state})
    await SharedState(run).checkpoint()
    return run


async def _unshown(card=RESEARCH, **more):
    """This run's build plan, checked and not yet shown: cap-02 c1's run after its research."""
    return await _run(intent="create", **{CHECKED_KEY: json.dumps(card), SUBMITTED_KEY: SUBMITTED}, **more)


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


def _goes_to(step: str, state: dict) -> str:
    route = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}[step]
    value, _ = _eval_state_expr(route["switch_expression"], state, default=None)
    return route["switch_cases"].get(str(value), route["switch_default_step_id"])


class TestAChangeWhileThePlanIsNotYetShown:
    async def test_it_is_sent_back_for_the_whole_plan(self, client):
        out, _ = await _call("submit_change", CHANGE, await _unshown())
        assert _unshown_said(out), out

    @pytest.mark.parametrize("card", [RESEARCH, PROMPTS], ids=["research", "prompts"])
    async def test_sent_back_twice_then_the_plan_goes_to_its_check(self, client, card):
        run = await _unshown(card)
        for _ in range(change_tools.PLAN_SEND_BACKS):
            out, _ = await _call("submit_change", CHANGE, run)
            assert _unshown_said(out), out
        out, _ = await _call("submit_change", CHANGE, run)
        assert out == SUBMITTED, out
        assert _goes_to("step_plan_finish", {"intent": "create", "plan_draft": out}) == "step_check_plan"
        # The plan as checked is untouched: its check carries what research found, so it is not researched again.
        assert checked_in(run.shared_state) == card

    async def test_the_plan_goes_to_its_check_once_a_run(self, client):
        run = await _unshown()
        for _ in range(change_tools.PLAN_SEND_BACKS):
            await _call("submit_change", CHANGE, run)
        assert (await _call("submit_change", CHANGE, run))[0] == SUBMITTED
        for _ in range(2):
            out, _ = await _call("submit_change", CHANGE, run)
            assert _unshown_said(out), out

    async def test_a_shown_plans_send_backs_are_its_own(self, client):
        """F1384's budget for the plan once shown is not spent by the sends before it was."""
        run = await _unshown()
        for _ in range(change_tools.PLAN_SEND_BACKS + 1):
            await _call("submit_change", CHANGE, run)
        shown = {**RESEARCH, "outcome": "review", "digest": "d" * 64, "same_say": "The plan above is the same."}
        run.shared_state[CHECKED_KEY] = json.dumps(shown)
        out, _ = await _call("submit_change", CHANGE, run)
        assert out.get("error") == change_tools.PLAN_UNDER_REVIEW, out

    async def test_what_it_keeps_is_the_engines(self, client):
        run = await _unshown()
        await _call("submit_change", CHANGE, run)
        assert SUBMITTED_KEY in run.shared_state and UNSHOWN_SENT in run.shared_state
        public = public_state(run.shared_state)
        assert SUBMITTED_KEY not in public and UNSHOWN_SENT not in public


class TestAChangeGoesToItsCheckAsBefore:
    async def test_with_no_plan_checked(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]}, await _run(intent="create"))
        assert out.get("finish") == "submit_change" and "kept_plan" not in out, out

    async def test_after_a_question_with_no_plan(self, client):
        """A question before any plan (a form, a model, a period) carries no plan: nothing is left unbuilt."""
        ask = {"object": "plan", "outcome": "ask", "question": "Which region?", "plan": None}
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]},
                             await _run(intent="create", **{CHECKED_KEY: json.dumps(ask)}))
        assert out.get("finish") == "submit_change", out

    async def test_in_a_manage_turn(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]},
                             await _run(intent="manage", **{CHECKED_KEY: json.dumps(RESEARCH)}))
        assert out.get("finish") == "submit_change", out

    async def test_once_the_build_is_saved(self, client):
        out, _ = await _call("submit_change", {"ops": [NIGHTLY]}, await _unshown(build_saved={"outcome": "saved"}))
        assert out.get("finish") == "submit_change", out


class TestNothingWaitingNeverQuotesItself:
    def test_it_says_again_what_was_done_last(self):
        said = "Nothing is waiting for a yes. The last thing I did: Turned off the schedule Nightly invoices."
        turn = decide("yes", previous={"summary": said}, facts={})
        assert (turn.route, turn.then) == ("yes_without_review", "said")
        assert turn.say == said

    def test_a_yes_to_its_question_goes_on_as_before(self):
        """"What would you like me to do?" is a question the yes answers: classify reads it, as any question's."""
        turn = decide("yes", previous={"summary": "Nothing is waiting for a yes. What would you like me to do?"},
                      facts={})
        assert turn.route == "classify"

    def test_after_its_own_bare_words_it_asks_what_to_do(self):
        turn = decide("approve", previous={"summary": "Nothing is waiting for a yes."}, facts={})
        assert turn.say == "Nothing is waiting for a yes. What would you like me to do?"

    def test_an_ordinary_last_reply_is_said_as_before(self):
        turn = decide("approve", previous={"summary": "Turned off the schedule Nightly invoices. It runs no more."},
                      facts={})
        assert turn.say == "Nothing is waiting for a yes. The last thing I did: Turned off the schedule Nightly invoices."
