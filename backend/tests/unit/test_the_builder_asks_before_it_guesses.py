"""The builder asks before it guesses (F1253, GUIDE1; TRI1's 10-29 run in Naveen's own workspace).

Asked for a weekly post about "our synapseorch product", the planner wrote https://synapseorch.com into the step that
reads the product page and said "I have what I need": nobody had given it that address. On its next pass it put the
address under the plan's `questions`, the card's "Open questions", which hold nothing up; the missing Twitter tool
sent the plan to research first, and the plan drafted after it had no questions left. The run after that made up
https://synapseorchestrator.com. Asking was the model's choice alone, and it had an outlet that never stops.

Now (Naveen, 10-30: "let the agent decide… build a form and ask questions if needed then proceed to build"):

* `ask_person` asks in one form: several typed questions at once, the planner's guess in each, one round;
* a plan carries no question: one the planner writes anyway is asked in that form before any plan is shown;
* code checks every address in the plan (a link, an email, a handle, a phone number) against what the run was given:
  the person's words and answers, the workspace, its profile, what research read, a pasted export. One it was never
  given is asked about before any plan or research, in the same form with the planner's value filled in; a public
  site too (Naveen's pick, 10-30), since code cannot tell the person's own site from a public one;
* every question goes through the request's seam (F1226): a form is one round and no question is asked twice; at the
  cap the plan goes on, and its review names each address the person has not given.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from _fakes import seed as S
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, change_tools as T, objects as O
from core.orchestration.engine import live
from core.orchestration.state import SharedState

ROOT = Path(__file__).resolve().parents[2] / "core" / "native_builder"
DEF = json.loads((ROOT / "orchestration.json").read_text())
STEPS = {s["id"]: s for s in DEF["steps"]}

#: TRI1's turn 1, near enough: the person named the product, never its site.
TURN_1 = ("Make a new workflow that looks at our synapseorch product and puts up a post like \"do you know…\" about "
          "one of its features each week.")
SITE = "https://synapseorch.com"
#: What turn 6's run made up, with the first answered.
TURN_6_SITE = "https://synapseorchestrator.com"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def desk():
    from core.store.resources import save_tool

    builder_access.forget_runs()
    await save_tool({"name": "read_page", "description": "Reads one web page as text.", "tool_type": "http",
                     "method": "GET", "url": "https://reader.example/read", "headers": {},
                     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
                     "annotations": {"readOnlyHint": True}})
    await S.seed_agents([S.make_agent(id="agent_writer", name="Post writer", tools=[])])
    yield
    builder_access.forget_runs()


async def _run(message: str = TURN_1, run_id: str = "builder_f1253", **state):
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": message, "person_timezone": "Europe/London", **state})
    await SharedState(run).checkpoint()
    return run


async def _call(tool: str, args: dict, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _check(run, draft):
    run.shared_state[build_plan.DRAFT_KEY] = draft
    out = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = out
    return out


def _plan(url: str = SITE, *, also: list[dict] | None = None, prompt: str = "Write a \"do you know…\" post about "
          "one feature from {{state.read}}.") -> dict:
    """Turn 1's plan: read the product page at `url`, then write the post."""
    return {"finish": "submit_plan", "kind": "create", "title": "Weekly feature post",
            "summary": "Each week, a \"do you know…\" post about one feature of the product.",
            "steps": [{"id": "read", "type": "tool", "name": "Read the product page", "does": "Reads the page",
                       "tool": "read_page", "config": {"tool_arguments": {"url": url}}, "next": "write"},
                      {"id": "write", "type": "agent", "name": "Write the post", "does": "Writes the post",
                       "agent": "agent_writer", "prompt": prompt, "next": (also or [{}])[0].get("id", "end")},
                      *(also or [])]}


#: Turn 5's Twitter step: a tool nothing here provides, so the plan would go to research.
TWEET = {"id": "post", "type": "tool", "name": "Post it", "does": "Posts the post", "tool": "post_tweet",
         "next": "end"}


# ── an address nobody gave ──────────────────────────────────────────────────────────────────────────────────────

class TestAnAddressNobodyGave:
    async def test_turn_1s_address_is_asked_before_any_plan_with_it_filled_in(self, client, desk):
        run = await _run()
        out = await _check(run, _plan())
        assert out["outcome"] == "ask" and out["plan"] is None, "no card before the person has said it"
        [field] = out["form"]
        assert field["value"] == SITE and field["type"] == "string"
        assert field["label"] == ("\"Read the product page\" uses this address, which you haven't given me. Is it the "
                                  "right one?"), "the address is the field's own, filled in, never said twice"
        assert f"1. {field['label']} (my guess: {SITE})" in out["question"], (
            "the words alone ask it too, with the address: the run page and a channel reply")
        [noted] = run.shared_state[T.ASKED_KEY]
        assert noted["subject"] == "address synapseorch.com" and noted["field"] == field["name"]

    async def test_it_is_asked_before_research(self, client, desk):
        run = await _run()
        out = await _check(run, _plan(also=[TWEET]))
        assert out["outcome"] == "ask", "turn 5: research went first, and the question was lost after it"
        assert [f["value"] for f in out["form"]] == [SITE]

    async def test_the_form_answer_is_kept_and_the_plan_goes_on_with_it(self, client, desk):
        run = await _run()
        field = (await _check(run, _plan()))["form"][0]
        run.shared_state["human_response"] = {"answer": f"{field['label']} {SITE}/features",
                                              "form": {field["name"]: f"{SITE}/features"}}
        out = await _check(run, _plan(f"{SITE}/features"))
        assert out["outcome"] == "review"
        assert out["asked"][-1] == {"question": field["label"], "answer": f"{SITE}/features"}
        assert T._with_answers(run.shared_state)[-1]["answer"] == f"{SITE}/features"

    async def test_a_typed_yes_answers_it_and_it_is_never_asked_again(self, client, desk):
        run = await _run()
        await _check(run, _plan())
        run.shared_state["human_response"] = {"answer": "yes, that's our site"}
        assert (await _check(run, _plan()))["outcome"] == "review"
        run.shared_state.pop("human_response")
        assert (await _check(run, _plan()))["outcome"] == "review"

    async def test_turn_6s_made_up_address_is_asked_though_the_first_was_answered(self, client, desk):
        run = await _run()
        await _check(run, _plan())
        run.shared_state["human_response"] = {"answer": SITE}
        out = await _check(run, _plan(TURN_6_SITE))
        assert out["outcome"] == "ask" and [f["value"] for f in out["form"]] == [TURN_6_SITE]

    async def test_an_email_in_a_steps_words_is_asked(self, client, desk):
        run = await _run(f"Read {SITE} each week, write a post about one feature and email it for a check.")
        out = await _check(run, _plan(prompt="Write the post from {{state.read}} and email it to team@synapseorch.com "
                                             "for a check."))
        [field] = out["form"]
        assert out["outcome"] == "ask" and field["type"] == "email" and field["value"] == "team@synapseorch.com"

    async def test_every_address_is_one_form_one_round(self, client, desk):
        run = await _run()
        out = await _check(run, _plan(prompt="Email the post to team@synapseorch.com."))
        assert [f["value"] for f in out["form"]] == [SITE, "team@synapseorch.com"]
        assert len({f["name"] for f in out["form"]}) == 2
        assert [a["subject"] for a in run.shared_state[T.ASKED_KEY]] == ["address synapseorch.com",
                                                                         "address team@synapseorch.com"]

    async def test_a_public_site_nobody_typed_is_asked_too(self, client, desk):
        run = await _run("Every morning, read the top stories on Hacker News and write me a short digest.")
        out = await _check(run, _plan("https://news.ycombinator.com"))
        assert out["outcome"] == "ask" and out["form"][0]["value"] == "https://news.ycombinator.com"


# ── what the run was given ──────────────────────────────────────────────────────────────────────────────────────

class TestWhatTheRunWasGivenIsNeverAsked:
    @pytest.mark.parametrize("state", [
        {"user_message": f"Read {SITE} each week and post one feature."},
        {"user_message": "Read synapseorch.com each week and post one feature."},
        {"chat_history": [{"role": "user", "content": f"our site is {SITE}"}]},
        {"org_profile": "SynapseOrch (synapseorch.com) builds AI workflows for small teams."},
        {"plan_findings": {"options": [{"provider": "SynapseOrch", "page": f"{SITE}/docs"}]}},
        {"export_source": f"agent = Agent(instructions='Read {SITE}')"},
        {"browser_steps": f"Recordings: 'Product tour' opens {SITE}/tour"},
        {T.ASKED_KEY: [{"about": "what", "subject": "the product's site", "question": "Which site?",
                        "answer": "www.synapseorch.com", "carried": True}]},
    ], ids=["message", "bare-domain", "chat", "profile", "research", "export", "recording", "an-answer"])
    async def test_it_goes_to_the_review(self, client, desk, state):
        run = await _run(**{"user_message": TURN_1, **state})
        out = await _check(run, _plan())
        assert out["outcome"] == "review", out.get("question")

    async def test_an_address_the_workspace_holds(self, client, desk):
        await S.seed_agents([S.make_agent(id="agent_site", name="Site reader", tools=[],
                                          system_prompt=f"You read {SITE} and say what is new.")])
        run = await _run()
        assert (await _check(run, _plan()))["outcome"] == "review"

    async def test_what_an_earlier_step_writes_is_the_runs_own(self, client, desk):
        run = await _run()
        assert (await _check(run, _plan("{{state.find.url}}")))["outcome"] == "review"

    async def test_a_plan_with_no_address_goes_straight_to_its_review(self, client, desk):
        run = await _run("Each week, write a \"do you know…\" post about one feature of our product.")
        write = {**_plan()["steps"][1], "prompt": "Write a \"do you know…\" post about one feature.", "next": "end"}
        out = await _check(run, {**_plan(), "steps": [write]})
        assert out["outcome"] == "review" and T.ASKED_KEY not in run.shared_state


# ── the request's cap holds ─────────────────────────────────────────────────────────────────────────────────────

class TestTheCapHolds:
    async def test_at_the_cap_the_plan_goes_on_and_its_review_names_the_address(self, client, desk):
        run = await _run(**{T.ROUNDS_KEY: T.QUESTIONS})
        out = await _check(run, _plan())
        assert out["outcome"] == "review"
        assert SITE in out["say"] and "haven't given me" in out["say"]


# ── a plan carries no question ──────────────────────────────────────────────────────────────────────────────────

class TestAPlanCarriesNoQuestion:
    def test_the_planner_cannot_write_one(self):
        assert "questions" not in O.plan_schema()["properties"]

    async def test_one_written_anyway_is_asked_first_in_the_form(self, client, desk):
        run = await _run(f"Read {SITE} each week and post one feature on Twitter.")
        sent = await _call("submit_plan", {**_plan(also=[TWEET]), "questions": ["Which day should it post?"]}, run)
        assert sent["questions"] == ["Which day should it post?"], "never dropped on its way to the check"
        out = await _check(run, sent)
        assert out["outcome"] == "ask", "before research, as any question is"
        [field] = out["form"]
        assert field["label"] == "Which day should it post?" and field["type"] == "string"

    def test_code_never_sends_the_planner_to_the_plans_questions(self):
        for module in (T, build_plan):
            assert "the plan's questions" not in inspect.getsource(module), module.__name__

    def test_the_planner_is_told_to_ask_in_one_form(self):
        prompt = " ".join(json.loads((ROOT / "agents" / "planner.json").read_text())["system_prompt"].split())
        assert "every question at once" in prompt and "your best guess" in prompt
        steps = " ".join((ROOT / "prompts" / "plan_steps.md").read_text().split())
        assert "never guessed" in steps


# ── the form ────────────────────────────────────────────────────────────────────────────────────────────────────

FORM = {"question": "Two things before I plan it:", "about": "what", "questions": [
    {"question": "Which page shows the product's features?", "kind": "address", "guess": SITE,
     "subject": "the product's features page"},
    {"question": "Which day should it post?", "kind": "choice", "options": ["Monday", "Friday"],
     "subject": "the posting day", "about": "when"},
]}


class TestTheForm:
    async def test_several_typed_questions_are_one_ask(self, client):
        run = await _run()
        asked = await _call("ask_person", FORM, run)
        assert asked["finish"] == "ask_person" and asked["question"] == "Two things before I plan it:"
        assert [(f["type"], f["label"], f.get("value"), f.get("options")) for f in asked["form"]] == [
            ("string", "Which page shows the product's features?", SITE, None),
            ("choice", "Which day should it post?", None, ["Monday", "Friday"])]
        assert [a["subject"] for a in run.shared_state[T.ASKED_KEY]] == ["the product's features page",
                                                                         "the posting day"]

    async def test_one_already_asked_is_refused_with_its_answer(self, client):
        run = await _run(**{T.ASKED_KEY: [{"about": "when", "subject": "the posting day", "question": "Which day?",
                                           "answer": "Mondays", "carried": True}]})
        out = await _call("ask_person", FORM, run)
        assert "You asked the person this already" in out["error"] and "Mondays" in out["error"]

    async def test_a_secret_in_any_question_is_refused(self, client):
        run = await _run()
        out = await _call("ask_person", {"questions": [FORM["questions"][0],
                                                       {"question": "What is your Twitter API key?"}]}, run)
        assert out["error"] == T.SECRET_ASKED

    async def test_at_most_six(self, client):
        run = await _run()
        many = [{"question": f"Question {n}?", "subject": f"thing {n}"} for n in range(9)]
        assert len((await _call("ask_person", {"questions": many}, run))["form"]) == 6

    async def test_the_single_question_is_as_it_was(self, client):
        run = await _run()
        asked = await _call("ask_person", {"question": "Which time?", "about": "when", "options": ["2am", "3am"]}, run)
        assert asked == {"finish": "ask_person", "question": "Which time?", "about": "when", "options": ["2am", "3am"]}

    async def test_the_build_check_asks_it_and_its_words_number_each(self, client, desk):
        run = await _run()
        asked = await _call("ask_person", FORM, run)
        out = await _check(run, asked)
        assert out["outcome"] == "ask" and out["form"] == asked["form"]
        assert out["question"].splitlines() == [
            "Two things before I plan it:", f"1. Which page shows the product's features? (my guess: {SITE})",
            "2. Which day should it post? (Monday or Friday)"]

    async def test_each_answer_is_its_own(self, client, desk):
        run = await _run()
        asked = await _call("ask_person", FORM, run)
        await _check(run, asked)
        page, day = (f["name"] for f in asked["form"])
        run.shared_state["human_response"] = {"answer": "…", "form": {page: f"{SITE}/features", day: "Friday"}}
        out = await _check(run, _plan(f"{SITE}/features"))
        assert out["asked"][-2:] == [{"question": "Which page shows the product's features?",
                                      "answer": f"{SITE}/features"},
                                     {"question": "Which day should it post?", "answer": "Friday"}]
        assert [a.get("answer") for a in T._with_answers(run.shared_state)] == [f"{SITE}/features", "Friday"]

    async def test_a_typed_reply_answers_the_whole_form(self, client, desk):
        run = await _run()
        asked = await _call("ask_person", FORM, run)
        await _check(run, asked)
        run.shared_state["human_response"] = {"answer": f"{SITE}/features, and Fridays"}
        out = await _check(run, _plan(f"{SITE}/features"))
        assert out["asked"][-1]["answer"] == f"{SITE}/features, and Fridays"
        assert [a.get("answer") for a in T._with_answers(run.shared_state)] == [f"{SITE}/features, and Fridays"] * 2

    async def test_the_change_route_asks_the_same_form(self, client):
        run = await _run(intent="manage")
        asked = await _call("ask_person", FORM, run)
        out = await _call("check_change", {"draft": asked}, run)
        assert out["outcome"] == "ask" and out["form"] == asked["form"]
        assert out["question"].startswith("Two things before I plan it:\n1. ")


class TestTheDefinition:
    @pytest.mark.parametrize("step_id, checked", [("step_human_clarify", "plan_checked"),
                                                  ("step_change_clarify", "change_checked")])
    def test_each_clarify_step_keeps_one_answer_and_sends_its_check(self, step_id, checked):
        """The panel draws the form from the event's `context` (the typed check); a channel, the run page and the
        chat box answer it in words, as one `answer`."""
        step = STEPS[step_id]
        assert [f["name"] for f in step["human_fields"]] == ["answer"]
        assert step["input_keys"][0] == checked
