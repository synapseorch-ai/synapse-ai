"""The one planner researches while it plans (D340 D2; Naveen 10-30: "researcher is also everything", 10-31: the plan
as restated).

Research ran only after a plan was checked, and only to find who provides a missing tool; every other fact a plan
rests on was guessed (Naveen's prod run invented the site to scrape). Now the planner can finish with `research_questions`: up to
four questions, each `product` (the help answers, in code), `public` (code reads a URL the question names through the
guarded read; the rest goes to the researcher with the exact questions, inside the plan's web budget) or `business`
(asked of the person in one form, the planner's own round). The answers come back on its next pass, typed, with their
sources — it never reads a raw page.
"""
from __future__ import annotations

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order
from core.expressions import evaluate_expression
from core.native_builder import research_service as R
from core.native_builder.seed import bundled_definitions


@pytest.fixture
def state(monkeypatch):
    """The run's state, as research's tools read it."""
    s: dict = {}
    monkeypatch.setattr(R, "_state", lambda: s)
    return s


def _ask(kind, ask, **extra):
    return {"kind": kind, "ask": ask, "why": "", "url": "", **extra}


class TestTheFinish:
    async def test_up_to_four_questions_each_with_a_kind_and_an_ask(self):
        out = await R.research({"questions": [{"kind": "public", "ask": " What does  the site hold? ",
                                               "url": "https://example.com"}]}, None, [])
        assert out == {"finish": "research_questions", "questions": [
            {"kind": "public", "ask": "What does the site hold?", "why": "", "url": "https://example.com"}]}

    @pytest.mark.parametrize("args", [
        {"questions": []},
        {"questions": [{"kind": "public", "ask": f"q{i}"} for i in range(5)]},
        {"questions": [{"kind": "rumour", "ask": "who?"}]},
        {"questions": [{"kind": "business", "ask": "  "}]},
    ])
    async def test_what_is_not_a_question_goes_back_to_the_model(self, args):
        assert "error" in await R.research(args, None, [])

    async def test_a_key_is_never_researched(self):
        out = await R.research({"questions": [{"kind": "business", "ask": "What is your Stripe API key?"}]}, None, [])
        assert "error" in out


class TestCodeAnswersFirst:
    async def test_a_product_question_is_answered_from_the_help(self, state, monkeypatch):
        import core.builder_help as help_

        async def search_help(args, *_):
            assert args == {"question": "How do evals score a workflow?"}
            return {"topics": [{"title": "Evals", "summary": "A rubric scores each run.", "steps": ["Open Evals"],
                                "screen": "Evals", "where": "/evals"}]}

        monkeypatch.setattr(help_, "search_help", search_help)
        state["plan_draft"] = {"questions": [_ask("product", "How do evals score a workflow?")]}
        out = await R.answer_research({}, None, [])
        assert out["next"] == "plan" and out["left"] == [] and out["business"] == []
        [answer] = out["answers"]
        assert answer["by"] == "code" and answer["sure"] and "A rubric scores each run" in answer["answer"]
        assert answer["sources"] == ["Evals"]

    async def test_a_product_question_the_help_has_nothing_on_is_said_so_never_sent_to_the_web(self, state,
                                                                                                  monkeypatch):
        import core.builder_help as help_

        async def nothing(args, *_):
            return {"topics": []}

        monkeypatch.setattr(help_, "search_help", nothing)
        state["plan_draft"] = {"questions": [_ask("product", "Can it print?")]}
        out = await R.answer_research({}, None, [])
        assert out["next"] == "plan" and out["left"] == []
        assert out["answers"][0]["sure"] is False and "may not be done here" in out["answers"][0]["answer"]

    async def test_a_page_the_question_names_is_read_through_the_guarded_read_typed(self, state, monkeypatch):
        from core.native_builder import named_sites

        async def read_named(text, *, already=None):
            assert "https://acme.example/blog" in text
            return {"https://acme.example/blog": {"url": "https://acme.example/blog", "reached": True,
                                                  "final_url": "https://acme.example/blog", "title": "Acme blog",
                                                  "what": "Posts about our product", "kind": "site",
                                                  "api_spec": None}}

        monkeypatch.setattr(named_sites, "read_named", read_named)
        state["plan_draft"] = {"questions": [_ask("public", "What does the blog hold?",
                                                  url="https://acme.example/blog")]}
        out = await R.answer_research({}, None, [])
        assert out["next"] == "plan"
        [answer] = out["answers"]
        assert answer["sure"] and "Acme blog" in answer["answer"] and "a site of many pages" in answer["answer"]
        assert answer["sources"] == ["https://acme.example/blog"] and "https://acme.example/blog" in out["pages"]

    async def test_a_public_question_code_cannot_settle_goes_to_the_researcher(self, state):
        state["plan_draft"] = {"questions": [_ask("public", "Which carriers does Shippo support?")]}
        out = await R.answer_research({}, None, [])
        assert out["next"] == "researcher" and [q["ask"] for q in out["left"]] == [
            "Which carriers does Shippo support?"]

    async def test_a_business_question_goes_to_the_person(self, state):
        state["plan_draft"] = {"questions": [_ask("business", "Which team approves refunds?")]}
        out = await R.answer_research({}, None, [])
        assert out["next"] == "person" and [q["ask"] for q in out["business"]] == ["Which team approves refunds?"]

    async def test_earlier_answers_are_kept(self, state):
        state["plan_answers"] = {"answers": [{"ask": "before", "answer": "kept", "by": "code", "sure": True}]}
        state["plan_draft"] = {"questions": [_ask("business", "now?")]}
        out = await R.answer_research({}, None, [])
        assert out["answers"][0]["answer"] == "kept"


class TestTheResearchersAnswers:
    async def test_what_it_settled_is_kept_and_the_rest_goes_to_the_person(self, state):
        state["plan_answers"] = {"answers": [], "left": [_ask("public", "Which carriers?"), _ask("public", "Rate?")],
                                 "business": [_ask("business", "Your carrier?")]}
        state["research_found"] = {"answers": [
            {"ask": "which carriers?", "answer": "UPS, USPS and DHL", "sources": ["https://docs.shippo.com"],
             "sure": True},
            {"ask": "Rate?", "answer": "unclear", "sure": False}]}
        out = await R.settle_research({}, None, [])
        assert [(a["ask"], a["by"]) for a in out["answers"]] == [("Which carriers?", "research")]
        assert [q["ask"] for q in out["business"]] == ["Your carrier?", "Rate?"]
        assert out["next"] == "person" and out["left"] == []

    async def test_all_settled_goes_back_to_the_planner(self, state):
        state["plan_answers"] = {"answers": [], "left": [_ask("public", "Which carriers?")], "business": []}
        state["research_found"] = {"answers": [{"ask": "Which carriers?", "answer": "UPS", "sure": True}]}
        assert (await R.settle_research({}, None, []))["next"] == "plan"


class TestThePersonIsAskedInOneForm:
    async def test_it_is_the_planners_own_ask_person_round(self, state, monkeypatch):
        from core.native_builder import change_tools

        seen = {}

        async def ask_person(args, *_):
            seen.update(args)
            return {"finish": "ask_person", "question": "A few things", "form": args["questions"]}

        monkeypatch.setattr(change_tools, "ask_person", ask_person)
        state["plan_answers"] = {"business": [_ask("business", "Which team approves refunds?"),
                                              _ask("public", "Rate?")]}
        out = await R.ask_research({}, None, [])
        assert out["finish"] == "ask_person"
        assert [q["question"] for q in seen["questions"]] == ["Which team approves refunds?", "Rate?"]


class TestTheWebBudget:
    async def test_a_question_has_its_own_six_calls(self, monkeypatch):
        from core import builder_research as B

        B._LOOSE.clear()
        for _ in range(B.WEB_PER_QUESTION):
            assert await B._over_question_budget({"question": "Which carriers?"}) is None
        over = await B._over_question_budget({"question": "Which carriers?"})
        assert over and over["enough"] and "6 web calls" in over["note"]
        assert await B._over_question_budget({"question": "Another one?"}) is None, "its own budget"
        B._LOOSE.clear()

    async def test_the_provider_researchs_calls_keep_only_their_backstop(self):
        from core import builder_research as B

        B._LOOSE.clear()
        for _ in range(B.WEB_PER_PLAN + 5):
            assert await B._over_question_budget({}) is None
        B._LOOSE.clear()

    async def test_findings_may_be_answers_alone(self):
        from core import builder_research as B

        out = await B.report_findings({"answers": [{"ask": "Which carriers?", "answer": "UPS",
                                                    "sources": ["https://docs.shippo.com"]}]}, None, [])
        assert out["status"] == "reported" and out["answers"][0]["sure"] is True and out["options"] == []


class TestTheWorkflow:
    @pytest.fixture(scope="class")
    def steps(self):
        return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}

    def test_the_planner_holds_research_and_says_when(self, steps):
        planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
        assert "research_questions" in planner["tools"] and "research_questions" in steps["step_plan"]["finish_tools"]
        assert "# Research" in planner["system_prompt"] and "`business`" in planner["system_prompt"]
        assert "{{ state.plan_answers.answers }}" in steps["step_plan"]["prompt_template"]

    def test_a_research_finish_reaches_code_first(self, steps):
        route = steps["step_plan_finish"]
        value = evaluate_expression(route["switch_expression"], {"plan_draft": {"finish": "research_questions"},
                                                                 "intent": "create"}, None).value
        assert route["switch_cases"].get(value) == "step_research_answer"
        assert steps["step_research_answer"]["forced_tool"] == "answer_research"
        assert steps["step_research_answer"]["next_step_id"] == "step_research_route"

    @pytest.mark.parametrize("next_, step", [("researcher", "step_research_find"), ("person", "step_research_ask"),
                                             ("plan", "step_plan")])
    def test_what_is_left_goes_where_it_should(self, steps, next_, step):
        route = steps["step_research_route"]
        value = evaluate_expression(route["switch_expression"], {"plan_answers": {"next": next_}}, None).value
        assert route["switch_cases"].get(value, route["switch_default_step_id"]) == step

    def test_the_researcher_gets_the_questions_and_its_answers_are_kept_in_code(self, steps):
        find = steps["step_research_find"]
        assert find["agent_id"] == "agent_native_builder_plan_researcher" and find["finish_tools"] == ["report_findings"]
        assert "{{ state.plan_answers.left }}" in find["prompt_template"] and "`question`" in find["prompt_template"]
        assert find["next_step_id"] == "step_research_settle"
        assert steps["step_research_settle"]["next_step_id"] == "step_research_route"

    @pytest.mark.parametrize("intent, check", [("create", "step_check_plan"), ("manage", "step_check_change")])
    def test_the_person_is_asked_the_way_the_planners_own_question_is(self, steps, intent, check):
        ask = steps["step_research_ask"]
        assert ask["forced_tool"] == "ask_research" and ask["output_key"] == "plan_draft"
        # F1296: its own route, the split step_plan_finish makes for an ask, so that router counts only the planner.
        assert ask["next_step_id"] == "step_research_ask_route"
        route = steps["step_research_ask_route"]
        value = evaluate_expression(route["switch_expression"], {"plan_draft": {"finish": "ask_person"},
                                                                 "intent": intent}, None).value
        assert route["switch_cases"].get(value, route["switch_default_step_id"]) == check

    def test_its_name_is_not_the_classifiers_research_route(self, steps):
        """The classifier's `research` is a route (a provider question), not a tool: the finish has its own name, so no
        prompt teaches a tool its agent doesn't hold (F800)."""
        from core.builder_tools import BUILDER_TOOL_NAMES

        assert "research_questions" in BUILDER_TOOL_NAMES and "research" not in BUILDER_TOOL_NAMES

    def test_the_provider_lookup_after_the_check_stays(self, steps):
        assert steps["step_plan_route"]["switch_cases"]["research"] == "step_plan_lookup"
        assert steps["step_plan_research"]["next_step_id"] == "step_plan"
