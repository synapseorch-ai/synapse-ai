"""When the person says they don't know, the change route stops asking and proposes a change they can approve (F1310,
CAP3's prod-07 on 63dfb505; 79 11-03).

prod-07 asked "why did my run fail? fix it". The diagnosis was right (Fetch prices, price_feed, HTTP 404), but the
change route asked five questions, each on a new subject, so neither the request's cap nor its "asked already" rule
held them; four came after the person had said they were not sure, and the request ended in words ("I cannot
proceed…"), with no card. Now an answer to our own question that says the person doesn't know (a short closed list,
read only to stop asking: it never acts and never fills a value) stops questions on that subject; two such answers
stop them all, saying what to propose; a change request that then ends in words is sent back once to propose the
change card; and the change route's question step, run out, goes back to the planner, not to "nothing was changed".
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import change_tools as CT
from core.native_builder.seed import bundled_definitions
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


def _asked(subject: str, answer: str, question: str = "What is the correct endpoint URL for price_feed?") -> dict:
    return {"about": "what", "subject": subject, "question": question, "answer": answer, "carried": True}


async def _run(*asked: dict, intent: str = "manage") -> OrchestrationRun:
    run = OrchestrationRun(run_id="builder_f1310", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": intent,
                                         "user_message": "why did my run fail? fix it", CT.ASKED_KEY: list(asked),
                                         CT.ROUNDS_KEY: len(asked)})
    await SharedState(run).checkpoint()
    return run


async def _call(run, tool: str, args: dict) -> dict:
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _ask(subject: str, question: str) -> dict:
    return {"question": question, "about": "what", "subject": subject}


DONT_KNOW = "don't know"


class TestADontKnowStopsAsking:
    @pytest.mark.parametrize("answer", ["I’m not sure what the correct URL should be.",
                                        "I'm not sure the URL is wrong, it worked yesterday",
                                        "I don't have the right URL – can you check the supplier’s docs?"])
    async def test_it_stops_questions_on_that_subject(self, answer):
        run = await _run(_asked("price_feed tool endpoint", answer))
        out = await _call(run, "ask_person", _ask("price_feed tool endpoint", "Which URL should price_feed call now?"))
        assert DONT_KNOW in out.get("error", ""), out

    async def test_and_nothing_else(self):
        run = await _run(_asked("price_feed tool endpoint", "I'm not sure the URL is wrong, it worked yesterday"))
        out = await _call(run, "ask_person", _ask("schedule day", "Which day should the price check run on?"))
        assert out.get("finish") == "ask_person", out

    async def test_an_ordinary_answer_does_not_trip_it(self):
        run = await _run(_asked("price_feed tool endpoint", "It calls https://feeds.example.com/prices with a key."))
        out = await _call(run, "ask_person", _ask("schedule day", "Which day should the price check run on?"))
        assert out.get("finish") == "ask_person", out
        out = await _call(run, "ask_person", _ask("price_feed tool endpoint", "Which URL should price_feed call now?"))
        assert DONT_KNOW not in out.get("error", ""), "a question asked already is that rule's, not this one's"

    async def test_two_dont_knows_stop_every_question_and_say_what_to_propose(self):
        run = await _run(_asked("price_feed tool call", "I’m not sure what parameters it sends."),
                         _asked("price_feed tool endpoint", "I’m not sure what the correct URL should be."))
        out = await _call(run, "ask_person", _ask("schedule day", "Which day should the price check run on?"))
        assert DONT_KNOW in out.get("error", "") and "edit_steps" in out["error"], out


class TestAChangeRequestDoesNotEndInWords:
    async def test_after_a_dont_know_words_go_back_once_then_stand(self):
        run = await _run(_asked("price_feed tool endpoint", "I’m not sure what the correct URL should be."))
        said = {"text": "I cannot proceed without the correct endpoint URL. Ask your team for it."}
        first = await _call(run, "say", said)
        assert "edit_steps" in first.get("error", "") and "does not fix" in first["error"], first
        assert (await _call(run, "say", said)).get("finish") == "say", "sent back once: the planner's to keep"

    async def test_a_cannot_goes_back_once_too(self):
        run = await _run(_asked("price_feed tool endpoint", "I’m not sure what the correct URL should be."))
        out = await _call(run, "cannot", {"why": "I don't know the right URL", "nearest": "Ask your team"})
        assert "edit_steps" in out.get("error", ""), out

    async def test_with_nothing_unknown_words_stand(self):
        run = await _run(_asked("price_feed tool endpoint", "It calls https://feeds.example.com/prices."))
        assert (await _call(run, "say", {"text": "Your run failed on Fetch prices."})).get("finish") == "say"

    async def test_a_build_is_not_held_to_it(self):
        run = await _run(_asked("price_feed tool endpoint", "I’m not sure."), intent="create")
        assert (await _call(run, "say", {"text": "Here is what I know."})).get("finish") == "say"


def test_the_change_routes_question_step_run_out_goes_back_to_the_planner():
    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    assert steps["step_change_clarify"]["on_exhausted_step_id"] == "step_plan"


def test_the_planner_is_told_so_too():
    prompt = json.dumps(bundled_definitions()["agents"])
    assert "22. When the person says they don't know" in prompt


# ── the card says what it does not fix (79's B2) ─────────────────────────────────────────────────────────────────
# A step's change (edit_steps hands it to the build route's update, whose plan card the person approves) does not fix
# the tool the step calls: where the person said they don't know, the card says so plainly, with the Tools screen.
async def test_the_steps_card_says_it_does_not_fix_the_tool(client):
    from unit.test_the_build_route_decides_in_code import _check, _run as _build_run

    existing = {"id": "orch_prices", "name": "Daily price check", "description": "", "entry_step_id": "fetch",
                "steps": [{"id": "fetch", "name": "Fetch prices", "type": "tool", "forced_tool": "price_feed",
                           "output_key": "fetch", "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]}
    run = await _build_run(intent="update", existing_orch=existing, current_orchestration_id="orch_prices",
                           **{CT.ASKED_KEY: [_asked("price_feed tool endpoint", "I’m not sure what the URL should be.")]})
    draft = {"finish": "submit_plan", "kind": "update", "title": "Daily price check", "summary": "s",
             "edits": [{"op": "set_meta", "fields": {"description": "Stops and says why when price_feed fails."}}]}
    checked, _ = await _check(run, draft)
    review = checked.get("review") or ""
    assert "does not fix the tool" in review and "Tools" in review, review[:600]


async def test_with_nothing_unknown_the_card_says_nothing_of_it(client):
    from unit.test_the_build_route_decides_in_code import _check, _run as _build_run

    existing = {"id": "orch_prices", "name": "Daily price check", "description": "", "entry_step_id": "done",
                "steps": [{"id": "done", "name": "End", "type": "end"}]}
    run = await _build_run(intent="update", existing_orch=existing, current_orchestration_id="orch_prices")
    draft = {"finish": "submit_plan", "kind": "update", "title": "Daily price check", "summary": "s",
             "edits": [{"op": "set_meta", "fields": {"description": "Runs daily."}}]}
    checked, _ = await _check(run, draft)
    assert "does not fix the tool" not in (checked.get("review") or "")
