"""Rubrics, versions, scores and thumbs.

The rule this file exists to pin is the versioning one. A score is only
comparable to another score taken under the same version of the same rubric, so
what does and does not mint a new version decides whether the dashboard is
telling the truth. Bump too eagerly and the history fragments into windows too
short to have a baseline; bump too rarely and the dashboard compares the answers
to two different questions and calls the difference a regression.
"""
import pytest

from core.evals import store as evals_store
from core.store import evals as store


def _rubric(**overrides):
    return {
        "name": "Research quality",
        "description": "Is it well sourced?",
        "criteria": [{"name": "Sources", "guidance": "At least three URLs"}],
        "scale": {"min": 0, "max": 10},
        "floor": 6.0, "drop": 1.5, "baseline_runs": 5,
        **overrides,
    }


class TestWhatMintsANewRubricVersion:
    """`criteria` and `scale` decide what a number *means*. Nothing else does."""

    async def test_a_new_rubric_starts_at_one(self):
        assert (await evals_store.save_rubric(_rubric()))["version"] == 1

    async def test_renaming_does_not_bump(self):
        first = await evals_store.save_rubric(_rubric())
        again = await evals_store.save_rubric({**first, "name": "Renamed"})
        assert again["version"] == 1

    async def test_rewording_the_description_does_not_bump(self):
        first = await evals_store.save_rubric(_rubric())
        again = await evals_store.save_rubric({**first, "description": "Reworded"})
        assert again["version"] == 1

    async def test_retuning_the_thresholds_does_not_bump(self):
        """Thresholds decide what is *flagged*, not what is *measured* — so
        older scores stay comparable and the baseline survives a retune."""
        first = await evals_store.save_rubric(_rubric())
        again = await evals_store.save_rubric({**first, "floor": 8.0, "drop": 0.5})
        assert again["version"] == 1

    async def test_changing_the_criteria_bumps(self):
        first = await evals_store.save_rubric(_rubric())
        again = await evals_store.save_rubric(
            {**first, "criteria": first["criteria"] + [{"name": "Depth"}]}
        )
        assert again["version"] == 2

    async def test_changing_the_scale_bumps(self):
        first = await evals_store.save_rubric(_rubric())
        again = await evals_store.save_rubric({**first, "scale": {"min": 0, "max": 5}})
        assert again["version"] == 2

    async def test_editing_guidance_bumps_because_it_steers_the_score(self):
        first = await evals_store.save_rubric(_rubric())
        changed = [{**first["criteria"][0], "guidance": "At least ten URLs"}]
        again = await evals_store.save_rubric({**first, "criteria": changed})
        assert again["version"] == 2


class TestIdChurnMustNotLookLikeAScoringChange:
    """The defect this class exists for, found by a route test.

    A client that does not echo a criterion's id back gets a freshly minted one
    from `clean_rubric`. When ids were part of the scoring surface that made
    *every* save look like a criteria change, so a pure rename bumped the
    version and the history fragmented into windows too short to have a
    baseline — the exact failure the versioning rule exists to prevent.
    """

    async def test_a_rename_that_omits_criterion_ids_does_not_bump(self):
        first = await evals_store.save_rubric(_rubric())
        # As a form would send it: same criteria, no ids echoed back.
        again = await evals_store.save_rubric({
            **_rubric(name="Renamed"), "id": first["id"],
        })
        assert again["version"] == 1

    async def test_criterion_ids_survive_an_edit(self):
        """Subscores are keyed by criterion id. A churning id leaves every
        historical breakdown saying 8/10 for a handle nothing can resolve."""
        first = await evals_store.save_rubric(_rubric())
        original_id = first["criteria"][0]["id"]
        again = await evals_store.save_rubric({
            **_rubric(name="Renamed"), "id": first["id"],
        })
        assert again["criteria"][0]["id"] == original_id


class TestTheStoreOwnsTheVersionNotTheCaller:
    async def test_a_supplied_version_is_ignored(self):
        """A client that could set this could make two different rubrics claim
        to be the same one, and every score under either becomes unreadable."""
        first = await evals_store.save_rubric(_rubric())
        again = await evals_store.save_rubric({**first, "version": 99, "name": "Renamed"})
        assert again["version"] == 1


class TestRubricCleaning:
    async def test_criteria_get_ids_so_subscores_have_something_to_key_on(self):
        saved = await evals_store.save_rubric(_rubric())
        assert saved["criteria"][0]["id"]

    async def test_a_nameless_criterion_is_dropped(self):
        saved = await evals_store.save_rubric(_rubric(criteria=[{"name": "  "}, {"name": "Real"}]))
        assert [c["name"] for c in saved["criteria"]] == ["Real"]

    async def test_an_inverted_scale_falls_back_rather_than_reaching_the_judge(self):
        saved = await evals_store.save_rubric(_rubric(scale={"min": 10, "max": 0}))
        assert saved["scale"] == {"min": 0.0, "max": 10.0}

    async def test_unparseable_thresholds_become_off_rather_than_zero(self):
        """Zero is a threshold that never fires for `floor` and always fires for
        `drop`; neither is what "I typed nonsense" should mean."""
        saved = await evals_store.save_rubric(_rubric(floor="abc", drop=""))
        assert saved["floor"] is None and saved["drop"] is None


class TestDeletingARubricLeavesItsScoresAlone:
    async def test_scores_survive_so_history_is_not_tidied_away(self):
        rubric = await evals_store.save_rubric(_rubric())
        await store.append_score({
            "run_id": "r1", "orchestration_id": "o1", "rubric_id": rubric["id"],
            "rubric_version": 1, "score": 9.0,
        })
        assert await evals_store.delete_rubric(rubric["id"]) is True
        assert len(await store.scores_for_run("r1")) == 1


class TestARescoreAppendsRatherThanOverwrites:
    """This is the whole mechanism behind "a rubric edit does not rewrite
    history". Two scores for one run are not a conflict — they are two
    different questions asked of the same work."""

    async def test_both_verdicts_survive(self):
        for version, score in ((1, 9.0), (2, 4.0)):
            await store.append_score({
                "run_id": "r1", "orchestration_id": "o1", "rubric_id": "rub",
                "rubric_version": version, "score": score,
            })
        history = await store.scores_for_run("r1")
        assert len(history) == 2
        assert {h["rubric_version"] for h in history} == {1, 2}

    async def test_the_newest_is_what_a_list_shows(self):
        for version, score in ((1, 9.0), (2, 4.0)):
            await store.append_score({
                "run_id": "r1", "orchestration_id": "o1", "rubric_id": "rub",
                "rubric_version": version, "score": score,
            })
        assert (await store.scores_for_runs(["r1"]))["r1"]["score"] == 4.0


class TestABaselineNeverCrossesARubricVersion:
    async def test_only_the_asked_for_version_comes_back(self):
        """A mean taken across a rubric change averages the answers to two
        different questions."""
        for version, score in ((1, 9.0), (1, 9.0), (1, 9.0), (2, 2.0)):
            await store.append_score({
                "run_id": f"r{version}_{score}", "orchestration_id": "o1",
                "rubric_id": "rub", "rubric_version": version, "score": score,
            })
        v1 = await store.scores_for_orchestration("o1", rubric_id="rub", rubric_version=1)
        assert [s["score"] for s in v1] == [9.0, 9.0, 9.0]

    async def test_the_trend_sees_every_version(self):
        """The dashboard renders the change as a break in the line, which it
        can only do if it is given both sides of it."""
        for version in (1, 2):
            await store.append_score({
                "run_id": f"r{version}", "orchestration_id": "o1",
                "rubric_id": "rub", "rubric_version": version, "score": 5.0,
            })
        assert len(await store.scores_for_orchestration("o1")) == 2


class TestTheTrendWindowIsTakenFromTheRecentEnd:
    async def test_a_limit_returns_the_newest_runs_oldest_first(self):
        for i in range(6):
            await store.append_score({
                "run_id": f"r{i}", "orchestration_id": "o1", "rubric_id": "rub",
                "rubric_version": 1, "score": float(i),
            })
        window = await store.scores_for_orchestration("o1", limit=3)
        # Newest three (3, 4, 5), handed back in the order they happened.
        assert [s["score"] for s in window] == [3.0, 4.0, 5.0]


class TestAThumbIsAnOpinionNotAnEvent:
    async def test_changing_your_mind_replaces(self):
        await store.set_feedback({"run_id": "r1", "verdict": 1, "user_ref": "alice"})
        await store.set_feedback({"run_id": "r1", "verdict": -1, "user_ref": "alice"})
        rows = await store.feedback_for_run("r1")
        assert len(rows) == 1 and rows[0]["verdict"] == -1

    async def test_one_persons_change_of_heart_does_not_erase_a_colleagues(self):
        await store.set_feedback({"run_id": "r1", "verdict": 1, "user_ref": "alice"})
        await store.set_feedback({"run_id": "r1", "verdict": -1, "user_ref": "bob"})
        assert len(await store.feedback_for_run("r1")) == 2

    async def test_withdrawing_removes_only_your_own(self):
        await store.set_feedback({"run_id": "r1", "verdict": 1, "user_ref": "alice"})
        await store.set_feedback({"run_id": "r1", "verdict": 1, "user_ref": "bob"})
        assert await store.clear_feedback("alice", run_id="r1") is True
        rows = await store.feedback_for_run("r1")
        assert [r["user_ref"] for r in rows] == ["bob"]

    async def test_withdrawing_nothing_says_so(self):
        assert await store.clear_feedback("alice", run_id="r1") is False

    async def test_a_chat_turn_is_its_own_target(self):
        await store.set_feedback({"session_id": "s1", "turn_index": 4,
                                  "verdict": 1, "user_ref": "alice"})
        await store.set_feedback({"session_id": "s1", "turn_index": 6,
                                  "verdict": -1, "user_ref": "alice"})
        rows = await store.feedback_for_session("s1")
        assert [(r["turn_index"], r["verdict"]) for r in rows] == [(4, 1), (6, -1)]


class TestSuiteRuns:
    async def test_a_suite_run_reads_honestly_while_it_is_still_going(self):
        created = await store.create_suite_run({
            "id": "sr1", "suite_id": "set1", "orchestration_id": "o1",
            "case_count": 5, "status": "running",
        })
        assert created["scored_count"] == 0 and created["mean_score"] is None
        await store.update_suite_run("sr1", scored_count=2, mean_score=8.0)
        assert (await store.get_suite_run("sr1"))["scored_count"] == 2

    async def test_scores_are_reachable_by_suite_run(self):
        await store.append_score({
            "run_id": "r1", "orchestration_id": "o1", "rubric_id": "rub",
            "rubric_version": 1, "score": 7.0, "suite_run_id": "sr1",
        })
        await store.append_score({
            "run_id": "r2", "orchestration_id": "o1", "rubric_id": "rub",
            "rubric_version": 1, "score": 9.0,
        })
        assert [s["score"] for s in await store.scores_for_suite_run("sr1")] == [7.0]
