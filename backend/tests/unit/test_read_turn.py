"""Code reads each turn before any model (SAI2-P4, F978; DESIGN.md "The new definition", P4-PROMPT §1).

`read_turn` reads the previous turn of this conversation (its state and its own card, F977) and decides, in code:

* **next_request:** the last turn queued a second request and this is a yes: that request is the message now.
* **pick:** "1", "the second one", a name or an id from the last "which one?" list. An act's list puts that card up
  with no model (F760's class: Nova Pro 0/3, gpt-oss looping on "1"); a change's list goes back to the change route
  with the chosen id.
* **card_label:** a typed button label ("Run now") answers "press it on the card above", never a second card
  (as-act-03).
* **pressed:** "done" after a press says what the press did; after no press, that nothing has been pressed yet
  (P2's after-pass: "not executed yet").
* **yes_without_review:** "approve" with nothing waiting says so, and what was done last (F959's third part). A yes
  after findings is F766's and goes on to classify; a yes to a question the last reply asked goes on to classify.
* Otherwise **classify**.
"""
from __future__ import annotations

import pytest

from core.native_builder.read_turn import decide, pick_from

RUN_CARD = {"title": 'Run "Invoice chaser"',
            "buttons": [{"label": "Run now", "tool": "run_orchestration",
                         "arguments": {"orchestration_id": "orch_inv"}}]}
ACT_PICK = {"action": "run_workflow", "field": "workflow", "arguments": {"message": "March"},
            "options": [{"id": "orch_first", "name": "C2 sched print"}, {"id": "orch_second", "name": "C2 sched print"},
                        {"id": "orch_eu", "name": "Invoice chaser (EU)"}]}
CHANGE = {"change": {"question": "Which one?", "picks": [
    {"op": 0, "field": "target", "kind": "agent", "wanted": "Support",
     "options": [{"id": "agt_1", "name": "Support"}, {"id": "agt_2", "name": "Support agent for billing"}]}]}}


class TestAPick:
    @pytest.mark.parametrize("said,chosen", [
        ("1", "orch_first"), ("2", "orch_second"), ("the second one", "orch_second"), ("#3", "orch_eu"),
        ("3.", "orch_eu"), ("last", "orch_eu"), ("orch_second", "orch_second"),
        ('"C2 sched print" (orch_second)', "orch_second"), ("invoice chaser (eu)", "orch_eu"),
        ("the third", "orch_eu"),
    ])
    def test_a_choice_from_the_list(self, said, chosen):
        assert pick_from(said, ACT_PICK["options"])["id"] == chosen

    @pytest.mark.parametrize("said", ["C2 sched print", "4", "add 2 steps", "run the second workflow tomorrow",
                                      "what are these?", ""])
    def test_what_is_not_one_choice_is_no_pick(self, said):
        assert pick_from(said, ACT_PICK["options"]) is None

    def test_an_acts_pick_puts_its_card_up(self):
        turn = decide("2", previous={"summary": "Which one?"}, facts={"picks": [ACT_PICK]})
        assert (turn.route, turn.then) == ("pick", "said")
        assert turn.picked == {"action": "run_workflow", "arguments": {"message": "March", "workflow": "orch_second"},
                               "id": "orch_second", "name": "C2 sched print"}

    def test_a_changes_pick_goes_back_to_the_change_route_with_the_id(self):
        turn = decide("the first one", previous={}, facts={}, change=CHANGE)
        assert (turn.route, turn.then) == ("pick", "change")
        assert turn.picked == {"op": 0, "field": "target", "kind": "agent", "id": "agt_1", "name": "Support"}

    def test_a_new_request_while_a_list_waits_is_a_new_request(self):
        assert decide("show me today's runs", previous={}, facts={"picks": [ACT_PICK]}).route == "classify"

    def test_a_yes_to_a_list_asks_which_again(self):
        turn = decide("yes", previous={}, facts={"picks": [ACT_PICK]})
        assert turn.then == "said" and "which one" in turn.say.lower() and "orch_second" in turn.say


class TestACardsLabel:
    def test_a_typed_label_points_at_the_card(self):
        turn = decide("Run now", previous={}, facts={"cards": [RUN_CARD]})
        assert (turn.route, turn.then) == ("card_label", "said")
        assert turn.say.startswith('Press "Run now" on the card above')
        assert "nothing happens until" in turn.say.lower()

    def test_a_label_already_pressed_says_what_it_did(self):
        facts = {"cards": [RUN_CARD], "presses": [{"card": RUN_CARD["title"], "label": "Run now",
                                                   "tool": "run_orchestration", "done": True, "say": "Started."}]}
        turn = decide("run now!", previous={}, facts=facts)
        assert turn.route == "card_label" and "already pressed" in turn.say.lower() and "Started." in turn.say


class TestAPress:
    def test_done_after_a_press_says_what_it_did(self):
        facts = {"cards": [RUN_CARD], "presses": [{"card": RUN_CARD["title"], "label": "Run now",
                                                   "tool": "run_orchestration", "done": True, "say": "Run started."}]}
        turn = decide("done", previous={}, facts=facts)
        assert (turn.route, turn.then) == ("pressed", "said")
        assert "Run now" in turn.say and "Run started." in turn.say

    def test_a_refused_press_is_said_as_refused(self):
        facts = {"cards": [RUN_CARD], "presses": [{"card": RUN_CARD["title"], "label": "Run now", "done": False,
                                                   "tool": "run_orchestration", "say": "This card has expired."}]}
        turn = decide("I pressed it", previous={}, facts=facts)
        assert "didn't go through" in turn.say and "expired" in turn.say

    def test_done_with_nothing_pressed_says_the_card_waits(self):
        turn = decide("done", previous={}, facts={"cards": [RUN_CARD]})
        assert turn.route == "pressed" and "hasn't been pressed" in turn.say and "Run now" in turn.say


class TestAYesWithNothingWaiting:
    def test_it_says_nothing_waits_and_what_was_done_last(self):
        turn = decide("approve", previous={"summary": "I turned off the schedule Nightly invoices. Anything else?\n"},
                      facts={})
        # A question the reply asked is answered by the yes: that goes on to classify.
        assert turn.route == "classify"
        turn = decide("approve", previous={"summary": "Turned off the schedule Nightly invoices."}, facts={})
        assert (turn.route, turn.then) == ("yes_without_review", "said")
        assert turn.say.startswith("Nothing is waiting for a yes.")
        assert "Turned off the schedule Nightly invoices." in turn.say

    def test_with_no_earlier_turn(self):
        turn = decide("yes", previous={}, facts={})
        assert turn.route == "yes_without_review" and "what would you like" in turn.say.lower()

    def test_a_yes_after_findings_goes_on_to_the_build(self):
        """F766: a yes to a findings card is a build; classify's override sends it there through the role door."""
        found = {"options": [{"provider": "Stripe"}]}
        assert decide("yes", previous={"summary": "Stripe has an MCP server."}, facts={},
                      prior_findings=found).route == "classify"


class TestTheQueuedRequest:
    def test_a_yes_takes_the_queued_request(self):
        turn = decide("yes", previous={"summary": "Done."}, facts={"also": "pause the nightly digest"})
        assert (turn.route, turn.then, turn.message) == ("next_request", "classify", "pause the nightly digest")

    @pytest.mark.parametrize("said", ["do that", "go ahead", "sure", "yes please"])
    def test_other_ways_of_saying_go_on(self, said):
        assert decide(said, previous={}, facts={"also": "pause it"}).route == "next_request"

    def test_something_else_leaves_it_queued(self):
        assert decide("what failed today?", previous={}, facts={"also": "pause it"}).route == "classify"


class TestEverythingElse:
    @pytest.mark.parametrize("said", ["hi", "thanks!", "build me a weekly report", "what can you do?"])
    def test_goes_to_classify(self, said):
        turn = decide(said, previous={"summary": "Hello."}, facts={"cards": [RUN_CARD]})
        assert (turn.route, turn.then) == ("classify", "classify") and turn.say is None


class TestTheStep:
    """`read_turn` as `step_turn` calls it: the previous turn of *this* conversation, read from the store."""

    @pytest.fixture(autouse=True)
    async def _ready(self):
        from core import builder_access
        from core.product_tools import confirm
        from core.store import get_store

        await get_store()
        confirm.set_claim_store(None)
        builder_access.forget_runs()
        yield
        builder_access.forget_runs()

    async def _turn(self, run_id, session_id, state, *, live=False):
        from core import builder_access
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState

        run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=session_id,
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, **state})
        await SharedState(run).checkpoint()
        return run

    async def _read(self, run):
        import json

        from core.builder_tools import execute_builder_tool_with_events
        from core.orchestration.engine import live

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        with live(run):
            raw, events = await execute_builder_tool_with_events("read_turn", {}, _Server(), run_id=run.run_id)
        return json.loads(raw), events

    async def test_two_then_the_second_ones_card_goes_up_with_no_model(self):
        from core.boards import assistant
        from core.store.resources import save_orchestration
        from tests._fakes.seed import make_orchestration

        for oid in ("orch_first", "orch_second"):
            await save_orchestration(make_orchestration(id=oid, name="C2 sched print"))
        await self._turn("builder_prev", "builder:t:panel:s1", {"user_message": "run C2 sched print",
                                                                "summary": "Which one?"})
        await assistant.note("builder_prev", owner_id=None, picks=[{
            "action": "run_workflow", "field": "workflow", "arguments": {"message": "x"},
            "options": [{"id": "orch_first", "name": "C2 sched print"},
                        {"id": "orch_second", "name": "C2 sched print"}]}])
        now = await self._turn("builder_now", "builder:t:panel:s1", {"user_message": "2"})

        turn, events = await self._read(now)
        assert (turn["route"], turn["then"], turn["previous_run_id"]) == ("pick", "said", "builder_prev")
        [card] = [e for e in events if e.get("type") == "action_needed"]
        assert card["buttons"][0]["arguments"]["orchestration_id"] == "orch_second"
        assert "card is up" in turn["say"].lower()

    async def test_another_conversations_turn_is_never_read(self):
        from core.boards import assistant

        await self._turn("builder_other", "builder:t:panel:elsewhere", {"summary": "Done."})
        await assistant.note("builder_other", owner_id=None, also="pause the digest")
        now = await self._turn("builder_mine", "builder:t:panel:mine", {"user_message": "yes"})
        turn, _ = await self._read(now)
        assert turn["route"] == "yes_without_review" and turn["previous_run_id"] == ""

    async def test_a_yes_to_the_queued_request_makes_it_the_message(self):
        from core.boards import assistant

        await self._turn("builder_q1", "builder:t:panel:q", {"summary": "Done."})
        await assistant.note("builder_q1", owner_id=None, also="pause the nightly digest")
        now = await self._turn("builder_q2", "builder:t:panel:q", {"user_message": "yes please"})
        turn, _ = await self._read(now)
        assert turn["route"] == "next_request" and turn["message"] == "pause the nightly digest"
        assert now.shared_state["user_message"] == "pause the nightly digest"

    async def test_a_viewers_turn_is_read_and_a_pick_is_refused_by_the_cards_own_door(self):
        from core.boards import assistant
        from core.product_tools import hooks
        from core.product_tools.roles import Role
        from core.store.resources import save_orchestration
        from tests._fakes.seed import make_orchestration

        async def source(user_id):
            return Role.VIEWER

        hooks.set_role_source(source)
        try:
            for oid in ("orch_v1", "orch_v2"):
                await save_orchestration(make_orchestration(id=oid, name="Twin"))
            await self._turn("builder_v1", "builder:t:panel:v", {"summary": "Which one?"})
            await assistant.note("builder_v1", owner_id="v", picks=[{
                "action": "run_workflow", "field": "workflow", "arguments": {"message": "x"},
                "options": [{"id": "orch_v1", "name": "Twin"}, {"id": "orch_v2", "name": "Twin"}]}])
            from core import builder_access
            from core.models_orchestration import OrchestrationRun
            from core.orchestration.state import SharedState

            now = OrchestrationRun(run_id="builder_v2", orchestration_id="orch_native_builder",
                                   session_id="builder:t:panel:v",
                                   shared_state={builder_access.CALLER_KEY: {"user_id": "v"}, "user_message": "1"})
            await SharedState(now).checkpoint()
            turn, events = await self._read(now)
            assert turn["route"] == "pick" and not [e for e in events if e.get("type") == "action_needed"]
            assert "viewer" in turn["say"]
        finally:
            hooks.set_role_source(None)

    async def test_a_changes_pick_goes_back_through_the_role_door(self):
        from core.boards import assistant

        await self._turn("builder_c1", "builder:t:panel:c", {"summary": "Which one?"})
        await assistant.put("builder_c1", title="Which one?", stage="waiting", owner_id=None, data={"change": {
            "question": "Which one?", "picks": [{"op": 0, "field": "target", "kind": "agent", "wanted": "Support",
                                                 "options": [{"id": "agt_1", "name": "Support"},
                                                             {"id": "agt_2", "name": "Support billing"}]}]}})
        now = await self._turn("builder_c2", "builder:t:panel:c", {"user_message": "the second one"})
        turn, events = await self._read(now)
        assert (turn["route"], turn["then"], turn["picked"]["id"]) == ("pick", "change", "agt_2")
        assert now.shared_state["intent"] == "manage" and events == []
