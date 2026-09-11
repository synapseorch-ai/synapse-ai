"""The judge fires when a real run finishes — and only when it should.

This is the file that matters most. The hook lives in `_execute_loop`, which is
the one place *every* path passes through: `run()` delegates to it and
`WorkerEngineAdapter.resume()` enters it directly, so an in-process run, an
in-process resume, a fleet run and a fleet resume all reach it. Hanging the
judge off the runner pump instead would have been invisible to the fleet — the
mistake this codebase has already made twice (D81, and again with the widget),
and the one `worker/notify.py` in cloud is a written-up post-mortem of.

A test that only ever drove `run()` would pass over a hook that fires on one
path and not the other, so the resume case here is not redundant.
"""
import types

import pytest

from _fakes import seed as S
from core.evals import store as evals_store
from core.store import evals as store

VERDICT = '{"score": 8.0, "reasoning": "Solid.", "criteria": {}, "steps": {}}'


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _collect(agen):
    return [ev async for ev in agen]


async def _rubric(**overrides):
    return await evals_store.save_rubric({
        "name": "Quality", "criteria": [{"name": "Overall"}],
        "scale": {"min": 0, "max": 10}, **overrides,
    })


def _orch_dict(rubric_id: str, **overrides):
    return S.make_orchestration(
        id=overrides.pop("id", "orch_scored"),
        entry_step_id="p",
        steps=[{"id": "p", "name": "Say", "type": "print", "print_content": "hello",
                "output_key": "final", "next_step_id": None}],
        rubric_id=rubric_id,
        **overrides,
    )


async def _run(orch_dict, run_id: str, fake_llm):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    fake_llm.set_default(VERDICT)
    await S.seed_orchestrations([orch_dict])
    orch = Orchestration.model_validate(orch_dict)
    return await _collect(OrchestrationEngine(orch, _server()).run("go", run_id=run_id))


class TestTheHookFiresOnAFinishedRun:
    async def test_a_bound_rubric_produces_a_score(self, fake_llm):
        rubric = await _rubric()
        await _run(_orch_dict(rubric["id"]), "run_scored", fake_llm)

        scores = await store.scores_for_run("run_scored")
        assert len(scores) == 1
        assert scores[0]["score"] == 8.0
        assert scores[0]["rubric_id"] == rubric["id"]
        assert scores[0]["rubric_version"] == rubric["version"]

    async def test_the_judge_call_is_attributed_to_the_scored_run(self, fake_llm):
        """`source` beginning `orchestration:` is what makes the usage summary
        bucket this call under the run it judged, so "what did this eval cost"
        is answerable without a second accounting path."""
        rubric = await _rubric()
        await _run(_orch_dict(rubric["id"]), "run_attributed", fake_llm)

        judge_calls = [c for c in fake_llm.calls if c.get("source") == "orchestration:judge"]
        assert len(judge_calls) == 1
        assert judge_calls[0]["run_id"] == "run_attributed"

    async def test_no_rubric_means_no_judge_call_at_all(self, fake_llm):
        """The opt-in is the binding. An orchestration nobody asked to have
        judged must not spend a single token on it."""
        await _run(_orch_dict(""), "run_unscored", fake_llm)

        assert await store.scores_for_run("run_unscored") == []
        assert [c for c in fake_llm.calls if c.get("source") == "orchestration:judge"] == []

    async def test_a_dangling_rubric_id_is_not_an_error(self, fake_llm):
        """Deleting a rubric leaves the id on every orchestration that used it.
        That has to read as "not evaluated", not as a broken run."""
        events = await _run(_orch_dict("rub_deleted"), "run_dangling", fake_llm)

        assert await store.scores_for_run("run_dangling") == []
        assert any(e.get("type") == "orchestration_complete" for e in events)


class TestScoringCanNeverFailTheRun:
    async def test_a_judge_that_raises_leaves_the_run_completed(self, fake_llm, monkeypatch):
        """The run is already finished and its result already belongs to the
        customer. An opinion about it must not be able to take it away."""
        rubric = await _rubric()

        async def _explode(*a, **kw):
            raise RuntimeError("judge is down")

        from core.evals import scoring
        monkeypatch.setattr(scoring, "score_run", _explode)

        events = await _run(_orch_dict(rubric["id"]), "run_judge_down", fake_llm)

        complete = next(e for e in events if e.get("type") == "orchestration_complete")
        assert complete["status"] == "completed"
        assert await store.scores_for_run("run_judge_down") == []

    async def test_an_unusable_verdict_records_nothing_rather_than_a_zero(self, fake_llm):
        """A zero is a verdict. Recording one because a model replied with prose
        would flag a regression that never happened."""
        rubric = await _rubric()
        fake_llm.set_default("I am unable to evaluate this run.")
        await S.seed_orchestrations([_orch_dict(rubric["id"])])

        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = Orchestration.model_validate(_orch_dict(rubric["id"]))
        await _collect(OrchestrationEngine(orch, _server()).run("go", run_id="run_prose"))

        assert await store.scores_for_run("run_prose") == []


class TestTheHookIsOnTheSharedLoopNotTheEntryPoint:
    """`_execute_loop` is shared between `run()` and `resume()`.

    A run that pauses for a human and is then resumed finishes *inside*
    `_execute_loop` without ever re-entering `run()` — which is exactly how the
    fleet finishes a resumed run. If the hook had been placed in `run()`, this
    test would be the one that caught it.
    """

    async def test_a_resumed_run_is_scored_when_it_finishes(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        rubric = await _rubric()
        orch_dict = S.make_orchestration(
            id="orch_resumed",
            entry_step_id="h",
            steps=[
                {"id": "h", "name": "Approve", "type": "human",
                 "human_prompt": "ok?", "output_key": "approval",
                 "input_keys": [], "next_step_id": "p"},
                {"id": "p", "name": "Show", "type": "print",
                 "print_content": "done", "output_key": "final", "next_step_id": None},
            ],
            rubric_id=rubric["id"],
        )
        await S.seed_orchestrations([orch_dict])
        fake_llm.set_default(VERDICT)

        orch = Orchestration.model_validate(orch_dict)
        paused = await _collect(
            OrchestrationEngine(orch, _server()).run("go", run_id="run_resumed")
        )
        assert any(e.get("type") == "human_input_required" for e in paused)
        # Nothing scored yet: a paused run has not finished, and judging half of
        # someone's work is worse than not judging it.
        assert await store.scores_for_run("run_resumed") == []

        await _collect(
            OrchestrationEngine.resume("run_resumed", {"decision": "yes"}, _server())
        )

        scores = await store.scores_for_run("run_resumed")
        assert len(scores) == 1 and scores[0]["score"] == 8.0


class TestRegressionsAreMeasuredAgainstWhatCameBefore:
    async def test_the_baseline_excludes_the_run_being_judged(self, fake_llm):
        """Measuring a run partly against itself drags the mean toward whatever
        just happened, and a sustained decline reads as a series of small ones."""
        rubric = await _rubric(floor=None, drop=0.5, baseline_runs=10)
        for i in range(3):
            await store.append_score({
                "run_id": f"seed_{i}", "orchestration_id": "orch_scored",
                "rubric_id": rubric["id"], "rubric_version": rubric["version"],
                "score": 9.0,
            })

        await _run(_orch_dict(rubric["id"]), "run_measured", fake_llm)

        score = (await store.scores_for_run("run_measured"))[0]
        assert score["baseline"] == 9.0        # not (9+9+9+8)/4
        assert score["regression"] is True     # 8.0 is more than 0.5 below 9.0
        assert score["rule"] == "drop"

    async def test_a_healthy_run_is_recorded_without_a_rule(self, fake_llm):
        rubric = await _rubric(floor=6.0, drop=None)
        await _run(_orch_dict(rubric["id"]), "run_healthy", fake_llm)

        score = (await store.scores_for_run("run_healthy"))[0]
        assert score["regression"] is False and score["rule"] is None

    async def test_a_regression_reaches_the_notification_hub(self, fake_llm):
        """"Surfaced before customers see them" is the promise; the bell is
        where it is kept."""
        from core.notifications import hub

        rubric = await _rubric(floor=9.5)
        await _run(_orch_dict(rubric["id"]), "run_flagged", fake_llm)

        published = [n for n in hub.list() if n["kind"] == "eval_regression"]
        assert len(published) == 1
        assert published[0]["run_id"] == "run_flagged"
        assert "9.5" in published[0]["body"]
