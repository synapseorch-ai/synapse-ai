"""
Evals — rubrics, judge scores, human feedback and golden sets.

Note the vocabulary, because the product already uses the other word: an
**evaluator** is a routing step inside an orchestration (`StepType.EVALUATOR`)
and has nothing to do with this. What lives here is the **judge** — a model
that scores a finished run against a rubric — and the dashboard over its
verdicts.

Scores are never written by this router on the ordinary path. A run is judged
by the engine at the moment it finishes (`OrchestrationEngine._score_run`), so
the automatic path needs no route at all. What the two scoring endpoints here
do is re-run that judgement on demand: one run, or the last N of an
orchestration. They exist because a rubric written today is worth pointing at
work that already happened.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.evals import regression as regression_mod
from core.evals import store as evals_store

router = APIRouter()

#: How many past runs one backfill call will score. A ceiling rather than a
#: setting: each one is a model call the caller pays for, and a backfill that
#: quietly cost fifty of them would be a surprise on the bill.
MAX_BACKFILL = 25


# ── rubrics ──────────────────────────────────────────────────────────────────

class CriterionIn(BaseModel):
    id: str = ""
    name: str = Field(min_length=1, max_length=evals_store.MAX_NAME_CHARS)
    weight: float = 1.0
    guidance: str = Field(default="", max_length=evals_store.MAX_TEXT_CHARS)


class ScaleIn(BaseModel):
    min: float = 0.0
    max: float = 10.0


class RubricIn(BaseModel):
    name: str = Field(min_length=1, max_length=evals_store.MAX_NAME_CHARS)
    description: str = Field(default="", max_length=evals_store.MAX_TEXT_CHARS)
    criteria: list[CriterionIn] = []
    scale: ScaleIn = ScaleIn()
    floor: float | None = None
    drop: float | None = None
    baseline_runs: int = evals_store.DEFAULT_BASELINE_RUNS
    judge_model: str = ""


@router.get("/api/rubrics")
async def list_rubrics():
    return await evals_store.load_rubrics()


@router.post("/api/rubrics")
async def create_rubric(payload: RubricIn):
    return await evals_store.save_rubric(payload.model_dump())


@router.put("/api/rubrics/{rubric_id}")
async def update_rubric(rubric_id: str, payload: RubricIn):
    """Save an edit. The store, not the caller, decides the version.

    A client that could set `version` could make two different rubrics claim to
    be the same one, and every score taken under either becomes unreadable —
    so the field is not on `RubricIn` at all.
    """
    if await evals_store.get_rubric(rubric_id) is None:
        raise HTTPException(status_code=404, detail="Rubric not found")
    return await evals_store.save_rubric({**payload.model_dump(), "id": rubric_id})


@router.delete("/api/rubrics/{rubric_id}")
async def delete_rubric(rubric_id: str):
    if not await evals_store.delete_rubric(rubric_id):
        raise HTTPException(status_code=404, detail="Rubric not found")
    return {"status": "deleted"}


# ── scores ───────────────────────────────────────────────────────────────────

@router.get("/api/evals/runs/{run_id}")
async def run_scores(run_id: str):
    """Every verdict on one run, newest first, with the thumbs beside them."""
    from core.store import evals as store

    return {
        "scores": await store.scores_for_run(run_id),
        "feedback": await store.feedback_for_run(run_id),
    }


@router.get("/api/evals/orchestrations/{orch_id}")
async def orchestration_trend(orch_id: str, limit: int = 200):
    """The trend, the regressions, and how often the judge matched a person.

    Returned together because they are one screen and one question. The
    agreement figure carries its own denominator — a rate computed from four
    runs should be shown with its sample size or not at all.
    """
    from core.store import evals as store

    scores = await store.scores_for_orchestration(orch_id, limit=max(1, min(limit, 500)))
    feedback = await store.feedback_for_runs([s["run_id"] for s in scores])

    rubric_id = next((s["rubric_id"] for s in reversed(scores)), None)
    rubric = await evals_store.get_rubric(rubric_id) if rubric_id else None
    scale_max = float(((rubric or {}).get("scale") or {}).get("max", 10.0))

    return {
        "scores": scores,
        "feedback": feedback,
        "rubric": rubric,
        "agreement": regression_mod.agreement(scores, feedback, scale_max),
        "regressions": [s for s in scores if s["regression"]],
    }


async def _score_one(run_id: str, suite_run_id: str | None = None) -> dict | None:
    """Re-judge a single run through the same path the engine uses."""
    from core.config import load_settings
    from core.evals import scoring
    from core.orchestration.state import SharedState
    from core.store.resources import get_orchestration

    try:
        run = (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    orch = await get_orchestration(run.orchestration_id)
    if not orch:
        raise HTTPException(status_code=404, detail="Orchestration not found")
    return await scoring.score_run(run, orch, load_settings(), suite_run_id=suite_run_id)


@router.post("/api/evals/runs/{run_id}/score")
async def score_run_now(run_id: str):
    record = await _score_one(run_id)
    if record is None:
        raise HTTPException(
            status_code=400,
            detail="Not scored — the orchestration has no rubric, the run has "
                   "not finished, or the judge did not return a usable verdict.",
        )
    return record


class BackfillIn(BaseModel):
    limit: int = Field(default=10, ge=1, le=MAX_BACKFILL)


@router.post("/api/evals/orchestrations/{orch_id}/backfill")
async def backfill(orch_id: str, payload: BackfillIn):
    """Score the most recent unscored runs of one orchestration.

    Oldest first, so the baseline each run is measured against is built in the
    order the runs actually happened. Scoring newest-first would measure the
    oldest run against runs that came after it, and every verdict in the batch
    would be against a baseline that never existed.
    """
    from core.orchestration.state import SharedState
    from core.store import evals as store

    runs = await SharedState.list_runs(limit=200, orchestration_ids={orch_id})
    existing = await store.scores_for_runs([r["run_id"] for r in runs])
    pending = [r for r in runs if r["run_id"] not in existing]
    pending = list(reversed(pending))[: payload.limit]

    scored = []
    for run in pending:
        try:
            record = await _score_one(run["run_id"])
        except HTTPException:
            continue
        if record is not None:
            scored.append(record)
    return {"requested": len(pending), "scored": len(scored), "scores": scored}


# ── feedback ─────────────────────────────────────────────────────────────────

class FeedbackIn(BaseModel):
    run_id: str | None = None
    session_id: str | None = None
    turn_index: int | None = None
    verdict: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2_000)
    user_ref: str = ""


def _target(payload: FeedbackIn) -> dict:
    """Exactly one target, or a 400 naming what was wrong.

    A row with both a run and a chat turn would be counted twice by the
    agreement figure and shown in two places, so the constraint is enforced at
    the door rather than left to whoever reads the table next.
    """
    has_run = bool(payload.run_id)
    has_turn = bool(payload.session_id) and payload.turn_index is not None
    if has_run == has_turn:
        raise HTTPException(
            status_code=400,
            detail="Give either run_id, or session_id with turn_index — not both, not neither.",
        )
    if has_run:
        return {"run_id": payload.run_id}
    return {"session_id": payload.session_id, "turn_index": payload.turn_index}


@router.post("/api/evals/feedback")
async def leave_feedback(payload: FeedbackIn):
    """Thumb a run or a chat turn. `verdict: 0` withdraws it.

    Zero rather than a DELETE because the control is a toggle: clicking the
    thumb you already gave takes it back, and that is one request from the
    same button rather than two endpoints the client has to choose between.
    """
    from core.store import evals as store

    target = _target(payload)
    if payload.verdict == 0:
        removed = await store.clear_feedback(payload.user_ref, **target)
        return {"status": "cleared" if removed else "absent"}
    return await store.set_feedback(
        {**target, "verdict": payload.verdict,
         "comment": payload.comment, "user_ref": payload.user_ref}
    )


@router.get("/api/evals/feedback/sessions/{session_id}")
async def session_feedback(session_id: str):
    from core.store import evals as store
    return await store.feedback_for_session(session_id)


# ── golden sets ──────────────────────────────────────────────────────────────

class CaseIn(BaseModel):
    id: str = ""
    name: str = Field(default="", max_length=evals_store.MAX_NAME_CHARS)
    input: str = Field(min_length=1, max_length=evals_store.MAX_TEXT_CHARS)
    notes: str = Field(default="", max_length=evals_store.MAX_TEXT_CHARS)


class SuiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=evals_store.MAX_NAME_CHARS)
    orchestration_id: str = Field(min_length=1)
    cases: list[CaseIn] = []


class GenerateIn(BaseModel):
    count: int = Field(default=5, ge=1, le=10)
    steer: str = Field(default="", max_length=1_000)


@router.get("/api/evals/suites")
async def list_suites():
    from core.evals import suites as suites_mod

    out = []
    for suite in await evals_store.load_suites():
        out.append({
            **suite,
            "estimate": suites_mod.estimate(suite),
            "running": await suites_mod.running_for(suite["id"]) is not None,
        })
    return out


@router.post("/api/evals/suites")
async def create_suite(payload: SuiteIn):
    return await evals_store.save_suite(payload.model_dump())


@router.put("/api/evals/suites/{suite_id}")
async def update_suite(suite_id: str, payload: SuiteIn):
    if await evals_store.get_suite(suite_id) is None:
        raise HTTPException(status_code=404, detail="Set not found")
    return await evals_store.save_suite({**payload.model_dump(), "id": suite_id})


@router.delete("/api/evals/suites/{suite_id}")
async def delete_suite(suite_id: str):
    if not await evals_store.delete_suite(suite_id):
        raise HTTPException(status_code=404, detail="Set not found")
    return {"status": "deleted"}


@router.post("/api/evals/suites/{suite_id}/generate")
async def generate_cases(suite_id: str, payload: GenerateIn):
    """Propose cases. **Nothing is saved** — the caller edits and accepts.

    A generator that wrote straight to the set would be putting model-authored
    text into the thing the model is later measured against, which is how a
    golden set quietly starts testing what the model finds easy.
    """
    from core.config import load_settings
    from core.evals import generate
    from core.store.resources import get_orchestration

    suite = await evals_store.get_suite(suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="Set not found")
    orch = await get_orchestration(suite.get("orchestration_id") or "")
    if not orch:
        raise HTTPException(status_code=404, detail="Orchestration not found")

    rubric = await evals_store.get_rubric(orch.get("rubric_id") or "")
    cases = await generate.propose(
        orch, rubric, load_settings(), count=payload.count, steer=payload.steer
    )
    return {"cases": cases, "saved": False}


@router.post("/api/evals/suites/{suite_id}/run")
async def run_suite(suite_id: str, request: Request):
    """Start a suite run. Returns as soon as it is under way.

    Detached deliberately: N runs takes minutes, and a request held open for
    them would be a timeout on every deployment that has one. Progress is read
    from the suite-run row, which is written as each case lands.
    """
    from core.evals import suites as suites_mod

    suite = await evals_store.get_suite(suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="Set not found")
    if not (suite.get("cases") or []):
        raise HTTPException(status_code=400, detail="This set has no cases yet.")
    if await suites_mod.running_for(suite_id) is not None:
        raise HTTPException(status_code=409, detail="This set is already running.")

    server_module = getattr(request.app.state, "server_module", None)
    suite_run_id = suites_mod.new_suite_run_id()

    async def _drive():
        try:
            await suites_mod.run_suite(suite, server_module, suite_run_id=suite_run_id)
        except Exception as exc:  # noqa: BLE001 - a detached task must not vanish silently
            from core.log import get_logger
            get_logger(__name__).warning(f"suite run '{suite_run_id}' failed: {exc}")

    from core.background import spawn_background
    spawn_background(_drive(), name=f"eval-suite-{suite_run_id}")
    return {"suite_run_id": suite_run_id, "status": "running",
            "estimate": suites_mod.estimate(suite)}


@router.post("/api/evals/suites/{suite_id}/cancel")
async def cancel_suite(suite_id: str):
    """Stop after the case in flight. Nothing is rolled back.

    The runs that already happened are real runs that really cost money, and
    pretending otherwise by hiding them would make the bill unexplainable.
    """
    from core.evals import suites as suites_mod

    suite_run_id = await suites_mod.running_for(suite_id)
    if suite_run_id is None or not await suites_mod.cancel(suite_run_id):
        raise HTTPException(status_code=404, detail="Nothing running for this set.")
    return {"status": "cancelling", "suite_run_id": suite_run_id}


@router.get("/api/evals/suites/{suite_id}/runs")
async def suite_history(suite_id: str, limit: int = 20):
    from core.store import evals as store

    runs = await store.suite_runs_for(suite_id, limit=max(1, min(limit, 100)))
    for run in runs:
        run["scores"] = await store.scores_for_suite_run(run["id"])
    return runs
