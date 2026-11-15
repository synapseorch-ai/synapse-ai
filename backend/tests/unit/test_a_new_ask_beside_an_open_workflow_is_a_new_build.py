"""A new ask typed beside an open workflow is a new request, never an edit of it, and nothing ends in silence (F1605).

A browser test of the panel: after O6 built "Customer Refund Approval", F1577 took the person to it, and
two new asks typed in the panel there, each in a new conversation, ended with one line: "Nothing in your workspace
changed." O7: "Cada lunes, resume los correos de clientes de la semana y envíamelo." O9: "Make a workflow that translates
a short document into French and Spanish.", then "run it", which offered to run the invoice workflow on screen. With
nothing named, the open workflow always won (`target.resolve_target`'s VIEWING), and "this" and "it" meant it.

Now:
- Beside an open workflow, a message that neither refers to it ("this", "it", "its", its name) nor starts as an edit of it
  ("add …", "change …", "also …", "remove …", "make it …") is a new request, whatever the quick model labelled it.
  An edit of the open one stays one, trigger words and all. Each way has its false-positive fixture.
- With nothing named, a workflow this conversation built and a different one open is asked about (AMBIGUOUS), once.
- "run it" or "test it" right after an ask that built nothing refers to that ask: code says nothing has been built for it
  yet and offers to build it, and never runs another workflow. Only with no ask of its own does it fall back to the
  workflow on screen, by name.
- `run_writes.render` never says "Nothing in your workspace changed." alone: a reason always follows it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.native_builder import read_turn
from core.native_builder.classify import override
from core.native_builder.objects import Request
from core.native_builder.target import AMBIGUOUS, BUILT, VIEWING, resolve_target
from core.orchestration import run_writes

INVENTORY = {"schedule": ["Nightly invoices"], "agent": ["Front desk"], "orchestration": ["Customer Refund Approval"]}
OPEN = {"may_change": True, "may_act": True, "current_orchestration_source": "viewing",
        "current_orchestration_id": "orch_refund", "current_orchestration_name": "Customer Refund Approval"}
O7 = "Cada lunes, resume los correos de clientes de la semana y envíamelo."
O9 = "Make a workflow that translates a short document into French and Spanish."


def _route(message: str, kind: str):
    return override(Request(kind=kind, needs="workspace"), message=message, state=dict(OPEN), inventory=INVENTORY,
                    connectable=set())


class TestANewAskIsNew:
    @pytest.mark.parametrize("kind", ["change", "build"])
    @pytest.mark.parametrize("message", [O9, O7, "Every morning, send me a summary of our sales.",
                                         "Watch our website every hour and tell me if it goes down."])
    def test_beside_an_open_workflow_it_is_planned_new(self, message, kind):
        routed = _route(message, kind)
        assert routed.kind == "build" and routed.route == "step_plan_room", (message, kind, routed.route)


class TestAnEditOfTheOpenOneStaysOne:
    """False-positive fixtures: an edit stays an edit, a trigger word in it or not."""

    @pytest.mark.parametrize("message", ["add a Slack step that posts the summary every Monday",
                                         "change its schedule to daily", "also email it to finance",
                                         "make it run every Friday instead"])
    def test_it_goes_to_the_open_workflow(self, message):
        """Its edit (step_fetch_existing) or its change (the change route: a schedule is one), never a new build."""
        routed = _route(message, "build")
        assert routed.route in ("step_fetch_existing", "step_change_context"), (message, routed.route)

    @pytest.mark.parametrize("message", ["change its schedule to daily", "also email it to finance"])
    def test_as_a_change_it_stays_a_change(self, message):
        assert _route(message, "change").kind == "change"


ROWS = [{"id": "orch_refund", "name": "Customer Refund Approval"}, {"id": "orch_translate", "name": "Translator"}]


class TestWhatTheConversationBuiltAndWhatIsOpen:
    def test_both_different_and_nothing_named_is_asked(self):
        target = resolve_target("add a step that emails me", "orch_refund", ROWS, built_id="orch_translate")
        assert target.source == AMBIGUOUS and len(target.matches) == 2

    def test_the_same_one_is_simply_it(self):
        target = resolve_target("add a step that emails me", "orch_refund", ROWS, built_id="orch_refund")
        assert target.source == VIEWING

    def test_with_nothing_open_it_is_the_conversations_own(self):
        assert resolve_target("add a step", None, ROWS, built_id="orch_translate").source == BUILT


#: The previous turn: O9's translation ask, which built nothing (it ended "Nothing in your workspace changed.").
UNBUILT = {"user_message": O9, "request": {"kind": "build"}, "intent": "create", "_run_writes": []}


class TestRunItAfterAnAskThatBuiltNothing:
    @pytest.mark.parametrize("message", ["run it", "Run it", "test it", "run it now"])
    def test_it_is_that_ask_said_back_never_another_workflow(self, message):
        turn = read_turn.decide(message, previous=dict(UNBUILT), facts={})
        assert turn.then == "said"
        assert "translates a short document" in turn.say and "built" in turn.say
        assert "Customer Refund Approval" not in turn.say and "Supplier Invoice" not in turn.say

    def test_after_an_answer_turn_it_is_not_this_rule(self):
        """T2's "Approval" then "run": the turn before was an answer, not an ask, so the run card's rule is unchanged."""
        turn = read_turn.decide("run", previous={"user_message": "Approval", "request": {"kind": "answer"},
                                                 "intent": "answer", "_run_writes": []}, facts={})
        assert turn.route != "run_unbuilt"

    def test_after_a_build_that_built_it_is_not_this_rule(self):
        built = {**UNBUILT, "_run_writes": [{"kind": "orchestration", "action": "created", "id": "orch_translate",
                                             "name": "Translator"}]}
        assert read_turn.decide("run it", previous=built, facts={}).route != "run_unbuilt"


class TestNothingEndsInSilence:
    def test_render_never_says_nothing_changed_alone(self):
        said = run_writes.render([])
        assert said.startswith(run_writes.NOTHING_CHANGED) and said.strip() != run_writes.NOTHING_CHANGED

    def test_render_says_the_routes_own_reason(self):
        said = run_writes.render([], why="It read your message as a change to \"Customer Refund Approval\".")
        assert 'a change to "Customer Refund Approval"' in said

    def test_the_runs_starting_words_are_not_the_bare_line(self):
        root = Path(__file__).resolve().parents[2] / "core" / "native_builder"
        state = json.loads((root / "orchestration.json").read_text()).get("state_schema") or {}
        default = json.dumps(state.get("run_changes", ""))
        assert run_writes.NOTHING_CHANGED + '"' not in default


class TestAnyLanguageIsAskedNeverAChange:
    """the job-shape words are a fast path; when they can't decide, in any language, the message is asked
    about once ("Change X, or build something new?"), within F1600's cap, and never goes silently to the change route."""

    BENGALI = "প্রতি সোমবার গ্রাহকদের ইমেইলের সারাংশ আমাকে পাঠাও"
    JAPANESE = "毎朝、売上の要約を送ってください"

    @pytest.mark.parametrize("message", [BENGALI, JAPANESE])
    @pytest.mark.parametrize("kind", ["change", "build"])
    def test_a_non_latin_new_job_is_asked_about_never_changed(self, message, kind):
        routed = _route(message, kind)
        assert routed.route == "step_turn_said" and routed.overridden_by == "open_or_new", (kind, routed.route)

    def test_the_ask_names_the_open_workflow_and_a_new_build(self):
        from core.native_builder.classify import open_or_new_said

        said = open_or_new_said("Customer Refund Approval")
        assert '"Customer Refund Approval"' in said and "something new" in said

    def test_talk_is_never_asked_it(self):
        assert _route("hello there", "answer").route == "step_answer"

    def test_past_the_cap_it_is_built_new(self):
        from core.native_builder.change_tools import QUESTIONS, ROUNDS_KEY

        routed = override(Request(kind="change", needs="workspace"), message=self.JAPANESE,
                          state={**OPEN, ROUNDS_KEY: QUESTIONS}, inventory=INVENTORY, connectable=set())
        assert routed.kind == "build" and routed.route == "step_plan_room"

    def test_a_new_answers_the_ask_with_a_new_build(self):
        turn = read_turn.decide("new", previous={"offered_build": {"fact": "", "ask": self.JAPANESE}}, facts={})
        assert turn.route == "offer_taken" and turn.message == self.JAPANESE
