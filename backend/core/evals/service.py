"""What Evals does, once, for every door that does it (F1255, BLD1; the one-place rule, F1189).

The Evals screen's routes (`core.routes.evals`), the product tools Synapse AI and MCP clients call
(`core.product_tools.evals`) and a test harness (D340's gate) call these, so each door says the same thing for the
same reason. Plain async functions over dicts: no request, no response type. A refusal is an `EvalsRefused` with the
words the screen has always shown and the HTTP status its route answers with.

A host whose Evals do more (cloud: its own routes, roles, plan counts, a credit check before a run, worker jobs) puts
its own in front of the product tools (`core.product_tools.evals.set_evals_host`); these stay self-hosted's.
"""
from __future__ import annotations

from typing import Any

from core.evals import store as evals_store

#: How many past runs one backfill call will score: each one is a model call the caller pays for.
MAX_BACKFILL = 25
#: How many cases one proposal writes (the screen's own ceiling).
MAX_PROPOSED = 10


class EvalsRefused(Exception):
    """Why Evals did not do it, in the screen's words, with the status its route answers."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail, self.status = detail, status


# ── rubrics ──────────────────────────────────────────────────────────────────

def rubric_body(payload: dict) -> dict:
    """A rubric as the screen's form sends it, held to its own rules (`RubricIn`); never a version: the store sets it,
    so two rubrics never claim to be the same one."""
    from pydantic import ValidationError

    from core.routes.evals import RubricIn

    try:
        return RubricIn.model_validate(payload).model_dump()
    except ValidationError as exc:
        raise EvalsRefused(_said(exc), 422) from None


async def list_rubrics() -> list[dict]:
    return await evals_store.load_rubrics()


async def get_rubric(rubric_id: str) -> dict:
    rubric = await evals_store.get_rubric(rubric_id) if rubric_id else None
    if rubric is None:
        raise EvalsRefused("Rubric not found", 404)
    return rubric


async def save_rubric(payload: dict, rubric_id: str | None = None) -> dict:
    """Make a rubric, or save an edit of one (`rubric_id`)."""
    body = rubric_body(payload)
    if rubric_id:
        await get_rubric(rubric_id)
        body["id"] = rubric_id
    return await evals_store.save_rubric(body)


async def delete_rubric(rubric_id: str) -> None:
    if not await evals_store.delete_rubric(rubric_id):
        raise EvalsRefused("Rubric not found", 404)


async def attach_rubric(orch_id: str, rubric_id: str) -> dict:
    """Score a workflow's runs with a rubric: its `rubric_id`, which the engine judges each finished run by. "" takes
    it off. Only a workflow a person can open, and only a rubric that is here."""
    from core.builder_tools import _visible_orchestration
    from core.store.resources import save_orchestration

    orch = await _visible_orchestration(orch_id)
    if not isinstance(orch, dict):
        raise EvalsRefused("Workflow not found", 404)
    if rubric_id:
        await get_rubric(rubric_id)
    await save_orchestration({**orch, "rubric_id": rubric_id})
    return {"orchestration_id": orch["id"], "name": orch.get("name"), "rubric_id": rubric_id}


# ── golden sets ──────────────────────────────────────────────────────────────

def suite_body(payload: dict, *, whole: bool = False) -> dict:
    """A golden set as the screen's form sends it (`SuiteIn`): a name, its workflow, its cases. The store keeps at most
    `MAX_CASES`; `whole` refuses more rather than keeping the first ones, for a door that must never drop a case the
    person approved (Synapse AI's card, F1255)."""
    from pydantic import ValidationError

    from core.routes.evals import SuiteIn

    try:
        body = SuiteIn.model_validate(payload).model_dump()
    except ValidationError as exc:
        raise EvalsRefused(_said(exc), 422) from None
    if whole and len(body["cases"]) > evals_store.MAX_CASES:
        raise EvalsRefused(f"A golden set holds at most {evals_store.MAX_CASES} cases.", 422)
    return body


async def list_suites() -> list[dict]:
    from core.evals import suites as suites_mod

    return [{**suite, "estimate": suites_mod.estimate(suite),
             "running": await suites_mod.running_for(suite["id"]) is not None}
            for suite in await evals_store.load_suites()]


async def get_suite(suite_id: str) -> dict:
    suite = await evals_store.get_suite(suite_id) if suite_id else None
    if suite is None:
        raise EvalsRefused("Set not found", 404)
    return suite


async def save_suite(payload: dict, suite_id: str | None = None, *, whole: bool = False) -> dict:
    body = suite_body(payload, whole=whole)
    if suite_id:
        await get_suite(suite_id)
        body["id"] = suite_id
    return await evals_store.save_suite(body)


async def delete_suite(suite_id: str) -> None:
    """Its past runs stay. Never one that is running (F508)."""
    from core.evals import suites as suites_mod

    await get_suite(suite_id)
    if await suites_mod.running_for(suite_id) is not None:
        raise EvalsRefused("Stop the set's run first.", 409)
    if not await evals_store.delete_suite(suite_id):
        raise EvalsRefused("Set not found", 404)


async def propose_cases(orch: dict, *, count: int = 5, steer: str = "") -> list[dict]:
    """Cases a model proposes for a workflow's golden set. **Nothing is saved**: a person accepts them, or a golden set
    quietly starts testing what the model finds easy."""
    from core.config import load_settings
    from core.evals import generate

    if not 1 <= int(count) <= MAX_PROPOSED:
        raise EvalsRefused(f"Propose between 1 and {MAX_PROPOSED} cases.", 422)
    rubric = await evals_store.get_rubric(orch.get("rubric_id") or "") if orch.get("rubric_id") else None
    try:
        return await generate.propose(orch, rubric, load_settings(), count=int(count), steer=steer or "")
    except generate.CasesNotWritten as exc:
        raise EvalsRefused(str(exc), 502) from None  # the model call failed: said, never an empty answer (F512)


async def propose_for_suite(suite_id: str, *, count: int = 5, steer: str = "") -> list[dict]:
    from core.store.resources import get_orchestration

    suite = await get_suite(suite_id)
    orch = await get_orchestration(suite.get("orchestration_id") or "")
    if not orch:
        raise EvalsRefused("Workflow not found", 404)
    return await propose_cases(orch, count=count, steer=steer)


async def start_suite_run(suite_id: str, server_module: Any) -> dict:
    """Start a golden set's run, detached: N runs take minutes. Progress is read from the suite-run row."""
    from core.background import spawn_background
    from core.evals import suites as suites_mod

    suite = await get_suite(suite_id)
    if not (suite.get("cases") or []):
        raise EvalsRefused("This set has no cases yet.", 400)
    if await suites_mod.running_for(suite_id) is not None:
        raise EvalsRefused("This set is already running.", 409)
    suite_run_id = suites_mod.new_suite_run_id()

    async def _drive():
        try:
            await suites_mod.run_suite(suite, server_module, suite_run_id=suite_run_id)
        except Exception as exc:  # noqa: BLE001 - a detached task must not vanish silently
            from core.log import get_logger
            get_logger(__name__).warning(f"suite run '{suite_run_id}' failed: {exc}")

    spawn_background(_drive(), name=f"eval-suite-{suite_run_id}")
    return {"suite_run_id": suite_run_id, "status": "running", "estimate": suites_mod.estimate(suite)}


async def cancel_suite_run(suite_id: str) -> dict:
    """Stop after the case in flight. Nothing is rolled back: those runs really ran."""
    from core.evals import suites as suites_mod

    suite_run_id = await suites_mod.running_for(suite_id)
    if suite_run_id is None or not await suites_mod.cancel(suite_run_id):
        raise EvalsRefused("Nothing running for this set.", 404)
    return {"status": "cancelling", "suite_run_id": suite_run_id}


async def suite_history(suite_id: str, limit: int = 20) -> list[dict]:
    from core.store import evals as store

    runs = await store.suite_runs_for(suite_id, limit=max(1, min(limit, 100)))
    for run in runs:
        run["scores"] = await store.scores_for_suite_run(run["id"])
    return runs


# ── scores ───────────────────────────────────────────────────────────────────

async def trend(orch_id: str, limit: int = 200) -> dict:
    """A workflow's scores, its regressions, and how often the judge matched a person, with its own denominator."""
    from core.evals import regression as regression_mod
    from core.store import evals as store

    scores = await store.scores_for_orchestration(orch_id, limit=max(1, min(limit, 500)))
    feedback = await store.feedback_for_runs([s["run_id"] for s in scores])
    rubric_id = next((s["rubric_id"] for s in reversed(scores)), None)
    rubric = await evals_store.get_rubric(rubric_id) if rubric_id else None
    scale_max = float(((rubric or {}).get("scale") or {}).get("max", 10.0))
    return {"scores": scores, "feedback": feedback, "rubric": rubric,
            "agreement": regression_mod.agreement(scores, feedback, scale_max),
            "regressions": [s for s in scores if s["regression"]]}


async def judge_once(rubric: dict, input_text: str, output_text: str, settings: dict | None = None) -> dict | None:
    """Score one input and its output against a rubric with the product's own judge (`judge.judge_run`), with no saved
    workflow or run: D340 E's gate scores a builder's plan this way. The verdict, or None when the judge gave none."""
    import uuid
    from types import SimpleNamespace

    from core.evals import judge

    if settings is None:
        from core.config import load_settings

        settings = load_settings()
    run = SimpleNamespace(run_id=f"judge_once_{uuid.uuid4().hex[:12]}", session_id=None, status="completed",
                          step_history=[{"step_id": "answer", "step_name": "Answer", "step_type": "agent",
                                         "status": "completed", "output": output_text}],
                          shared_state={"user_input": input_text, "answer": output_text})
    return await judge.judge_run(run, evals_store.clean_rubric(dict(rubric)), dict(settings), result_key="answer")


def _said(exc: Any) -> str:
    """A validation error in the form's own words: each field and what is wrong with it."""
    parts = []
    for err in exc.errors()[:5]:
        where = ".".join(str(p) for p in err.get("loc") or ()) or "it"
        parts.append(f"{where}: {err.get('msg')}")
    return "; ".join(parts)
