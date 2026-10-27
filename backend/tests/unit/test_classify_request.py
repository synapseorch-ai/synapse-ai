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
    @pytest.mark.parametrize("message", ["can you book patient rides through Uber Health?",
                                         "can we send claims to Uber Health from here?"])
    def test_using_one_found_nowhere_goes_to_research(self, message):
        routed = _route(message, "answer", providers=["Uber Health"])
        assert (routed.kind, routed.route, routed.overridden_by) == ("research", "step_research", "provider_unknown")

    @pytest.mark.parametrize("message,provider", [
        ("How do I connect Slack?", "Slack"), ("Hook up Notion.", "Notion"),
        ("Connect my GitHub MCP server.", "GitHub"),
        ("Can you connect our Linear workspace so the agents can file issues?", "Linear"),
        ("can you connect to Uber Health?", "Uber Health")])
    def test_asking_to_connect_one_is_answered_with_its_card_not_researched(self, message, provider):
        """P4's router replay (10-24): self-hosted has no catalogue, so "found nowhere" sent every connect request to
        research. Connecting a named service is the answer route's: its Connect card, or how to connect it."""
        routed = _route(message, "answer", providers=[provider])
        assert (routed.kind, routed.route) == ("answer", "step_answer")

    def test_one_the_workspace_can_connect_is_answered(self):
        routed = _route("how do I connect Slack?", "answer", providers=["Slack"])
        assert (routed.kind, routed.route) == ("answer", "step_answer")

    def test_a_provider_in_a_question_that_asks_for_nothing_is_left_alone(self):
        assert _route("is Uber Health a good company?", "answer", providers=["Uber Health"]).kind == "answer"


class TestAQuestionIsAnswered:
    """P4's router replay (10-24): Nova Pro labelled "how do i make a workflow run every weekday at 9?" and "is the
    invoice sync still running?" as changes. A message shaped as a question about how or whether is an answer."""

    @pytest.mark.parametrize("message", ["how do i make a workflow run every weekday at 9?",
                                         "Can a run wait for my approval before it sends anything?",
                                         "Can I stop a workflow from spending more than a few credits per run?",
                                         "is the invoice sync still running?",
                                         "How do I invite my office manager so she can approve runs?"])
    @pytest.mark.parametrize("kind", ["change", "build"])
    def test_a_how_or_whether_question_is_an_answer(self, message, kind):
        routed = _route(message, kind)
        assert (routed.kind, routed.route, routed.overridden_by) == ("answer", "step_answer", "question")

    @pytest.mark.parametrize("message", ["When a treatment plan is accepted, send the patient a Stripe payment link",
                                         "When a new lead arrives, add it to the sheet",
                                         "What I need is a weekly report every Monday",
                                         "Is it ok to build me an invoice reminder. Every Friday."])
    def test_a_request_that_starts_like_a_question_is_still_a_build(self, message):
        """The combined-suite check (10-27): "When X happens, do Y" is a trigger, not a question. A weak question word
        counts only when the message asks, with a question mark."""
        assert _route(message, "build").kind == "build"

    @pytest.mark.parametrize("message", ["When does the nightly digest run?", "Is the invoice sync on?",
                                         "Is there a way to send invoices automatically"])
    def test_a_real_question_is_still_an_answer(self, message):
        """SAI2's pair: a weak start with a "?" asks; a strong start asks with or without one."""
        routed = _route(message, "build")
        assert (routed.kind, routed.route) == ("answer", "step_answer")

    @pytest.mark.parametrize("message", ["can you pause the morning digest?", "please turn on Nightly invoices",
                                         "change Nightly invoices to 09:00"])
    def test_a_request_said_politely_is_still_a_request(self, message):
        assert _route(message, "change").kind == "change"


class TestConnectingANamedServiceIsAnswered:
    """P4's router replay: Haiku and gpt-oss labelled "Hook up Notion" a build. Connecting a named service is the
    answer route's Connect card; asking for an automation that uses it is still a build."""

    @pytest.mark.parametrize("message,provider", [("Hook up Notion.", "Notion"),
                                                  ("Connect my GitHub MCP server.", "GitHub"),
                                                  ("Connect the 'Acme Weather' MCP server.", "Acme Weather")])
    @pytest.mark.parametrize("kind", ["build", "change"])
    def test_a_connect_request_is_an_answer(self, message, provider, kind):
        routed = _route(message, kind, providers=[provider])
        assert (routed.kind, routed.route, routed.overridden_by) == ("answer", "step_answer", "connect_named")

    def test_an_automation_through_it_is_a_build(self):
        routed = _route("Connect Notion and every morning file the new tickets into it", "build", providers=["Notion"])
        assert routed.kind == "build"


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
                                         "what do you think of a two-step approval?", "what can you do?"])
    def test_talk_needs_nothing(self, message):
        for said in ("none", None):
            assert _route(message, "answer", needs=said).needs == "none"

    @pytest.mark.parametrize("message", ["what failed today", "is my nightly digest on",
                                         "why did invoice sync stop", "how many agents do I have?",
                                         "what did Front desk do yesterday?"])
    def test_a_question_about_this_workspace_reads_it_whatever_the_model_said(self, message):
        assert _route(message, "answer", needs="none").needs == "workspace"

    @pytest.mark.parametrize("message", ["how do I call your API from Python?", "how do I add an MCP server?",
                                         "does Synapse do webhooks?"])
    def test_how_the_product_works_reads_its_help(self, message):
        assert _route(message, "answer", needs="none").needs == "help"

    @pytest.mark.parametrize("message", ["pause it", "turn everything off", "stop it now", "delete that one",
                                         "rename it to Reception"])
    def test_an_act_or_a_change_with_nothing_named_still_reads(self, message):
        """SAI2's review 3: with no name code knows, an act is the model's to place, but never as talk."""
        assert _route(message, "answer", needs="none").needs == "workspace"

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

    def test_pasted_export_code_is_never_an_act_or_a_change(self):
        """SAI2's review 1: a paste naming an existing agent and saying "run it" is still a new build."""
        state = {"export_source": "agents_sdk"}
        for kind in ("answer", "change", "build"):
            routed = _route("Here is our Support agent's export, build it and run it", kind, state=state)
            assert (routed.kind, routed.route) == ("build", "step_plan_room"), kind

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
