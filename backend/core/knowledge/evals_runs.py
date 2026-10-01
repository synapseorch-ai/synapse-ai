"""Running a knowledge base's question set: one search per question per snapshot, scored five ways (V1, D168).

**A run** is a `knowledge_eval_runs` row and one callable, `run(kb_id,
run_id)` — spawned in this process in OSS, a worker job in cloud (V1c). It
searches each question once in each chosen snapshot exactly as the knowledge
base serves it (`top_k` at least 10, rerank as configured unless the run says
otherwise), traced as the eval's own (`surface "eval"`, session
`knowledge-eval:<run id>`, so every cost row of the run sums by that session).
From each search come the five rankings of `evals_metrics`.

**Comparing snapshots** — an index built another way, before or after a
settings change — is done only on the questions every chosen snapshot can
answer: a document a building snapshot has not reached yet, or a passage no
longer in an edited document ("stale"), takes its question out of the
comparison. A snapshot still building is marked not comparable until it holds
every document: with fewer documents competing its scores read high.

**Money.** Before a run starts its searches are priced (query embeddings and
reranks) and checked against the base's monthly budget; eval searches count
toward it, as every search does. A search that finds the base at its budget
(`budget_paused`) stops the run. The run's cost is what `usage_logs` holds for
its session.

**Cancel** sets `cancelling`; the run stops before its next question and keeps
what it scored. A run left `running` by a process that stopped is
`interrupted` (marked at startup, or when read and seen stale here).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from core.knowledge import evals_metrics as EM
from core.knowledge.evals import EvalBusy, EvalNotFound
from core.knowledge.ids import EVAL_RUN_PREFIX, new_id
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import (
        EvalCompareRequest, EvalConfigResult, EvalEstimate, EvalRun, EvalRunRequest, EvalRunSummary,
    )
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

SESSION_PREFIX = "knowledge-eval:"
#: A run not touched for this long, with no task in this process, was left by a process that stopped.
STALE_AFTER = timedelta(minutes=5)
#: Snapshots a run may search.
SEARCHABLE = ("active", "building", "retired")
#: The served passages a case result shows.
TOP_SHOWN = 3
SCORED = ("lexical", "vector", "hybrid", "served")

_tasks: dict[str, asyncio.Task] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at: datetime | None) -> datetime | None:
    return None if at is None else (at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc))


def _tenant() -> str:
    from core.tenancy import get_tenant

    return get_tenant()


def session_of(run_id: str) -> str:
    return SESSION_PREFIX + run_id


def tasks() -> dict[str, asyncio.Task]:
    """The runs this process is driving (tests await them)."""
    return _tasks


# ── what a run searches ───────────────────────────────────────────────────────

async def snapshots_for(kb: KnowledgeBase, wanted: Sequence[str]) -> list[dict[str, Any]]:
    """The snapshots a run searches, each as `EvalSnapshotInfo` fields: the ones named (active, building or
    retired within its grace), else the serving one."""
    from sqlalchemy import select

    from core.knowledge.snapshots import active_snapshots
    from core.store import session
    from core.store.models import KnowledgeSnapshotDB as S

    ids = list(dict.fromkeys(wanted)) or [s for s in [(await active_snapshots([kb.id])).get(kb.id)] if s]
    if not ids:
        raise ValueError("Nothing in this knowledge base is indexed yet, so there is nothing to search.")
    async with session() as s:
        rows = {r.id: r for r in (await s.execute(select(S).where(
            S.tenant_id == _tenant(), S.kb_id == kb.id, S.id.in_(ids)))).scalars().all()}
    out = []
    for snap in ids:
        row = rows.get(snap)
        if row is None:
            raise EvalNotFound(f"No snapshot {snap!r} of this knowledge base.")
        if row.status not in SEARCHABLE:
            raise ValueError(f"Snapshot {snap} failed to build; its passages are gone, so it cannot be searched.")
        building = row.status == "building"
        out.append({
            "snapshot_id": row.id, "status": row.status,
            "docs_done": row.docs_done if building else None, "docs_total": row.docs_total if building else None,
            "comparable": not building or row.docs_done >= row.docs_total,
            "embed_model": row.embed_model, "chunker_version": row.chunker_version,
            "settings": (row.stats or {}).get("settings") or {}})
    return out


def query_settings(kb: KnowledgeBase) -> dict[str, Any]:
    """The settings a search reads at query time."""
    return {"rerank": kb.rerank.model_dump(mode="json"), "fusion": kb.fusion.model_dump(mode="json"),
            "retrieval": kb.retrieval.model_dump(mode="json")}


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def eval_top_k(kb: KnowledgeBase) -> int:
    """At least 10 passages, so @10 is scored; no more than the knowledge base would give, past that."""
    return max(10, kb.retrieval.top_k)


def rerank_plan(kb: KnowledgeBase, wanted: bool | None, settings: Mapping[str, Any] | None = None
                ) -> tuple[bool, str | None]:
    """(whether the run's searches rerank, with which model), as `search.search` decides it."""
    from core.knowledge.config import knowledge_settings

    model = kb.rerank.model or knowledge_settings(settings).rerank_model or None
    if wanted is not None:
        return bool(wanted and model), model
    if kb.rerank.enabled is not None:
        return bool(kb.rerank.enabled and model), model
    return bool(model), model


# ── the estimate ──────────────────────────────────────────────────────────────

async def estimate(kb: KnowledgeBase, body: EvalRunRequest, settings: Mapping[str, Any] | None = None
                   ) -> EvalEstimate:
    """What the run would cost: its searches (counted toward the budget), and its answers and verdicts."""
    from core.knowledge import evals
    from core.knowledge.api_models import EvalCostLine, EvalEstimate
    from core.knowledge.budgets import embed_estimate_usd
    from core.knowledge.ledger import kb_month_spend
    from core.knowledge.rerankers import route_for

    cases = await evals.cases_of(kb.id, body.set_id)
    questions = [c["question"] for c in cases]
    snaps = await snapshots_for(kb, body.snapshot_ids)
    lines = []
    for snap in snaps:
        model = snap["embed_model"] or ""
        lines.append(EvalCostLine(part="search", model=model or "keyword only", calls=len(questions),
                                  input_tokens=sum(_tokens(q) for q in questions),
                                  est_usd=await embed_estimate_usd(model, questions) if model else 0.0,
                                  counts_toward_budget=True))
    on, model = rerank_plan(kb, body.rerank, settings)
    if on and questions:
        route = route_for(model or "")
        rate = getattr(route, "usd_per_search", None)
        lines.append(EvalCostLine(part="search", model=model or "", calls=len(questions) * len(snaps),
                                  est_usd=None if rate is None else rate * len(questions) * len(snaps),
                                  counts_toward_budget=True))
    if body.judge is not None:
        from core.knowledge import evals_judge

        lines.extend(await evals_judge.estimate(kb, questions, body.judge))
    unpriced = sorted({line.model for line in lines if line.est_usd is None})
    remaining = None
    if kb.budget_monthly_usd is not None:
        remaining = round(max(0.0, kb.budget_monthly_usd - await kb_month_spend(kb.id)), 6)
    return EvalEstimate(lines=lines, total_est_usd=round(sum(line.est_usd or 0.0 for line in lines), 8),
                        unpriced=unpriced, budget_remaining_usd=remaining)


def _tokens(text: str) -> int:
    from core.knowledge.chunker import estimate_tokens

    return estimate_tokens(text)


def _search_usd(est: EvalEstimate) -> float:
    return sum(line.est_usd or 0.0 for line in est.lines if line.counts_toward_budget)


# ── starting ──────────────────────────────────────────────────────────────────

async def start(kb: KnowledgeBase, body: EvalRunRequest, *, started_by: str = "person",
                settings: Mapping[str, Any] | None = None) -> EvalRunSummary:
    """Check, price and record a run, then start it. A base whose budget the searches would cross is refused
    (a 422); a run started by a settings change records itself `skipped` instead."""
    from sqlalchemy.exc import IntegrityError

    from core.background import spawn_background
    from core.knowledge import evals
    from core.knowledge.errors import BudgetExceeded
    from core.knowledge.ledger import check_budget
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    cases = await evals.cases_of(kb.id, body.set_id)
    if not cases:
        raise ValueError("This question set has no questions yet.")
    if body.judge is not None:
        from core.knowledge import evals_judge

        evals_judge.check_families(body.judge)
    snaps = await snapshots_for(kb, body.snapshot_ids)
    est = await estimate(kb, body, settings)
    on, model = rerank_plan(kb, body.rerank, settings)
    config = {"snapshots": snaps, "rerank": body.rerank, "rerank_on": on, "rerank_model": model,
              "top_k": eval_top_k(kb), "agent_sees": kb.retrieval.top_k,
              "judge": body.judge.model_dump() if body.judge else None, "query_settings": query_settings(kb),
              "fingerprints": {s["snapshot_id"]: fingerprint(s["settings"], query_settings(kb)) for s in snaps}}
    run_id = new_id(EVAL_RUN_PREFIX)
    row = R(id=run_id, tenant_id=_tenant(), kb_id=kb.id, set_id=body.set_id, status="queued",
            started_by=started_by, config=config, case_count=len(cases), est_usd=est.total_est_usd)
    try:
        await check_budget(kb, _search_usd(est))
    except BudgetExceeded as exc:
        if started_by == "person":
            raise ValueError(f"{exc} Raise the budget, or run fewer questions.") from None
        row.status, row.error, row.finished_at = "skipped", str(exc), _now()
    try:
        async with session() as s:
            s.add(row)
    except IntegrityError:
        raise EvalBusy("A run of this question set is already under way.") from None
    if row.status == "queued":
        _tasks[run_id] = spawn_background(_drive(kb.id, run_id), name=f"knowledge-eval:{run_id}")
    async with session() as s:
        return evals.run_summary(await s.get(R, run_id))


async def _drive(kb_id: str, run_id: str) -> None:
    try:
        await run(kb_id, run_id)
    finally:
        _tasks.pop(run_id, None)
        from core.knowledge import evals_auto

        await evals_auto.after_run(kb_id, run_id)  # a re-run that waited for this one


# ── the run ───────────────────────────────────────────────────────────────────

async def _update(run_id: str, **values: Any) -> None:
    from sqlalchemy import update

    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        await s.execute(update(R).where(R.tenant_id == _tenant(), R.id == run_id).values(
            updated_at=_now(), **values))


async def _status(run_id: str) -> str | None:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        return (await s.execute(select(R.status).where(R.tenant_id == _tenant(), R.id == run_id))).scalar_one_or_none()


async def _held(snapshot_id: str, cases: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """case id → "ok", "missing" (its document is not in this snapshot yet) or "stale" (its passage is not)."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeChunkDB as C

    doc_ids = sorted({ev["doc_id"] for case in cases for ev in case["evidence"] if ev.get("doc_id")})
    by_doc: dict[str, list[EM.Passage]] = {}
    if doc_ids:
        async with session() as s:
            for i in range(0, len(doc_ids), 500):
                for row in (await s.execute(select(C).where(
                        C.tenant_id == _tenant(), C.snapshot_id == snapshot_id,
                        C.doc_id.in_(doc_ids[i:i + 500])))).scalars().all():
                    by_doc.setdefault(row.doc_id, []).append(EM.Passage.of(row))
    out = {}
    for case in cases:
        state = "ok"
        for ev in case["evidence"]:
            passages = by_doc.get(ev.get("doc_id") or "", [])
            if not passages:
                state = "missing"
                break
            if not any(EM.covers(p, ev) for p in passages):
                state = "stale"
        out[case["id"]] = state
    return out


async def run(kb_id: str, run_id: str) -> None:
    """Drive run `run_id` to its end. Never raises: a failure is the run's `error`."""
    from core.knowledge import bases, evals
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        row = await s.get(R, run_id)
    if row is not None and row.status == "cancelling":  # cancelled before it began
        await _update(run_id, status="cancelled", finished_at=_now())
        return
    if row is None or row.status != "queued":
        return
    await _update(run_id, status="running", started_at=_now())
    try:
        kb = await bases.require_base(kb_id)
        cases = await evals.cases_of(kb_id, row.set_id)
        status, error, results = await _search_all(kb, run_id, dict(row.config or {}), cases)
        summary = await score(kb, dict(row.config or {}), results)
        if status == "completed" and (row.config or {}).get("judge"):
            from core.knowledge import evals_judge

            status, error = await evals_judge.grade(kb, run_id, dict(row.config or {}), results, summary)
        await _update(run_id, status=status, error=error, results=results, summary=summary,
                      done_count=len(results["cases"]), cost_usd=await spent(run_id), finished_at=_now())
    except Exception as exc:  # noqa: BLE001 — a run's failure is its own, never the server's
        _log.warning(f"knowledge eval {run_id} failed: {exc}")
        await _update(run_id, status="failed", error=str(exc)[:2000], cost_usd=await spent(run_id),
                      finished_at=_now())


async def _search_all(kb: KnowledgeBase, run_id: str, config: dict, cases: list[dict]
                      ) -> tuple[str, str | None, dict]:
    from core.knowledge.api_models import SearchRequest
    from core.knowledge.errors import KnowledgeError
    from core.knowledge.search import load_chunks, search
    from core.knowledge.trace import Origin

    snaps = [s["snapshot_id"] for s in config["snapshots"]]
    held = {snap: await _held(snap, cases) for snap in snaps}
    origin = Origin(surface="eval", session_id=session_of(run_id))
    out: list[dict] = []
    gone: dict[str, str] = {}
    status, error = "completed", None
    for n, case in enumerate(cases):
        if await _status(run_id) == "cancelling":
            status = "cancelled"
            break
        entry = {"id": case["id"], "kind": case["kind"], "question": case["question"],
                 "evidence": len(case["evidence"]), "by_snapshot": {}}
        for snap in snaps:
            if snap in gone:
                entry["by_snapshot"][snap] = {"state": "gone"}
                continue
            t0 = time.monotonic()
            try:
                response = await search(SearchRequest(
                    query=case["question"][:2000], kb_ids=[kb.id], snapshot_ids={kb.id: snap}, mode="hybrid",
                    rerank=config.get("rerank"), top_k=config["top_k"], include_trace=True), origin=origin)
            except KnowledgeError as exc:
                if exc.code == "snapshot_not_found":  # retired past its grace mid-run
                    gone[snap] = str(exc)
                    entry["by_snapshot"][snap] = {"state": "gone"}
                    continue
                raise
            legs = EM.legs_of(response.trace, kb.id)
            views = await load_chunks(legs.ids)
            passages = {cid: EM.Passage.of(v.row) for cid, v in views.items()}
            rel = EM.relevance(passages, case["evidence"])
            where = {cid: i for i, cid in enumerate(legs.ids)}
            entry["by_snapshot"][snap] = {
                "state": held[snap].get(case["id"], "ok"), "legs": legs.to_json(),
                "rel": {str(where[cid]): held_ev for cid, held_ev in rel.items()},
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "top": [{"path": h.citation.path, "snippet": h.citation.snippet, "relevant": h.chunk_id in rel}
                        for h in response.hits[:TOP_SHOWN]]}
            if config.get("judge") and snap == snaps[0]:  # what the answer is drawn from; dropped once graded
                entry["by_snapshot"][snap]["hits"] = [h.model_dump(mode="json") for h in response.hits]
            if "budget_paused" in response.degraded:
                status = "failed"
                error = (f"The knowledge base reached its monthly budget at question {n + 1} of {len(cases)}; "
                         "the run stopped there.")
        out.append(entry)
        await _update(run_id, done_count=len(out))
        if status == "failed":
            break
    notes = [f"Snapshot {snap} was dropped during the run: {why}" for snap, why in gone.items()]
    return status, error, {"cases": out, "notes": notes}


# ── scoring ───────────────────────────────────────────────────────────────────

def _case_rankings(entry: dict, snap: str, kb_id: str, *, config: str, rrf_k: int | None = None,
                   weights: Mapping[str, float] | None = None) -> tuple[list[str] | None, dict[str, list[int]]]:
    per = entry["by_snapshot"].get(snap) or {}
    if "legs" not in per:
        return None, {}
    legs = EM.CaseLegs.from_json(per["legs"])
    rel = {legs.ids[int(i)]: held for i, held in (per.get("rel") or {}).items()}
    return EM.ranking(legs, config, kb_id=kb_id, snapshot_id=snap, rrf_k=rrf_k, weights=weights), rel


def compared_cases(results: dict, snaps: Sequence[str]) -> list[dict]:
    """The cases every snapshot searched and holds the evidence of."""
    return [c for c in results["cases"]
            if all((c["by_snapshot"].get(s) or {}).get("state") == "ok" for s in snaps)]


def config_result(kb_id: str, snap: str, config: str, cases: Sequence[dict], *, reranked: bool = False,
                  agent_sees: int | None = None, rrf_k: int | None = None,
                  weights: Mapping[str, float] | None = None) -> dict[str, Any]:
    """One row of a run's results: a configuration's scores in one snapshot, over `cases`."""
    legs = [EM.CaseLegs.from_json(c["by_snapshot"][snap]["legs"]) for c in cases]
    why = EM.unavailable(legs, config)
    row: dict[str, Any] = {
        "key": f"{snap}:{config}" if config != "custom" else f"{snap}:custom:{rrf_k}:{json.dumps(weights)}",
        "snapshot_id": snap, "config": config,
        "label": EM.label(config, reranked=reranked, rrf_k=rrf_k, weights=weights),
        "available": why is None, "reason": why, "agent_sees": agent_sees if config == "served" else None,
        "weights": dict(weights) if weights else None, "rrf_k": rrf_k}
    if why is not None:
        return row
    scores: list[EM.CaseScore] = []
    kinds: dict[str, list[EM.CaseScore]] = {}
    for case in cases:
        order, rel = _case_rankings(case, snap, kb_id, config=config, rrf_k=rrf_k, weights=weights)
        if order is None:
            continue
        scored = EM.score(order, rel, case["evidence"])
        scores.append(scored)
        kinds.setdefault(case["kind"], []).append(scored)
    row["scores"] = EM.aggregate(scores)
    row["by_kind"] = {kind: EM.aggregate(group) for kind, group in sorted(kinds.items())}
    return row


async def score(kb: KnowledgeBase, config: dict, results: dict) -> dict[str, Any]:
    """The run's summary: each snapshot × configuration scored over the questions every snapshot can answer."""
    snaps = [s["snapshot_id"] for s in config["snapshots"]]
    cases = compared_cases(results, snaps)
    rows = []
    for snap in snaps:
        reranked = any(EM.CaseLegs.from_json(c["by_snapshot"][snap]["legs"]).reranked for c in cases)
        for name in SCORED:
            rows.append(config_result(kb.id, snap, name, cases, reranked=reranked,
                                      agent_sees=config.get("agent_sees")))
    states = [(c["by_snapshot"].get(s) or {}).get("state") for c in results["cases"] for s in snaps]
    notes = list(results.get("notes") or [])
    missing = sum(1 for c in results["cases"] if any((c["by_snapshot"].get(s) or {}).get("state") == "missing"
                                                     for s in snaps))
    if missing:
        notes.append(f"{missing} question{'s' if missing != 1 else ''} left out: a snapshot does not hold the "
                     "document yet.")
    for snap in config["snapshots"]:
        if not snap.get("comparable", True):
            notes.append(f"Snapshot {snap['snapshot_id']} was still building ({snap.get('docs_done')} of "
                         f"{snap.get('docs_total')} documents): with fewer documents competing its scores read high.")
    return {"compared_cases": len(cases), "stale_cases": sum(1 for s in states if s == "stale"),
            "snapshots": config["snapshots"], "results": rows, "notes": notes}


async def spent(run_id: str) -> float:
    """What the run has cost: every `usage_logs` row of its session (searches through the ledger, chat calls
    through the chat path)."""
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import UsageLogDB

    async with session() as s:
        total = (await s.execute(select(func.coalesce(func.sum(UsageLogDB.estimated_cost), 0.0)).where(
            UsageLogDB.tenant_id == _tenant(), UsageLogDB.session_id == session_of(run_id)))).scalar_one()
    return round(float(total or 0.0), 8)


# ── reading, cancelling, comparing ────────────────────────────────────────────

async def _row(kb_id: str, run_id: str):
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        row = await s.get(R, run_id)
    if row is None or row.tenant_id != _tenant() or row.kb_id != kb_id:
        raise EvalNotFound(f"No run {run_id!r} of this knowledge base.")
    if row.status in ("queued", "running", "cancelling") and run_id not in _tasks and (
            _now() - (_aware(row.updated_at) or _now())) > STALE_AFTER:
        await _update(run_id, status="interrupted", error="The server stopped during the run.", finished_at=_now())
        async with session() as s:
            row = await s.get(R, run_id)
    return row


def case_results(row: Any) -> list[dict[str, Any]]:
    """Each question's result, as `api_models.EvalCaseResult` fields."""
    config = dict(row.config or {})
    snaps = [s["snapshot_id"] for s in config.get("snapshots") or []]
    out = []
    for entry in (row.results or {}).get("cases") or []:
        ranks: dict[str, int | None] = {}
        found: dict[str, int] = {}
        query_ids = {}
        stale = trimmed = False
        for snap in snaps:
            per = entry["by_snapshot"].get(snap) or {}
            stale = stale or per.get("state") == "stale"
            if "legs" not in per:
                continue
            legs = EM.CaseLegs.from_json(per["legs"])
            trimmed = trimmed or legs.trimmed
            query_ids[snap] = legs.query_id
            for name in SCORED:
                order, rel = _case_rankings(entry, snap, row.kb_id, config=name)
                if order is None:
                    continue
                scored = EM.score(order, rel, entry["evidence"])
                ranks[f"{snap}:{name}"] = scored.first
                found[f"{snap}:{name}"] = scored.found
        first = (entry["by_snapshot"].get(snaps[0]) or {}) if snaps else {}
        out.append({"case_id": entry["id"], "kind": entry["kind"], "question": entry["question"], "stale": stale,
                    "trimmed": trimmed, "ranks": ranks, "found": found, "query_ids": query_ids,
                    "top": first.get("top") or [], "answer": entry.get("answer"),
                    "quote_check": entry.get("quote_check"), "verdict": entry.get("verdict")})
    return out


async def get_run(kb_id: str, run_id: str) -> EvalRun:
    from core.knowledge import evals
    from core.knowledge.api_models import EvalRun

    row = await _row(kb_id, run_id)
    return EvalRun.model_validate({**evals.run_summary(row).model_dump(), "cases": case_results(row)})


async def cancel(kb_id: str, run_id: str) -> EvalRunSummary:
    """Stop the run before its next question; what it scored is kept."""
    from core.knowledge import evals
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    row = await _row(kb_id, run_id)
    if row.status in ("queued", "running"):
        if run_id in _tasks:
            await _update(run_id, status="cancelling")
        else:
            await _update(run_id, status="cancelled", finished_at=_now())
    async with session() as s:
        return evals.run_summary(await s.get(R, run_id))


async def compare(kb_id: str, run_id: str, body: EvalCompareRequest) -> list[EvalConfigResult]:
    """The run's searches fused again with other weights: one row per snapshot, no search, $0."""
    from core.knowledge.api_models import EvalConfigResult

    row = await _row(kb_id, run_id)
    if row.status not in ("completed", "cancelled") or not row.results:
        raise ValueError("Only a finished run can be compared again.")
    config = dict(row.config or {})
    snaps = [s["snapshot_id"] for s in config["snapshots"]]
    cases = compared_cases(row.results, snaps)
    return [EvalConfigResult.model_validate(config_result(
        kb_id, snap, "custom", cases, rrf_k=body.rrf_k,
        weights={"lexical": body.weights.get("lexical", 0.0), "vector": body.weights.get("vector", 0.0)}))
        for snap in snaps]


async def mark_interrupted() -> int:
    """At startup: every run this process's predecessor left under way was interrupted."""
    from sqlalchemy import update

    from core.knowledge.api_models import EVAL_ACTIVE_STATUSES
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        result = await s.execute(update(R).where(R.status.in_(EVAL_ACTIVE_STATUSES), R.id.notin_(list(_tasks)))
                                 .values(status="interrupted", error="The server stopped during the run.",
                                         finished_at=_now(), updated_at=_now()))
    return int(result.rowcount or 0)
