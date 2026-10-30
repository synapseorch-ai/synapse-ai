"""Synapse AI's steps have room, and every agent's last turn is an answer turn (F1219, part A; Naveen's prod turn).

"Fix all the bugs" ended "Something went wrong. Agent step 'Plan the Build' ran out of turns (6) before answering."
Naveen: "give good turns for builder agents, we never know what might be needed before making a decision." Two
things: the builder's own agent steps had low limits (classify 2, plan 6), and a model on its last turn was only told
not to call tools while still being offered them, so it spent that turn on one more call. Now the builder's agent
steps have at least 40 turns in 600 s, 60 in 1200 s for the ones that read and plan (Naveen's table with the prod
hotfix, HOT3), a quick one-call step aside, and on any agent's last turn only its finish tools are offered (F1015's
answer turn), so it answers from what it has. A step still out of turns says where its limit is raised.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures

from tests.unit.test_an_agent_step_with_no_answer_fails import _agent_step, _entry, _print, _seed, tools  # noqa: F401

SEED = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "orchestration.json"
#: The steps that read the workspace, research, plan or change: 60 turns in 1200 s (Naveen's table, 10-29, with HOT3).
READS_AND_PLANS = {"step_answer_workspace", "step_plan_research", "step_lookup_research", "step_plan", "step_change",
                   "step_make_new"}
#: A quick step is one call: its agent holds only its finish tools, so more turns would offer the same call again.
#: Its own pin keeps it at 2 (`test_the_builders_quick_steps_take_their_own_model`).
#: The classify at a waiting plan is the same one call (F1241's follow-up).
QUICK = {"step_classify", "step_classify_at_plan"}


# ── the floors ───────────────────────────────────────────────────────────────────────────────────────────────────
def _agent_steps() -> list[dict]:
    return [s for s in json.loads(SEED.read_text())["steps"] if s.get("type") == "agent"]


def test_every_builder_agent_step_but_a_quick_one_has_40_turns_in_600_seconds():
    low = {s["id"]: (s.get("max_turns"), s.get("timeout_seconds")) for s in _agent_steps()
           if s["id"] not in QUICK and ((s.get("max_turns") or 0) < 40 or (s.get("timeout_seconds") or 0) < 600)}
    assert not low, f"a builder agent step below 40 turns or 600 s: {low}"


@pytest.mark.parametrize("step_id", sorted(READS_AND_PLANS))
def test_the_steps_that_read_and_plan_have_60_turns_in_1200_seconds(step_id):
    (step,) = [s for s in _agent_steps() if s["id"] == step_id]
    assert step["max_turns"] >= 60 and step["timeout_seconds"] >= 1200


def test_the_floors_name_real_steps():
    names = {s["id"] for s in _agent_steps()}
    assert READS_AND_PLANS | QUICK <= names, "a renamed step would leave its floor unpinned"


# ── the last turn answers ────────────────────────────────────────────────────────────────────────────────────────
def _offered(call: dict) -> list:
    return list(call.get("tools") or [])


class TestTheLastTurnIsAnAnswerTurn:
    async def test_it_is_offered_no_tool_and_its_words_are_the_answer(self, driver, fake_llm, tools):
        orch = S.make_orchestration(id="orch_f1219_ans", entry_step_id="write", steps=[
            _agent_step(nxt="after", max_turns=3), _print("after")])
        await _seed(orch)
        fake_llm.script([tool_call("look", where="shelf 1"), tool_call("look", where="shelf 2"),
                         "The tides turn twice a day."])

        assert await driver.start(orch, "run_f1219_ans") == "completed"
        run = await _run("run_f1219_ans")
        assert run.shared_state["draft"] == "The tides turn twice a day."
        assert _offered(fake_llm.calls[0]) and not _offered(fake_llm.calls[2]), "its last turn is offered none"
        assert "this is your last turn" in json.dumps(fake_llm.calls[2])

    async def test_a_call_on_it_anyway_is_out_of_turns_and_says_where_the_limit_is(self, driver, fake_llm, tools):
        orch = S.make_orchestration(id="orch_f1219_out", entry_step_id="write", steps=[
            _agent_step(nxt="after", max_turns=3), _print("after")])
        await _seed(orch)
        fake_llm.script([tool_call("look", where=f"shelf {i}") for i in range(5)])

        assert await driver.start(orch, "run_f1219_out") == "failed"
        entry = _entry(await _run("run_f1219_out"))
        assert "ran out of turns (3)" in entry["error"] and "Max turns" in entry["error"]
        assert "repeating" not in entry["error"], "out of turns, not the repeat guard's words"

    async def test_a_one_turn_step_keeps_its_tools(self, driver, fake_llm, tools):
        """A step of one turn has no turn to spare for an answer turn: it is offered its tools, as before."""
        orch = S.make_orchestration(id="orch_f1219_one", entry_step_id="write", steps=[
            _agent_step(nxt="after", max_turns=1), _print("after")])
        await _seed(orch)
        fake_llm.script(["Done in one."])

        assert await driver.start(orch, "run_f1219_one") == "completed"
        assert _offered(fake_llm.calls[0])
