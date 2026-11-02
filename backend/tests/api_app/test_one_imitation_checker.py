"""One checker says when a plan rebuilds a screen by hand (F1257 step 3, GUIDE1; D340 A, TRI1's change 4).

F1255 (BLD1) found the first one in code: a create plan that runs a workflow here and has a model judge what it wrote is
Evals made by hand. The census already says what a stand-in for each screen would look like (`imitation`, CAP3's rows),
so the check is the census's: `imitation.SIGNATURES` holds a code matcher for a row by its id, and `findings` reads
every row a matcher has. It is pure (the plan and the workspace only), so a planner can call it on its own draft;
`check_build` says what it finds where it said F1255's. Evals is the only matcher for now (8a/a0 10-31): the other
rows reach the planner as their words in the pack, and gain a matcher when a real request shows one.
"""
from __future__ import annotations

import inspect

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools, feature_census  # noqa: F401 — the builder's modules, as at boot
from core.native_builder import build_plan, imitation, objects as O


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _eval_copy() -> O.Plan:
    return O.Plan.from_draft({"kind": "create", "title": "Post Generator Eval", "summary": "Scores the posts.",
                              "steps": [{"id": "gen", "type": "workflow", "name": "Run it", "does": "Runs it",
                                         "workflow": "orch_posts", "next": "judge"},
                                        {"id": "judge", "type": "llm", "name": "Judge", "does": "Scores it",
                                         "prompt": "Score {{state.gen}} from 1 to 10.", "next": "end"}]})


@pytest.fixture
async def ws(client):
    from core.store import get_store

    await get_store()
    await S.seed_agents([S.make_agent(id="agent_writer", name="Post writer")])
    await S.seed_orchestrations([S.make_orchestration(
        id="orch_posts", name="Curiosity post generator",
        steps=[{"id": "write", "name": "Write", "type": "agent", "agent_id": "agent_writer",
                "prompt_template": "Write about {{ state.user_input }}.", "next_step_id": None}],
        entry_step_id="write")])
    return await build_plan.workspace(_Server())


class TestTheSignatures:
    def test_each_is_a_census_row_that_says_what_its_imitation_looks_like(self):
        rows = {s.id: s for s in feature_census.CENSUS}
        assert imitation.SIGNATURES, "no matcher at all"
        for key in imitation.SIGNATURES:
            assert key in rows, f"{key} is no census row"
            assert rows[key].imitation, f"{key}'s row says no imitation for its matcher to find"

    def test_evals_is_one(self):
        assert "evals" in imitation.SIGNATURES

    def test_the_check_is_pure(self):
        assert not inspect.iscoroutinefunction(imitation.findings)
        assert list(inspect.signature(imitation.findings).parameters) == ["plan", "ws"]


class TestTheCheck:
    async def test_a_plan_that_runs_one_and_judges_it_is_evals_by_its_census_row(self, ws):
        found = imitation.findings(_eval_copy(), ws)
        assert [(f["screen"], f["label"], f["href"]) for f in found] == [("evals", "Evals", "/evals")]
        assert found[0]["say"].startswith('That is an eval of "Curiosity post generator": Evals scores its runs')

    async def test_running_one_and_then_sending_it_is_a_workflow(self, ws):
        plan = O.Plan.from_draft({"kind": "create", "title": "Post and send", "summary": "Writes and sends.",
                                  "steps": [{"id": "gen", "type": "workflow", "name": "Run it", "does": "Runs it",
                                             "workflow": "orch_posts", "next": "send"},
                                            {"id": "send", "type": "tool", "name": "Send", "does": "Sends it",
                                             "tool": "send_email", "next": "end"}]})
        assert imitation.findings(plan, ws) == []

    async def test_a_row_with_no_matcher_is_never_found(self, ws, monkeypatch):
        monkeypatch.setattr(imitation, "SIGNATURES", {})
        assert imitation.findings(_eval_copy(), ws) == []

    async def test_a_host_census_without_the_row_finds_nothing(self, ws, monkeypatch):
        # Cloud's census is its own (`feature_census.set_census`): a matcher answers only for a row the host has.
        monkeypatch.setattr(feature_census, "current", lambda: tuple(s for s in feature_census.CENSUS
                                                                      if s.id != "evals"))
        assert imitation.findings(_eval_copy(), ws) == []

    async def test_another_row_s_matcher_says_its_own_row(self, ws, monkeypatch):
        # Every row is read the same way: a matcher for Templates is said from the Templates row.
        monkeypatch.setattr(imitation, "SIGNATURES", {"templates": lambda plan, ws: True})
        found = imitation.findings(_eval_copy(), ws)
        assert [(f["screen"], f["href"]) for f in found] == [("templates", "/templates")]
        assert "Templates" in found[0]["say"] and "/templates" in found[0]["say"]


class TestTheBuildCheckSaysIt:
    async def test_imitates_eval_is_the_checker_s_evals_row(self, ws, monkeypatch):
        assert build_plan.imitates_eval(_eval_copy(), ws).startswith('That is an eval of "Curiosity post generator"')
        monkeypatch.setattr(imitation, "SIGNATURES", {})
        assert build_plan.imitates_eval(_eval_copy(), ws) is None

    def test_check_build_asks_the_checker(self):
        assert "imitation.findings(" in inspect.getsource(build_plan.check_build)
