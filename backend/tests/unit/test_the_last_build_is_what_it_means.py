"""After a build, "it" is the workflow the conversation just built, and a second of the same name is never saved
(F1229, CAP3's stage 1).

Brief 4: after "Built the workflow 'Sales Analysis by Region'", the person said "run it"; the turn had no target, so it
was planned as a new build ("Where is the CSV?") and saved a second workflow and board. Brief 7: details given after
the build planned a second "Book Train and Hotel for Next Tuesday". Now the route carries the workflow the conversation
built last, and with nothing open on screen and nothing named it is the turn's target (`built`): a build request
about it goes to its update unless the person asks for a new one, and "run it" runs it. And a new workflow named like
one already here is the person's question (F1395, which replaced F1229's send-back to the planner): its own tests are
tests/unit/test_a_name_clash_is_asked.py.
"""
from __future__ import annotations

import pytest

from core.native_builder.classify import _entry
from core.native_builder.target import BUILT, LAST_BUILT_KEY, VIEWING, last_built, resolve_target
from core.orchestration.run_writes import WRITES_KEY

from tests.unit.test_the_build_route_decides_in_code import CHECK, TELL, _call, _plan, _run, workspace  # noqa: F401

ROWS = [{"id": "orch_sales", "name": "Sales Analysis by Region"}, {"id": "orch_other", "name": "Weekly digest"}]


# ── which workflow "it" is ───────────────────────────────────────────────────────────────────────────────────────
class TestTheTarget:
    def test_with_nothing_open_and_nothing_named_it_is_the_last_build(self):
        target = resolve_target("run it", None, ROWS, built_id="orch_sales")
        assert (target.id, target.source) == ("orch_sales", BUILT)

    def test_what_is_open_on_screen_comes_first(self):
        assert resolve_target("run it", "orch_other", ROWS, built_id="orch_sales").source == VIEWING

    def test_a_name_in_the_message_comes_first(self):
        target = resolve_target("run the Weekly digest", None, ROWS, built_id="orch_sales")
        assert target.id == "orch_other"

    def test_a_build_no_longer_here_is_no_target(self):
        assert resolve_target("run it", None, ROWS, built_id="orch_gone").id == ""


class TestTheCarry:
    def test_the_newest_run_s_own_build(self):
        writes = [{"kind": "agent", "action": "created", "id": "agent_x"},
                  {"kind": "orchestration", "action": "created", "id": "orch_sales"}]
        assert last_built({WRITES_KEY: writes}) == "orch_sales"

    def test_a_turn_that_built_nothing_carries_the_one_before(self):
        assert last_built({LAST_BUILT_KEY: "orch_sales"}) == "orch_sales"

    def test_a_deleted_workflow_is_not_it(self):
        assert last_built({WRITES_KEY: [{"kind": "orchestration", "action": "deleted", "id": "orch_sales"}],
                           LAST_BUILT_KEY: "orch_sales"}) == ""

    def test_the_first_turn_has_none(self):
        assert last_built({}) == "" and last_built(None) == ""


class TestWhereABuildRequestGoes:
    @pytest.mark.parametrize("text", [
        "Departure: London King's Cross, destination York, out next Tuesday morning",
        "post the results as a chart instead of a note",
    ])
    def test_more_detail_about_the_last_build_updates_it(self, text):
        assert _entry("build", text, {"current_orchestration_source": BUILT}) == "step_fetch_existing"

    def test_asking_for_a_new_one_still_builds_new(self):
        assert _entry("build", "make me a new one for the weekly digest", {"current_orchestration_source": BUILT}) \
            == "step_plan_room"


# ── the route: brief 4's "run it" is about the workflow just built ──────────────────────────────────────────────
async def test_the_route_s_target_for_run_it_is_the_last_build(client):
    from core.routes.builder import _target_state
    from tests._fakes import seed as S

    await S.seed_orchestrations([S.make_orchestration(id="orch_sales", name="Sales Analysis by Region",
                                                      entry_step_id="end",
                                                      steps=[{"id": "end", "name": "End", "type": "end"}])])
    state = await _target_state("run it", None, "", last_built({WRITES_KEY: [
        {"kind": "orchestration", "action": "created", "id": "orch_sales", "name": "Sales Analysis by Region"}]}))
    assert state["current_orchestration_id"] == "orch_sales" and state["current_orchestration_source"] == BUILT


# ── the check: a second of the same name is the person's question (F1395) ───────────────────────────────────────
class TestASecondOfAName:
    async def test_a_new_workflow_named_like_one_here_is_asked_never_sent_back(self, client):
        from tests._fakes import seed as S

        await S.seed_orchestrations([S.make_orchestration(id="orch_wind", name="Rotterdam Wind Warning",
                                                          entry_step_id="end",
                                                          steps=[{"id": "end", "name": "End", "type": "end"}])])
        run = await _run(**{LAST_BUILT_KEY: "orch_wind"})
        # Never back to the planner, which once answered in words and ended in "Say it another way" (F1395).
        out, _ = await _call("submit_plan", _plan(CHECK, TELL), run)
        assert out.get("finish") == "submit_plan", out
        asked, _ = await _call("check_build", {"draft": out}, run)
        assert asked["outcome"] == "ask", asked
        assert [f["options"][0] for f in asked["form"]] == ["Change “Rotterdam Wind Warning” to do this"], asked

    async def test_a_name_of_its_own_is_built(self, client):
        out, _ = await _call("submit_plan", _plan(CHECK, TELL, title="Rotterdam gusts"), await _run())
        assert out.get("finish") == "submit_plan", out
