"""Asked for a fact from outside that changes over time, Synapse AI offers to build the agent that gets it, and a yes
builds it (F1382, TRI1; eb's audit, Naveen 11-04: "Build as planned").

Asked a city's weather, Synapse AI said a bare "can't", or read the workspace for it: "today" is a workspace word, so
the answer went to the step that reads it. This chat does not look a live fact up; it builds the thing that does. These
pin the class in code:

- the quick model types the fact (`Request.live_fact`) and code routes it to the talk step (`live_fact`), never when
  the message names something here, asks how to, acts, or is about this workspace's own state ("how many runs failed
  today?", this month's usage), and never over a build;
- "what can you do for me?" goes to the talk step too (`about_you`), unless it names a service;
- a builder's offer is kept on the turn (`OFFERED_KEY`), a viewer's never: their pack says who can ask for one;
- the yes after it is the offer's ask (`offer_taken`), through the role door to the build route; a viewer's yes, or
  a yes with no offer, is read as before;
- a reply that does not end asking gets code's fixed offer, so a yes always has an offer behind it.
"""
from __future__ import annotations

import pytest

from core.native_builder.classify import override
from core.native_builder.objects import Request

FACT = "the weather in Lisbon today"
ASKED = "What's the weather in Lisbon today?"
INVENTORY = {"schedule": ["Morning digest"], "agent": ["Weather report"], "workflow": ["Daily price check"]}
BUILDER = {"may_change": True, "may_act": True}
VIEWER = {"may_change": False, "may_act": False}


def _route(message, kind="answer", state=None, **request):
    return override(Request(kind=kind, **request), message=message, state=state if state is not None else BUILDER,
                    inventory=INVENTORY, connectable=set())


class TestALiveFactGoesToTalk:
    @pytest.mark.parametrize("needs", ["none", "workspace", "help", None])
    def test_whatever_the_model_said_it_needs(self, needs):
        routed = _route(ASKED, needs=needs, live_fact=FACT)
        assert (routed.route, routed.needs, routed.overridden_by) == ("step_answer", "none", "live_fact"), routed
        assert routed.live_fact == FACT

    def test_a_viewers_goes_to_talk_too(self):
        routed = _route(ASKED, state=VIEWER, needs="workspace", live_fact=FACT)
        assert (routed.route, routed.needs, routed.overridden_by) == ("step_answer", "none", "live_fact")


class TestNeverOnWhatIsHere:
    @pytest.mark.parametrize("message, needs", [
        ("How many runs failed today?", "workspace"),
        ("What's this month's usage?", "workspace"),
        ("What did the Weather report agent send today?", "workspace"),
        ("How do I connect Telegram?", "help"),
    ])
    def test_this_workspaces_own_state_a_named_thing_or_a_how_to(self, message, needs):
        routed = _route(message, needs=needs, live_fact=FACT)
        assert routed.overridden_by != "live_fact" and routed.needs == needs, routed

    def test_a_build_still_builds(self):
        routed = _route("Build me an agent that sends the weather in Lisbon every morning", kind="build",
                        live_fact=FACT)
        assert (routed.kind, routed.route) == ("build", "step_plan_room") and routed.overridden_by is None

    def test_an_act_is_not_an_offer(self):
        routed = _route("run the Daily price check now", needs="workspace", live_fact="today's prices")
        assert routed.overridden_by != "live_fact"

    def test_talk_with_no_fact_is_as_before(self):
        routed = _route("hi", needs="none")
        assert (routed.needs, routed.overridden_by) == ("none", None)


class TestWhatCanYouDo:
    @pytest.mark.parametrize("message, kind, needs", [
        ("What can you do for me?", "answer", "help"),
        ("what can you do for me?", "build", None),
        ("Who are you?", "answer", "workspace"),
        ("How can you help my team?", "answer", "help"),
    ])
    def test_goes_to_talk(self, message, kind, needs):
        routed = _route(message, kind=kind, needs=needs)
        assert (routed.route, routed.needs, routed.overridden_by) == ("step_answer", "none", "about_you"), routed

    def test_not_when_it_names_a_service(self):
        routed = _route("What can you do with Slack?", needs="help", providers=["Slack"])
        assert routed.overridden_by != "about_you"


class TestTheYesBuildsIt:
    def test_the_offers_ask_goes_to_the_build_route(self):
        from core.native_builder.classify import OFFER_TAKEN_KEY, offer_ask

        routed = _route(offer_ask(FACT), needs="none", state={**BUILDER, OFFER_TAKEN_KEY: True})
        assert (routed.kind, routed.route, routed.overridden_by) == ("build", "step_plan_room", "offer_taken")

    def test_through_the_role_door(self):
        from core.native_builder.classify import OFFER_TAKEN_KEY, offer_ask

        routed = _route(offer_ask(FACT), needs="none", state={**VIEWER, OFFER_TAKEN_KEY: True})
        assert routed.route == "step_not_yours"

    def test_the_ask_names_the_fact_and_an_agent(self):
        from core.native_builder.classify import offer_ask

        assert FACT in offer_ask(FACT) and "agent" in offer_ask(FACT)


class TestTheOfferIsKept:
    """`classify_request` as `step_classify` calls it: a builder's offer is kept on the turn; a viewer's never."""

    @pytest.fixture(autouse=True)
    async def _ready(self):
        from core import builder_access
        from core.store import get_store

        await get_store()
        builder_access.forget_runs()
        yield
        builder_access.forget_runs()

    async def _classify(self, args, message, **state):
        import json

        from core import builder_access
        from core.builder_tools import execute_builder_tool_with_events
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import live
        from core.orchestration.state import SharedState

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        run = OrchestrationRun(run_id="builder_f1382", orchestration_id="orch_native_builder", session_id="s",
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, "user_message": message,
                                             **BUILDER, **state})
        await SharedState(run).checkpoint()
        with live(run):
            raw, _ = await execute_builder_tool_with_events("classify_request", args, _Server(), run_id=run.run_id)
        return json.loads(raw), run

    async def test_a_builders_offer_is_kept(self):
        from core.native_builder.classify import OFFERED_KEY, offer_ask

        routed, run = await self._classify({"kind": "answer", "needs": "workspace", "live_fact": FACT}, ASKED)
        assert routed["overridden_by"] == "live_fact"
        assert run.shared_state[OFFERED_KEY] == {"fact": FACT, "ask": offer_ask(FACT)}

    async def test_a_viewers_never(self):
        from core.native_builder.classify import OFFERED_KEY

        routed, run = await self._classify({"kind": "answer", "needs": "workspace", "live_fact": FACT}, ASKED,
                                           **VIEWER)
        assert routed["overridden_by"] == "live_fact" and not run.shared_state.get(OFFERED_KEY)

    async def test_nor_on_a_workspace_question(self):
        from core.native_builder.classify import OFFERED_KEY

        _, run = await self._classify({"kind": "answer", "needs": "workspace", "live_fact": FACT},
                                      "How many runs failed today?")
        assert not run.shared_state.get(OFFERED_KEY)


class TestTheNextTurn:
    @staticmethod
    def _previous(**more):
        from core.native_builder.classify import OFFERED_KEY, offer_ask

        return {OFFERED_KEY: {"fact": FACT, "ask": offer_ask(FACT)}, "summary": "I can build that agent. Want it?",
                **more}

    @pytest.mark.parametrize("said", ["yes", "Yes!", "sure", "yes please", "ok", "go ahead"])
    def test_a_yes_is_the_offers_ask(self, said):
        from core.native_builder.classify import offer_ask
        from core.native_builder.read_turn import decide

        turn = decide(said, previous=self._previous(), facts={})
        assert (turn.route, turn.then, turn.message) == ("offer_taken", "classify", offer_ask(FACT))

    def test_a_yes_with_more_keeps_what_they_added(self):
        from core.native_builder.classify import offer_ask
        from core.native_builder.read_turn import decide

        said = "yes, at 7am by email"
        turn = decide(said, previous=self._previous(), facts={})
        assert (turn.route, turn.message) == ("offer_taken", f"{offer_ask(FACT)}\n\n{said}")

    @pytest.mark.parametrize("said", ["no thanks", "ok thanks", "what about Porto?", "how many runs failed today?"])
    def test_anything_else_is_read_as_before(self, said):
        from core.native_builder.read_turn import decide

        assert decide(said, previous=self._previous(), facts={}).route != "offer_taken"

    def test_a_yes_with_no_offer_is_as_before(self):
        """A viewer's turn keeps no offer, so their yes never becomes the ask."""
        from core.native_builder.read_turn import decide

        turn = decide("yes", previous={"summary": "An owner or a builder here can ask for one. Anything else?"},
                      facts={})
        assert turn.route != "offer_taken" and turn.message is None


class TestTheOfferIsSaid:
    def test_a_reply_that_does_not_ask_gets_codes_offer(self):
        from core.native_builder.answer_pack import offer_kept, offer_line

        said = offer_kept("I don't look that up here.", {"fact": FACT})
        assert said.endswith(offer_line(FACT)) and said.startswith("I don't look that up here.")
        assert FACT in offer_line(FACT) and offer_line(FACT).endswith("?")

    def test_a_reply_that_asks_keeps_its_words(self):
        from core.native_builder.answer_pack import offer_kept

        reply = "I can make an agent that gets it each morning. Want it?"
        assert offer_kept(reply, {"fact": FACT}) == reply

    def test_no_offer_no_line(self):
        from core.native_builder.answer_pack import offer_kept

        assert offer_kept("Hello!", None) == "Hello!" and offer_kept("Hello!", {}) == "Hello!"

    def test_with_an_offer_kept_a_yes_asked_for_is_kept_too(self):
        """A yes has somewhere to go after the offer, so F1243's cut does not apply; without one it does."""
        from core.native_builder.answer_pack import NO_YES_HERE, checked_answer

        reply = "I can build an agent that gets it each morning. Just say yes and I'll set it up?"
        assert checked_answer(reply, None, {"fact": FACT}) == reply
        assert NO_YES_HERE in checked_answer(reply, None, None)

    def test_the_line_asks_for_no_yes_nothing_takes_and_claims_no_change(self):
        from core.native_builder.answer_pack import no_change_claimed, no_yes_offered, offer_line

        assert no_change_claimed(no_yes_offered(offer_line(FACT))) == offer_line(FACT)


class TestThePackSaysIt:
    @staticmethod
    def _pack(**kw):
        from core.native_builder.answer_pack import build_pack
        from core.product_tools.roles import Role

        return build_pack(routed={"needs": "none"}, workspace_name="Acme", profile="", asker_name="",
                          asker_role=Role.OWNER, last="", capabilities="What this product does:\n- Workflows.",
                          help_hits=[], named=[], **kw)

    def test_a_builders_pack_names_the_fact(self):
        pack = self._pack(live_fact=FACT, may_build=True)
        assert f"outside this workspace and changes over time: {FACT}." in pack
        assert "owner or a builder" not in pack

    def test_a_viewers_pack_says_who_can_ask(self):
        pack = self._pack(live_fact=FACT, may_build=False)
        assert f"outside this workspace and changes over time: {FACT}." in pack
        assert "for an owner or a builder of this workspace" in pack

    def test_no_fact_no_line(self):
        assert "changes over time" not in self._pack()
