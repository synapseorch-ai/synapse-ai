"""One quick call types the request; code overrides it with facts (SAI2-P4, F979; DESIGN.md decision 4).

The quick model writes a `Request` (four kinds: answer, change, build, research; plus what the answer `needs`).
Code then corrects it from what it knows, so a fact never rests on a model's label:

* **acts on an existing thing** ("turn on / pause / run / stop / delete X", X exists) are a change, whatever the
  model said (P2's after-pass: "turn on" labelled answer; F755/act on Nova Pro);
* **a named schedule or agent with a change verb** is a change, never a build (dev: "change the schedule to 09:00"
  routed create);
* **findings, then a yes or a build** is a build (F766), through the role door; with no findings, research stays
  research (BWA1 #9);
* **an outside provider found nowhere** is never answered "can't" without a lookup: it goes to research (BWA1 #8;
  Nova Pro sent 4/12 research to cantdo);
* **below builder**, a build is the role's sentence (`step_not_yours`); a change needs `may_act`;
* **`needs` errs towards reading** (SAI2's review): `none` only when the model said none (or nothing) and code finds
  no workspace noun, no named thing and no question about runs.
"""
from __future__ import annotations

import pytest

from core.native_builder.classify import override
from core.native_builder.objects import Request

INVENTORY = {"schedule": ["Nightly invoices", "Morning digest"], "agent": ["Front desk", "Support"],
             "workflow": ["Invoice chaser", "Inbox triage"]}
CONNECTABLE = {"slack", "notion", "google sheets", "stripe", "openai"}
BUILDER = {"may_change": True, "may_act": True}
APPROVER = {"may_change": False, "may_act": True, "asker_role": "approver"}
VIEWER = {"may_change": False, "may_act": False, "asker_role": "viewer"}
STRIPE_FINDINGS = {"options": [{"provider": "Stripe", "via": "mcp_server"}]}


def _route(message, kind, *, state=None, **request):
    return override(Request(kind=kind, **request), message=message, state={**BUILDER, **(state or {})},
                    inventory=INVENTORY, connectable=CONNECTABLE)


class TestActsOnWhatExists:
    @pytest.mark.parametrize("message", ["turn on Nightly invoices", "pause the morning digest",
                                         "run Invoice chaser now", "please stop inbox triage",
                                         "can you turn off the nightly invoices schedule",
                                         "delete the Support agent"])
    def test_an_act_on_an_existing_thing_is_a_change(self, message):
        routed = _route(message, "answer")
        assert (routed.kind, routed.route) == ("change", "step_change_context")
        assert routed.overridden_by == "act_on_existing" and routed.model_kind == "answer"

    def test_an_act_on_nothing_that_exists_is_left_to_the_model(self):
        assert _route("turn on the payroll sync", "answer").kind == "answer"

    @pytest.mark.parametrize("message", ["what runs failed today?", "how do I run a workflow every hour?",
                                         "show me what Nightly invoices did"])
    def test_a_question_about_a_thing_is_not_an_act(self, message):
        assert _route(message, "answer").kind == "answer"


class TestANamedThingIsChangedNotBuilt:
    @pytest.mark.parametrize("message", ["change Nightly invoices to 09:00", "move the morning digest to 7am",
                                         "rename Front desk to Reception", "switch the Support agent to gpt-4o",
                                         "change the schedule to 09:00"])
    def test_a_change_verb_on_a_named_schedule_or_agent(self, message):
        routed = _route(message, "build")
        assert (routed.kind, routed.route, routed.overridden_by) == ("change", "step_change_context", "named_change")

    def test_a_new_thing_beside_an_old_one_is_still_a_build(self):
        assert _route("build a new schedule like Nightly invoices but for receipts", "build").kind == "build"


class TestFindingsThenABuild:
    @pytest.mark.parametrize("message,kind", [("go ahead and build it", "research"), ("yes", "answer"),
                                              ("build it with the MCP server", "build")])
    def test_after_findings_it_is_a_build(self, message, kind):
        routed = _route(message, kind, state={"prior_findings": STRIPE_FINDINGS})
        assert (routed.kind, routed.route) == ("build", "step_plan_room")

    @pytest.mark.parametrize("found", [None, {}, {"options": []}])
    def test_with_no_findings_research_stays_research(self, found):
        routed = _route("find who can send WhatsApp messages", "research", state={"prior_findings": found})
        assert (routed.kind, routed.route) == ("research", "step_research")

    def test_a_question_about_the_findings_is_still_a_question(self):
        assert _route("what did you find about Stripe?", "answer",
                      state={"prior_findings": STRIPE_FINDINGS}).kind == "answer"

    @pytest.mark.parametrize("who", [VIEWER, APPROVER])
    def test_the_role_door_holds_after_findings(self, who):
        routed = _route("go ahead and build it", "research", state={"prior_findings": STRIPE_FINDINGS, **who})
        assert routed.route == "step_not_yours"


class TestAProviderIsLookedUpNotRefused:
    def test_one_found_nowhere_goes_to_research(self):
        routed = _route("can you connect to Uber Health?", "answer", providers=["Uber Health"])
        assert (routed.kind, routed.route, routed.overridden_by) == ("research", "step_research", "provider_unknown")

    def test_one_the_workspace_can_connect_is_answered(self):
        routed = _route("how do I connect Slack?", "answer", providers=["Slack"])
        assert (routed.kind, routed.route) == ("answer", "step_answer")

    def test_a_provider_in_a_question_that_asks_for_nothing_is_left_alone(self):
        assert _route("is Uber Health a good company?", "answer", providers=["Uber Health"]).kind == "answer"


class TestTheRoleDoor:
    @pytest.mark.parametrize("who", [VIEWER, APPROVER])
    def test_below_builder_a_build_is_the_roles_sentence(self, who):
        assert _route("build me a weekly report", "build", state=who).route == "step_not_yours"

    def test_an_approver_reaches_the_change_route_and_a_viewer_does_not(self):
        assert _route("pause the morning digest", "change", state=APPROVER).route == "step_change_context"
        assert _route("pause the morning digest", "change", state=VIEWER).route == "step_not_yours"

    def test_anyone_may_ask_and_research(self):
        assert _route("what failed today?", "answer", state=VIEWER).route == "step_answer"
        assert _route("find who can send faxes", "research", state=VIEWER).route == "step_research"


class TestWhatTheAnswerNeeds:
    @pytest.mark.parametrize("message", ["hi", "thanks!", "brainstorm a reminder flow with me", "goodbye",
                                         "what do you think of a two-step approval?"])
    def test_talk_needs_nothing(self, message):
        for said in ("none", None):
            assert _route(message, "answer", needs=said).needs == "none"

    @pytest.mark.parametrize("message", ["what failed today", "is my nightly digest on",
                                         "why did invoice sync stop", "how many agents do I have?",
                                         "what did Front desk do yesterday?"])
    def test_a_question_about_this_workspace_reads_it_whatever_the_model_said(self, message):
        assert _route(message, "answer", needs="none").needs == "workspace"

    @pytest.mark.parametrize("message", ["how do I call your API from Python?", "how do I add an MCP server?",
                                         "what can you do?", "does Synapse do webhooks?"])
    def test_how_the_product_works_reads_its_help(self, message):
        assert _route(message, "answer", needs="none").needs == "help"

    def test_a_name_that_is_also_a_word_reads_the_workspace(self):
        """An agent called "Support": a message with that word may be about it, so it reads (the safe side)."""
        assert _route("does Synapse support webhooks?", "answer", needs="help").needs == "workspace"

    def test_the_models_reading_is_kept_when_it_asks_for_more(self):
        assert _route("hello there", "answer", needs="workspace").needs == "workspace"
        assert _route("hello there", "answer", needs="help").needs == "help"

    def test_a_route_that_is_not_an_answer_reads_the_workspace(self):
        assert _route("build me a weekly report", "build", needs="none").needs == "workspace"


class TestCreateOrUpdate:
    def test_a_named_workflow_is_updated(self):
        routed = _route("add a Slack step to Invoice chaser", "build",
                        state={"current_orchestration_source": "named", "current_orchestration_id": "orch_inv"})
        assert routed.route == "step_fetch_existing"

    def test_the_open_workflow_is_updated_when_the_message_edits_it(self):
        state = {"current_orchestration_source": "viewing", "current_orchestration_id": "orch_inv"}
        assert _route("add a step that emails me", "build", state=state).route == "step_fetch_existing"
        assert _route("build me a new weekly report", "build", state=state).route == "step_plan_room"

    def test_pasted_export_code_is_always_a_new_build(self):
        state = {"current_orchestration_source": "viewing", "current_orchestration_id": "orch_inv",
                 "export_source": "agents_sdk"}
        assert _route("add this", "build", state=state).route == "step_plan_room"

    def test_two_that_match_are_asked_about_in_the_answer(self):
        state = {"current_orchestration_source": "ambiguous", "current_orchestration_matches": "a\nb"}
        routed = _route("update the invoice workflow", "build", state=state)
        assert (routed.route, routed.needs) == ("step_answer", "workspace")


class TestTheLegacyIntent:
    """The build route reads `state.intent` ("update"), and the role door (`step_may_change`) too."""

    @pytest.mark.parametrize("message,kind,state,intent", [
        ("what failed today", "answer", {}, "answer"),
        ("pause the morning digest", "answer", {}, "manage"),
        ("build me a weekly report", "build", {}, "create"),
        ("add a step", "build", {"current_orchestration_source": "named", "current_orchestration_id": "o"}, "update"),
        ("find who can send faxes", "research", {}, "research"),
    ])
    def test_each_route_says_its_intent(self, message, kind, state, intent):
        from core.native_builder.classify import intent_of

        assert intent_of(_route(message, kind, state=state)) == intent


class TestTheStep:
    """`classify_request` as `step_classify` calls it: the Routed it returns, the legacy intent, the queued request."""

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

        run = OrchestrationRun(run_id="builder_cls", orchestration_id="orch_native_builder", session_id="s",
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, "user_message": message,
                                             "may_change": True, "may_act": True, **state})
        await SharedState(run).checkpoint()
        with live(run):
            raw, events = await execute_builder_tool_with_events("classify_request", args, _Server(),
                                                                 run_id=run.run_id)
        return json.loads(raw), events, run

    async def test_an_act_on_an_existing_schedule_routes_to_change_and_says_its_intent(self):
        from core.store import schedules

        await schedules.save({"id": "sched_n", "name": "Nightly invoices", "enabled": False,
                              "target_type": "orchestration", "target_id": "o", "prompt": "",
                              "schedule_type": "cron", "cron_expression": "0 2 * * *"})
        routed, _, run = await self._classify({"kind": "answer"}, "turn on Nightly invoices")
        assert (routed["kind"], routed["route"], routed["model_kind"]) == ("change", "step_change_context", "answer")
        assert run.shared_state["intent"] == "manage"

    async def test_a_second_request_is_kept_and_said(self):
        from core.boards import assistant

        routed, events, _ = await self._classify({"kind": "answer", "needs": "workspace",
                                                  "also": "pause the digest"}, "what failed today, and pause the digest")
        assert routed["also"] == "pause the digest"
        assert (await assistant.turn("builder_cls"))["also"] == "pause the digest"
        [notice] = [e for e in events if e.get("type") == "builder_notice"]
        assert "pause the digest" in notice["message"] and "say yes" in notice["message"]

    async def test_a_call_that_is_no_request_is_an_answer_read_by_code(self):
        routed, _, run = await self._classify({"kind": "cantdo"}, "hi")
        assert (routed["kind"], routed["route"], routed["needs"]) == ("answer", "step_answer", "none")
        assert run.shared_state["intent"] == "answer"
