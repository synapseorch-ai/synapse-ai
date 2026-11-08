"""A knowledge base's eval re-runs itself when its settings change (V1, D168 #3).

**When.** After the index switches to a snapshot built another way
(`knowledge.base.reindexed`, heard through `events.knowledge_observers`), and
after a search setting is saved (rerank, fusion, retrieval) without a rebuild.
Only sets that already have a completed run take part — a person ran them once
— and only their judge-free part: a judge costs a person's say-so every time.

**How.** One run per set on the serving snapshot, started by "settings":

* the switch is waited out first: the documents it queued again are indexed,
  then `DEBOUNCE_S` more, so a run does not search half an index;
* a change to fusion weights or `rrf_k` alone, on a set whose last run reranked
  nothing, is scored again from that run's stored searches: a run with no
  search, $0;
* a run a person started is never cancelled: the new one waits for it to end;
  an earlier automatic run is replaced;
* a base whose budget the searches would cross records a `skipped` run, and
  nothing is spent.

The pending re-runs live in this process (`_pending`); a restart forgets them,
and the screen still shows which settings each run was scored under.

**With a runner** (cloud, V1c: runs are worker jobs) the waiting is the
runner's: the observer hands it the switch (`EvalRunner.after_switch`, awaited
by the job that switched — nothing is left running in a worker that may stop),
and a re-run that cannot start yet is handed to `EvalRunner.rerun_later`.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable

    from core.knowledge.events import KnowledgeLifecycleEvent
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

#: After a switch: how often to look whether the base has settled, for how long at most, and how long after.
SETTLE_POLL_S = 2.0
SETTLE_MAX_S = 600.0
DEBOUNCE_S = 30.0
_BUSY_DOCS = ("queued", "parsing", "indexing")

_waiting: dict[tuple[str, str], asyncio.Task] = {}  # (tenant, kb) → the wait before a switch's re-runs
_pending: dict[tuple[str, str], str] = {}  # (tenant, set) → kb: re-run once the set's current run ends


def _tenant() -> str:
    from core.tenancy import get_tenant

    return get_tenant()


# ── registration ──────────────────────────────────────────────────────────────

def install() -> None:
    """Hear index switches. Idempotent: the server's startup calls it, and so do tests."""
    from core.knowledge.events import knowledge_observers

    if _observer not in knowledge_observers:
        knowledge_observers.append(_observer)


async def startup() -> None:
    """At the server's start: hear switches, and mark what the last process left running as interrupted."""
    from core.knowledge.evals_runs import mark_interrupted

    install()
    left = await mark_interrupted()
    if left:
        _log.info(f"knowledge eval: {left} run(s) the last process left were interrupted")


def _observer(event: KnowledgeLifecycleEvent) -> Awaitable[None] | None:
    """Only spawns: the switch announcing this must not wait on an eval. With a runner, returns its
    `after_switch` (queuing the wait), which `events.emit` awaits."""
    if event.type != "knowledge.base.reindexed":
        return None
    from core.background import spawn_background
    from core.knowledge.evals_runs import get_eval_runner

    runner = get_eval_runner()
    if runner is not None:
        return runner.after_switch(event.kb_id)

    key = (_tenant(), event.kb_id)
    waiting = _waiting.pop(key, None)
    if waiting is not None and not waiting.done():
        waiting.cancel()  # a newer switch: wait from here instead
    _waiting[key] = spawn_background(_after_switch(event.kb_id), name=f"knowledge-eval-settle:{event.kb_id}")
    return None


async def settled(kb_id: str) -> bool:
    """No document of the base is waiting to be indexed."""
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import KnowledgeDocumentDB as D

    async with session() as s:
        busy = (await s.execute(select(func.count()).where(
            D.tenant_id == _tenant(), D.kb_id == kb_id, D.status.in_(_BUSY_DOCS)))).scalar_one()
    return not busy


async def _after_switch(kb_id: str) -> None:
    key = (_tenant(), kb_id)
    try:
        deadline = time.monotonic() + SETTLE_MAX_S
        while not await settled(kb_id) and time.monotonic() < deadline:
            await asyncio.sleep(SETTLE_POLL_S)
        await asyncio.sleep(DEBOUNCE_S)
        await rerun(kb_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a re-run that cannot start never troubles the switch
        _log.warning(f"knowledge eval re-run after a switch of {kb_id} failed: {exc}")
    finally:
        if _waiting.get(key) is asyncio.current_task():
            _waiting.pop(key, None)


def waiting() -> dict[tuple[str, str], asyncio.Task]:
    """The switches being waited out (tests await them)."""
    return _waiting


# ── what changed ──────────────────────────────────────────────────────────────

def _search_part(kb: KnowledgeBase) -> dict[str, Any]:
    from core.knowledge.evals_runs import query_settings

    return query_settings(kb)


def _fusion_only(before: KnowledgeBase, after: KnowledgeBase) -> bool:
    """Only fusion weights or `rrf_k` changed among the search settings."""
    a, b = _search_part(before), _search_part(after)
    if a["rerank"] != b["rerank"] or a["retrieval"] != b["retrieval"]:
        return False
    fa, fb = dict(a["fusion"]), dict(b["fusion"])
    return fa.get("overfetch") == fb.get("overfetch") and fa != fb


async def settings_changed(before: KnowledgeBase, after: KnowledgeBase) -> None:
    """A saved edit that started no rebuild: re-run what its search settings changed. Never raises."""
    try:
        if _search_part(before) == _search_part(after):
            return
        await rerun(after.id, fusion_only=_fusion_only(before, after))
    except Exception as exc:  # noqa: BLE001 — the edit was saved; its eval is a courtesy
        _log.warning(f"knowledge eval re-run after an edit of {after.id} failed: {exc}")


# ── re-running ────────────────────────────────────────────────────────────────

async def _sets_that_ran(kb_id: str) -> list[str]:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        rows = (await s.execute(select(R.set_id).where(
            R.tenant_id == _tenant(), R.kb_id == kb_id, R.status == "completed").distinct())).all()
    from core.knowledge import evals

    live = {record["id"] for record, _ in await evals._rows(kb_id=kb_id)}
    return sorted(set_id for (set_id,) in rows if set_id in live)


async def _active(set_id: str):
    from sqlalchemy import select

    from core.knowledge.api_models import EVAL_ACTIVE_STATUSES
    from core.knowledge.evals_runs import reap
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    await reap(set_id=set_id)  # a run its worker left is not "under way"
    async with session() as s:
        return (await s.execute(select(R).where(
            R.tenant_id == _tenant(), R.set_id == set_id, R.status.in_(EVAL_ACTIVE_STATUSES)))).scalars().first()


async def rerun(kb_id: str, *, fusion_only: bool = False) -> list[str]:
    """Re-run each of `kb_id`'s sets that ran before. Returns the runs started (or recorded)."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled

    if not knowledge_enabled():
        return []
    kb = await bases.get_base(kb_id)
    if kb is None or kb.status != "active":
        return []
    started = []
    for set_id in await _sets_that_ran(kb_id):
        made = await _rerun_set(kb, set_id, fusion_only=fusion_only)
        if made:
            started.append(made)
    return started


async def _rerun_set(kb: KnowledgeBase, set_id: str, *, fusion_only: bool) -> str | None:
    from core.knowledge import evals_runs
    from core.knowledge.api_models import EvalRunRequest
    from core.knowledge.evals import EvalBusy

    active = await _active(set_id)
    if active is not None and active.started_by == "settings" and active.status in ("queued", "running"):
        if await _measures_now(kb, active):
            return None  # the automatic run under way already searches what the base serves now
        await evals_runs.cancel(kb.id, active.id)  # replaced: the newer settings are what count
        active = await _active(set_id)  # a run with no task here ends at once; a live one at its next question
    if active is not None:
        await _later(kb.id, set_id)
        return None
    if fusion_only:
        made = await rescore(kb, set_id)
        if made:
            return made
    try:
        run = await evals_runs.start(kb, EvalRunRequest(set_id=set_id), started_by="settings")
    except EvalBusy:
        await _later(kb.id, set_id)
        return None
    return run.id


async def rerun_set(kb: KnowledgeBase, set_id: str) -> str | None:
    """Re-run one set that waited (cloud's job, once a run under way ended). Returns the run started, if any."""
    return await _rerun_set(kb, set_id, fusion_only=False)


async def _later(kb_id: str, set_id: str) -> None:
    from core.knowledge.evals_runs import get_eval_runner

    runner = get_eval_runner()
    if runner is None:
        _pending[(_tenant(), set_id)] = kb_id
    else:
        await runner.rerun_later(kb_id, set_id)


async def _measures_now(kb: KnowledgeBase, active: Any) -> bool:
    """`active` (an automatic run) searches the snapshot the base serves, under the search settings it has now."""
    from core.knowledge import evals_runs

    config = dict(active.config or {})
    try:
        serving = [s["snapshot_id"] for s in await evals_runs.snapshots_for(kb, [])]
    except Exception:  # noqa: BLE001 — nothing indexed: nothing to compare
        return False
    return (config.get("query_settings") == evals_runs.query_settings(kb)
            and [s.get("snapshot_id") for s in config.get("snapshots") or []] == serving)


async def after_run(kb_id: str, run_id: str) -> None:
    """A run ended: start the re-run that waited for it, if any. Never raises."""
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    try:
        async with session() as s:
            row = await s.get(R, run_id)
        if row is None:
            return
        kb_waiting = _pending.pop((_tenant(), row.set_id), None)
        if kb_waiting is None:
            return
        from core.knowledge import bases

        kb = await bases.get_base(kb_waiting)
        if kb is not None and kb.status == "active":
            await _rerun_set(kb, row.set_id, fusion_only=False)
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"knowledge eval: the re-run waiting on {run_id} did not start: {exc}")


async def rescore(kb: KnowledgeBase, set_id: str) -> str | None:
    """The set's last completed run scored again under `kb`'s fusion: no search, $0. None when that run reranked
    (a reranker reads the fused order, so its answer cannot be known without searching) or is judged-only."""
    import copy
    from datetime import datetime, timezone

    from sqlalchemy import select

    from core.knowledge import evals_metrics as EM
    from core.knowledge import evals_runs
    from core.knowledge.ids import EVAL_RUN_PREFIX, new_id
    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    async with session() as s:
        last = (await s.execute(select(R).where(
            R.tenant_id == _tenant(), R.set_id == set_id, R.status == "completed",
        ).order_by(R.created_at.desc()).limit(1))).scalars().first()
    if last is None or not last.results:
        return None
    config = copy.deepcopy(dict(last.config or {}))
    snaps = [s["snapshot_id"] for s in config.get("snapshots") or []]
    results = copy.deepcopy(dict(last.results))
    k, weights = kb.fusion.rrf_k, dict(kb.fusion.weights)
    for entry in results.get("cases") or []:
        for snap in snaps:
            per = entry["by_snapshot"].get(snap) or {}
            if "legs" not in per:
                continue
            legs = EM.CaseLegs.from_json(per["legs"])
            if legs.reranked:
                return None
            legs.rrf_k, legs.weights = k, weights
            fused = EM.ranking(legs, "hybrid", kb_id=kb.id, snapshot_id=snap)
            if fused is not None:  # no rerank: served is the fused order, cut where the search cut it
                where = {cid: i for i, cid in enumerate(legs.ids)}
                legs.served = [where[cid] for cid in fused[:len(legs.served)]]
            per["legs"] = legs.to_json()
            per.pop("top", None)  # the passages shown first were the old order's
        for graded in ("answer", "quote_check", "verdict"):
            entry.pop(graded, None)
    config.update(judge=None, query_settings=evals_runs.query_settings(kb), rescored_from=last.id)
    summary = await evals_runs.score(kb, config, results)
    summary["notes"] = [*summary.get("notes", []),
                        f"Scored again from run {last.id}'s searches under the new fusion settings: no search, $0."]
    now = datetime.now(timezone.utc)
    run_id = new_id(EVAL_RUN_PREFIX)
    async with session() as s:
        s.add(R(id=run_id, tenant_id=_tenant(), kb_id=kb.id, set_id=set_id, status="completed",
                started_by="settings", config=config, summary=summary, results=results,
                case_count=last.case_count, done_count=len(results.get("cases") or []), est_usd=0.0, cost_usd=0.0,
                started_at=now, finished_at=now))
    return run_id
