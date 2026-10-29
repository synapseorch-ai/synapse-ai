"""The planner's pack looks up each product feature the request names, before any model plans (F1257, GUIDE1; D340
A, gather).

TRI1's prod run (10-30): "create an eval for my curiosity post generator" was planned as a workflow whose agent
grades another, never told what an eval is here. Now code reads the message for the features it names, by the
census rows' own words (CAP3's table: a row's id, label and `names`), and the pack carries `features`: what Synapse AI
does there or does not yet, with the screen, what imitating it would look like, and the help and docs on it. A
message that names none leaves the pack as it was; what every plan is made of (workflows, agents, tools, runs) is
never looked up as a feature.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, feature_census
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import features as FT
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _pack(message: str) -> dict:
    run = OrchestrationRun(run_id="builder_f1257_pack", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": message})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("read_plan_context", {}, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


class TestWhatAMessageNames:
    @pytest.mark.parametrize("message, ids", [
        ("create an eval for my curiosity post generator", ["evals"]),
        ("Score it against a golden set on a schedule", ["evals", "schedules"]),
        ("Score it against a golden set every Friday", ["evals"]),
        ("change just one knowledge-base note", ["knowledge"]),
        ("Put the summaries on a Kanban board", ["boards"]),
        ("Answer our customers on Telegram and on a website widget", ["messaging", "widgets"]),
        ("Make me a weekly report", []),
        ("Build a workflow whose agent runs a tool on every run", []),
    ])
    def test_by_the_census_rows_own_words(self, message, ids):
        assert [s.id for s in FT.named_in(message)] == ids

    def test_a_word_is_whole_never_part_of_another(self):
        assert FT.named_in("Cardiff's rubrics") == [s for s in feature_census.CENSUS if s.id == "evals"]
        assert FT.named_in("a scoreboard of boarding passes") == [], "score and board only as whole words"

    def test_a_feature_named_in_a_new_census_row_is_found_with_no_change_here(self):
        row = feature_census.Screen("sparks", "Sparks", "/sparks", names=("spark plug",))
        assert [s.id for s in FT.named_in("Fire the spark plugs", census=(row,))] == ["sparks"]

    def test_a_typed_kind_adds_its_feature(self):
        assert [s.id for s in FT.named_in("make it better", also=["evals"])] == ["evals"]

    def test_what_a_plan_is_made_of_is_never_a_feature_and_each_is_a_census_row(self):
        assert FT.PLAN_ITSELF <= {s.id for s in feature_census.CENSUS}


class TestThePack:
    async def test_turn_s_eval_request_carries_what_an_eval_is_here(self, client):
        pack = await _pack("create an eval for my curiosity post generator")
        [evals] = pack["features"]
        row = next(s for s in feature_census.CENSUS if s.id == "evals")
        assert evals["feature"] == "Evals" and evals["screen"] == "/evals"
        assert evals["never_build"] == row.imitation
        assert ("not_from_here" in evals) == bool(row.cant) and ("here" in evals) == bool(row.does)
        assert evals["help"]["steps"], "the '?' panel's own steps for it"
        assert evals["docs"].startswith("https://docs.synapseorch.com/")

    async def test_a_message_naming_no_feature_leaves_the_pack_as_it_was(self, client):
        pack = await _pack("Make me a weekly report")
        assert "features" not in pack

    async def test_one_knowledge_base_note_looks_up_knowledge_alone(self, client):
        """TRI1's ask: a note's change names knowledge and nothing else; the op behind it is its own F-number."""
        pack = await _pack("change just one knowledge-base note")
        assert [f["feature"] for f in pack["features"]] == [
            next(s.label for s in feature_census.CENSUS if s.id == "knowledge")]

    async def test_a_reached_screen_keeps_its_imitation_as_use_the_real_tools(self, client):
        reached = [s for s in feature_census.CENSUS if s.imitation and s.does and s.id not in FT.PLAN_ITSELF]
        if not reached:
            pytest.skip("no reached screen has an imitation in this census")
        pack = await _pack(f"Set up {reached[0].label} for me")
        entry = next(f for f in pack["features"] if f["feature"] == reached[0].label)
        assert entry["never_build"] == reached[0].imitation and entry["here"]
