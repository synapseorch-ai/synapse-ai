"""A planner that ends in words instead of a finish is explained, never answered with a generic dead end (F1600
hardening).

A browser test on self-hosted: a whole-ecosystem message ran four planner
turns of 6–13k tokens each, one reply "looks like a tool call but did not parse", and the turn ended "I couldn't turn
that into something I can make or change here. Say it another way, or do it on its own page." On cloud the same message
was explained: what can be built here, and choices. The planner had answered in words after its reminder; the step
takes that as its `finish_fallback` ({"finish": "cannot"}, marked `fallback`, F958: never the model's words as its
finish), and code said the one fixed line (change route) or "." (build route: an empty why and nearest).

Now: the builder's planner's unparsed reply is asked for once more, shorter
(`react_engine.unparsed_nudge`; every other agent keeps today's words, word for word); a planner
that still ends in words ends the turn on the explanation path (`outcome: "explain"`, routed to the answer step, which
says what can be built here and offers choices, in its own checked words); never the generic line, never "."; it counts
no question round. A planner's own `cannot`, with its why and what is nearest, is said as before.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import react_engine
from core.native_builder import change_tools as T
from core.native_builder.change_tools import FALLBACK_CANNOT
from test_the_builder_asks_before_it_guesses import _call, _check, _run, desk  # noqa: F401

ROOT = Path(__file__).resolve().parents[2] / "core" / "native_builder"
STEPS = {s["id"]: s for s in json.loads((ROOT / "orchestration.json").read_text())["steps"]}
WORDS = {"finish": "cannot", "fallback": True}


class TestTheBuildRoute:
    async def test_words_instead_of_a_finish_are_explained(self, client, desk):
        out = await _check(await _run(), dict(WORDS))
        assert out["outcome"] == "explain", out.get("say")
        assert out.get("say", "").strip(" .") == "" or FALLBACK_CANNOT not in out.get("say", "")

    async def test_a_planners_own_cannot_is_said_as_before(self, client, desk):
        out = await _check(await _run(), {"finish": "cannot", "why": "This product doesn't build phone apps",
                                          "nearest": "An agent on a phone line can take the calls."})
        assert out["outcome"] == "said" and "phone apps" in out["say"]

    async def test_it_counts_no_question_round(self, client, desk):
        run = await _run()
        before = T.questions_asked(run.shared_state, run)
        await _check(run, dict(WORDS))
        assert T.questions_asked(run.shared_state, run) == before


class TestTheChangeRoute:
    async def test_words_instead_of_a_finish_are_explained_never_the_fixed_line(self, client, desk):
        out = await _call("check_change", {"draft": dict(WORDS)}, await _run(intent="manage"))
        assert out["outcome"] == "explain" and FALLBACK_CANNOT not in (out.get("say") or "")


class TestTheRoutes:
    @pytest.mark.parametrize("switch", ["step_plan_route", "step_check_route"])
    def test_explain_goes_to_the_answer_step(self, switch):
        assert STEPS[switch]["switch_cases"].get("explain") == "step_answer"


#: Today's words to any agent whose reply looked like a tool call but did not parse (react_engine's retry), kept word for
#: word for every agent but the builder's planner (a document or an email written through a tool must
#: never be asked to be shorter).
TODAY = ("\nSystem: that looked like a tool call but it could not be parsed as JSON. Emit the whole call again as one "
         'valid JSON object: {"tool": "<name>", "arguments": {...}}. If you did not mean to call a tool, answer in '
         "plain text with no JSON.\n")


class TestTheRetry:
    def test_the_planners_unparsed_reply_is_asked_for_again_shorter(self):
        nudge = react_engine.unparsed_nudge(react_engine.PLANNER_AGENT_ID)
        assert "shorter" in nudge and "JSON" in nudge

    def test_it_is_the_builders_own_planner(self):
        planner = json.loads((ROOT / "agents" / "planner.json").read_text())
        assert react_engine.PLANNER_AGENT_ID == planner["id"]

    @pytest.mark.parametrize("agent", ["agent_writer", "agent_native_builder_answer", "", None])
    def test_every_other_agent_gets_todays_words(self, agent):
        assert react_engine.unparsed_nudge(agent) == TODAY
