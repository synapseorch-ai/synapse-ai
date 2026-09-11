"""The evals HTTP surface.

Scores are not written here on the ordinary path — the engine judges a run the
moment it finishes — so what these routes mostly do is let a rubric written
today be pointed at work that already happened, and let a person disagree with
the judge.
"""
import pytest

from _fakes import seed as S
from core.store import evals as store

RUBRIC = {
    "name": "Quality",
    "description": "Is it any good?",
    "criteria": [{"name": "Sources", "guidance": "At least three"}],
    "scale": {"min": 0, "max": 10},
    "floor": 6.0, "drop": 1.5, "baseline_runs": 5,
}
VERDICT = '{"score": 8.0, "reasoning": "Solid.", "criteria": {}, "steps": {}}'


class TestRubricCrud:
    async def test_create_list_update_delete(self, client):
        created = (await client.post("/api/rubrics", json=RUBRIC)).json()
        assert created["version"] == 1

        listed = (await client.get("/api/rubrics")).json()
        assert [r["id"] for r in listed] == [created["id"]]

        renamed = await client.put(f"/api/rubrics/{created['id']}",
                                   json={**RUBRIC, "name": "Renamed"})
        assert renamed.json()["version"] == 1, "a rename must not invalidate history"

        rescoped = await client.put(f"/api/rubrics/{created['id']}",
                                    json={**RUBRIC, "criteria": [{"name": "Different"}]})
        assert rescoped.json()["version"] == 2

        assert (await client.delete(f"/api/rubrics/{created['id']}")).status_code == 200
        assert (await client.get("/api/rubrics")).json() == []

    async def test_editing_something_that_is_not_there_is_a_404(self, client):
        assert (await client.put("/api/rubrics/nope", json=RUBRIC)).status_code == 404

    async def test_deleting_something_that_is_not_there_is_a_404(self, client):
        assert (await client.delete("/api/rubrics/nope")).status_code == 404

    async def test_the_caller_cannot_choose_the_version(self, client):
        """Not merely ignored — the field is not on the request model at all."""
        created = (await client.post("/api/rubrics", json={**RUBRIC, "version": 99})).json()
        assert created["version"] == 1


class TestScoringOnDemand:
    async def test_a_finished_run_can_be_scored_after_the_fact(self, client, fake_llm):
        fake_llm.set_default(VERDICT)
        rubric = (await client.post("/api/rubrics", json=RUBRIC)).json()

        orch = S.make_orchestration(id="orch_1", entry_step_id="p", rubric_id=rubric["id"],
                                    steps=[{"id": "p", "name": "Say", "type": "print",
                                            "print_content": "hi", "output_key": "final",
                                            "next_step_id": None}])
        await S.seed_orchestrations([orch])
        await client.post("/api/orchestrations/orch_1/run", json={"input": "go"})

        runs = (await client.get("/api/orchestrations/runs")).json()
        run_id = runs[0]["run_id"]

        scored = await client.post(f"/api/evals/runs/{run_id}/score")
        assert scored.status_code == 200 and scored.json()["score"] == 8.0

    async def test_scoring_an_unknown_run_is_a_404(self, client):
        assert (await client.post("/api/evals/runs/nope/score")).status_code == 404

    async def test_scoring_without_a_rubric_explains_itself(self, client, fake_llm):
        """A 400 that names the three reasons, because "not scored" is
        otherwise indistinguishable from "broken"."""
        orch = S.make_orchestration(id="orch_bare", entry_step_id="p",
                                    steps=[{"id": "p", "name": "Say", "type": "print",
                                            "print_content": "hi", "output_key": "final",
                                            "next_step_id": None}])
        await S.seed_orchestrations([orch])
        await client.post("/api/orchestrations/orch_bare/run", json={"input": "go"})
        run_id = (await client.get("/api/orchestrations/runs")).json()[0]["run_id"]

        refused = await client.post(f"/api/evals/runs/{run_id}/score")
        assert refused.status_code == 400
        assert "rubric" in refused.json()["detail"]


class TestTheRunsListCarriesTheScore:
    async def test_an_unscored_run_reads_as_no_score_rather_than_zero(self, client):
        orch = S.make_orchestration(id="orch_2", entry_step_id="p",
                                    steps=[{"id": "p", "name": "Say", "type": "print",
                                            "print_content": "hi", "output_key": "final",
                                            "next_step_id": None}])
        await S.seed_orchestrations([orch])
        await client.post("/api/orchestrations/orch_2/run", json={"input": "go"})

        row = (await client.get("/api/orchestrations/runs")).json()[0]
        assert row["score"] is None and row["score_regression"] is False

    async def test_a_scored_run_shows_its_latest_verdict(self, client, fake_llm):
        fake_llm.set_default(VERDICT)
        rubric = (await client.post("/api/rubrics", json=RUBRIC)).json()
        orch = S.make_orchestration(id="orch_3", entry_step_id="p", rubric_id=rubric["id"],
                                    steps=[{"id": "p", "name": "Say", "type": "print",
                                            "print_content": "hi", "output_key": "final",
                                            "next_step_id": None}])
        await S.seed_orchestrations([orch])
        await client.post("/api/orchestrations/orch_3/run", json={"input": "go"})

        row = (await client.get("/api/orchestrations/runs")).json()[0]
        assert row["score"] == 8.0


class TestTheTrendEndpoint:
    async def test_it_returns_the_series_the_regressions_and_the_agreement(self, client):
        for i, score in enumerate([9.0, 8.0, 3.0]):
            await store.append_score({
                "run_id": f"r{i}", "orchestration_id": "orch_t", "rubric_id": "rub",
                "rubric_version": 1, "score": score,
                "regression": score < 6, "rule": "floor" if score < 6 else None,
            })
        await store.set_feedback({"run_id": "r0", "verdict": 1, "user_ref": "alice"})

        body = (await client.get("/api/evals/orchestrations/orch_t")).json()
        assert [s["score"] for s in body["scores"]] == [9.0, 8.0, 3.0]
        assert len(body["regressions"]) == 1
        assert body["agreement"] == {"compared": 1, "agreed": 1, "rate": 1.0}

    async def test_an_orchestration_nobody_scored_is_empty_not_an_error(self, client):
        body = (await client.get("/api/evals/orchestrations/never")).json()
        assert body["scores"] == [] and body["agreement"]["rate"] is None


class TestFeedback:
    async def test_a_thumb_on_a_run(self, client):
        posted = await client.post("/api/evals/feedback",
                                   json={"run_id": "r1", "verdict": 1, "user_ref": "alice"})
        assert posted.status_code == 200 and posted.json()["verdict"] == 1

    async def test_zero_withdraws_it(self, client):
        """The control is a toggle, so taking it back is the same button and
        the same request rather than a second endpoint."""
        await client.post("/api/evals/feedback",
                          json={"run_id": "r1", "verdict": 1, "user_ref": "alice"})
        cleared = await client.post("/api/evals/feedback",
                                    json={"run_id": "r1", "verdict": 0, "user_ref": "alice"})
        assert cleared.json()["status"] == "cleared"
        assert await store.feedback_for_run("r1") == []

    async def test_a_thumb_on_a_chat_turn(self, client):
        posted = await client.post("/api/evals/feedback",
                                   json={"session_id": "s1", "turn_index": 3,
                                         "verdict": -1, "user_ref": "alice"})
        assert posted.json()["turn_index"] == 3

        listed = (await client.get("/api/evals/feedback/sessions/s1")).json()
        assert [f["verdict"] for f in listed] == [-1]

    @pytest.mark.parametrize("payload,why", [
        ({"verdict": 1}, "neither a run nor a turn"),
        ({"run_id": "r1", "session_id": "s1", "turn_index": 1, "verdict": 1}, "both"),
    ])
    async def test_exactly_one_target_is_required(self, client, payload, why):
        """A row with both would be counted twice by the agreement figure and
        shown in two places."""
        assert (await client.post("/api/evals/feedback", json=payload)).status_code == 400

    async def test_a_verdict_outside_the_range_is_refused(self, client):
        assert (await client.post("/api/evals/feedback",
                                  json={"run_id": "r1", "verdict": 5})).status_code == 422
