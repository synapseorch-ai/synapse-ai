"""A knowledge base's question sets, the real searches they can borrow, and its runs as lists read them (V1, D168).

**A set** is one row of the engine's `collections` (`knowledge_eval_sets`),
written alone with a compare-and-set, as `bases.py` writes a knowledge base:
`collections.save` replaces a whole list, so a person saving one set could
otherwise lose another's edit. Only a person's routes write a set — a
background run never does.

**A case** is a question and where its answer is written: quote spans in
documents (`api_models.EvalEvidence`), never chunk ids, which change with every
snapshot. A person accepts or edits every case before it is saved: drafts are
suggestions (`evals_generate`), and a promoted search becomes a case only from
the passages a person ticked.

**Promoting a real search** keeps its trace: `knowledge_queries.retained_by`
names the set, which exempts the row from the 30-day cleanup. When the case or
the set goes, the row passes to another set holding the same search, or is
released to the cleanup.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from core.knowledge.errors import KnowledgeError, StaleRevision
from core.knowledge.ids import EVAL_SET_PREFIX, new_id
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import (
        EvalPromote, EvalQuery, EvalRunSummary, EvalSet, EvalSetCreate, EvalSetSummary, EvalSetUpdate,
    )

_log = get_logger(__name__)

COLLECTION = "knowledge_eval_sets"
#: The searches a promoted case may come from are read from this many of the newest.
RECENT_QUERIES_SCANNED = 200
#: How long a promoted case's own passages are quoted, when the chunk is gone from the index.
QUOTE_CHARS = 300


class EvalNotFound(KnowledgeError, LookupError):
    """No such question set, run or search here (a 404)."""

    code = "eval_not_found"


class EvalBusy(KnowledgeError):
    """A run of this set is under way (a 409)."""

    code = "eval_busy"


class EvalUnavailable(KnowledgeError):
    """A run was recorded but whatever drives runs could not take it (a 503; cloud's worker queue, V1c)."""

    code = "eval_unavailable"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _tenant() -> str:
    from core.tenancy import get_tenant

    return get_tenant()


_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), _tenant())
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


# ── sets: reads ───────────────────────────────────────────────────────────────

async def _rows(*, kb_id: str | None = None, set_id: str | None = None) -> list[tuple[dict, datetime]]:
    from sqlalchemy import select

    from core.store import session
    from core.store.models import CollectionDB

    stmt = select(CollectionDB.value, CollectionDB.updated_at).where(
        CollectionDB.tenant_id == _tenant(), CollectionDB.collection == COLLECTION)
    if set_id is not None:
        stmt = stmt.where(CollectionDB.key == set_id)
    async with session() as s:
        rows = (await s.execute(stmt.order_by(CollectionDB.created_at))).all()
    return [(dict(value), updated) for value, updated in rows
            if isinstance(value, dict) and (kb_id is None or value.get("kb_id") == kb_id)]


async def _record(kb_id: str, set_id: str) -> tuple[dict, datetime]:
    rows = await _rows(kb_id=kb_id, set_id=set_id)
    if not rows:
        raise EvalNotFound(f"No question set {set_id!r} in this knowledge base.")
    return rows[0]


def _summary(record: dict, last_run: EvalRunSummary | None = None) -> dict[str, Any]:
    cases = record.get("cases") or []
    kinds: dict[str, int] = {}
    for case in cases:
        kinds[case.get("kind", "direct")] = kinds.get(case.get("kind", "direct"), 0) + 1
    return {"id": record["id"], "kb_id": record["kb_id"], "name": record.get("name", ""),
            "revision": int(record.get("revision") or 1), "case_count": len(cases), "counts_by_kind": kinds,
            "created_at": record.get("created_at"), "updated_at": record.get("updated_at"), "last_run": last_run}


async def list_sets(kb_id: str) -> list[EvalSetSummary]:
    from core.knowledge.api_models import EvalSetSummary

    rows = await _rows(kb_id=kb_id)
    last = await latest_runs([record["id"] for record, _ in rows])
    return [EvalSetSummary.model_validate(_summary(record, last.get(record["id"]))) for record, _ in rows]


async def get_set(kb_id: str, set_id: str) -> EvalSet:
    from core.knowledge.api_models import EvalSet

    record, _ = await _record(kb_id, set_id)
    last = await latest_runs([set_id])
    return EvalSet.model_validate({**_summary(record, last.get(set_id)), "cases": record.get("cases") or []})


async def cases_of(kb_id: str, set_id: str) -> list[dict]:
    record, _ = await _record(kb_id, set_id)
    return list(record.get("cases") or [])


# ── sets: writes ──────────────────────────────────────────────────────────────

def _clean_cases(cases: list[Any], before: list[dict] | None = None) -> list[dict]:
    """Cases as stored: each with an id and a time, ids kept from before and never repeated."""
    from core.knowledge.api_models import EvalCase

    known = {c.get("id"): c for c in before or [] if c.get("id")}
    seen: set[str] = set()
    now = _iso(_now())
    out = []
    for case in cases:
        data = (case if isinstance(case, EvalCase) else EvalCase.model_validate(case)).model_dump(mode="json")
        for ev in data["evidence"]:
            if ev["char_end"] < ev["char_start"]:
                raise ValueError("a piece of evidence ends before it starts")
        if not data.get("id") or data["id"] in seen:
            data["id"] = "case_" + uuid.uuid4().hex[:12]
        seen.add(data["id"])
        data["created_at"] = (known.get(data["id"]) or {}).get("created_at") or data.get("created_at") or now
        out.append(data)
    return out


async def create_set(kb_id: str, body: EvalSetCreate) -> EvalSet:
    from core.knowledge.bases import require_base
    from core.store import session
    from core.store.models import CollectionDB

    await require_base(kb_id)
    now = _now()
    record = {"id": new_id(EVAL_SET_PREFIX), "kb_id": kb_id, "name": body.name, "revision": 1,
              "cases": _clean_cases(list(body.cases)), "created_at": _iso(now), "updated_at": _iso(now)}
    async with session() as s:
        s.add(CollectionDB(tenant_id=_tenant(), collection=COLLECTION, key=record["id"], value=record,
                           created_at=now, updated_at=now))
    await _retain(_query_ids(record), record["id"])
    return await get_set(kb_id, record["id"])


async def _write(record: dict, seen: datetime) -> None:
    """Store `record` over the row read at `seen`; `StaleRevision` when someone wrote it since."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import CollectionDB

    now = _now()
    stamp = max(now, _aware(seen) + timedelta(microseconds=1))
    record["updated_at"] = _iso(stamp)
    async with session() as s:
        result = await s.execute(update(CollectionDB).where(
            CollectionDB.tenant_id == _tenant(), CollectionDB.collection == COLLECTION,
            CollectionDB.key == record["id"], CollectionDB.updated_at == seen,
        ).values(value=record, updated_at=stamp))
        if result.rowcount != 1:
            raise StaleRevision("This question set was changed by someone else just now. Reload it.")


async def update_set(kb_id: str, set_id: str, body: EvalSetUpdate) -> EvalSet:
    """Save a person's edit. A stale `revision` is refused before anything is written."""
    async with _lock():
        record, seen = await _record(kb_id, set_id)
        if body.revision != int(record.get("revision") or 1):
            raise StaleRevision(f"This question set was changed (revision {record.get('revision')}) after the edit "
                                f"started (revision {body.revision}). Reload it and apply the change again.")
        before = _query_ids(record)
        if body.name is not None:
            record["name"] = body.name
        if body.cases is not None:
            record["cases"] = _clean_cases(list(body.cases), record.get("cases"))
        record["revision"] = int(record.get("revision") or 1) + 1
        await _write(record, seen)
    after = _query_ids(record)
    await _retain(after - before, set_id)
    await _release(before - after, set_id)
    return await get_set(kb_id, set_id)


async def delete_set(kb_id: str, set_id: str) -> None:
    """Forget the set and its runs. Refused while one of its runs is under way."""
    from sqlalchemy import delete, select

    from core.knowledge.api_models import EVAL_ACTIVE_STATUSES
    from core.store import session
    from core.store.models import CollectionDB, KnowledgeEvalRunDB

    from core.knowledge.evals_runs import reap

    record, _ = await _record(kb_id, set_id)
    await reap(set_id=set_id)  # a run its worker left does not hold the set
    async with session() as s:
        busy = (await s.execute(select(KnowledgeEvalRunDB.id).where(
            KnowledgeEvalRunDB.tenant_id == _tenant(), KnowledgeEvalRunDB.set_id == set_id,
            KnowledgeEvalRunDB.status.in_(EVAL_ACTIVE_STATUSES)))).first()
    if busy is not None:
        raise EvalBusy("A run of this question set is under way. Cancel it first, then delete the set.")
    async with session() as s:
        await s.execute(delete(KnowledgeEvalRunDB).where(
            KnowledgeEvalRunDB.tenant_id == _tenant(), KnowledgeEvalRunDB.set_id == set_id))
        await s.execute(delete(CollectionDB).where(
            CollectionDB.tenant_id == _tenant(), CollectionDB.collection == COLLECTION, CollectionDB.key == set_id))
    await _release(_query_ids(record), set_id)


async def forget_base(kb_id: str) -> None:
    """A deleted knowledge base takes its sets and runs with it, and releases the searches they kept."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import CollectionDB, KnowledgeEvalRunDB

    rows = await _rows(kb_id=kb_id)
    async with session() as s:
        await s.execute(delete(KnowledgeEvalRunDB).where(
            KnowledgeEvalRunDB.tenant_id == _tenant(), KnowledgeEvalRunDB.kb_id == kb_id))
        if rows:
            await s.execute(delete(CollectionDB).where(
                CollectionDB.tenant_id == _tenant(), CollectionDB.collection == COLLECTION,
                CollectionDB.key.in_([record["id"] for record, _ in rows])))
    for record, _ in rows:
        await _release(_query_ids(record), record["id"])


# ── the searches a set keeps ──────────────────────────────────────────────────

def _query_ids(record: dict) -> set[str]:
    return {c["query_id"] for c in record.get("cases") or [] if c.get("query_id")}


async def _retain(query_ids: set[str], set_id: str) -> None:
    """Keep these searches past the cleanup for `set_id`, unless another set already keeps them."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import KnowledgeQueryDB

    if not query_ids:
        return
    async with session() as s:
        await s.execute(update(KnowledgeQueryDB).where(
            KnowledgeQueryDB.tenant_id == _tenant(), KnowledgeQueryDB.id.in_(sorted(query_ids)),
            KnowledgeQueryDB.retained_by.is_(None)).values(retained_by=set_id))


async def _release(query_ids: set[str], set_id: str) -> None:
    """`set_id` no longer keeps these searches: each passes to another set holding it, or back to the cleanup."""
    from sqlalchemy import update

    from core.store import session
    from core.store.models import KnowledgeQueryDB

    if not query_ids:
        return
    holders: dict[str, str] = {}
    for record, _ in await _rows():
        if record.get("id") == set_id:
            continue
        for q in _query_ids(record) & query_ids:
            holders.setdefault(q, record["id"])
    async with session() as s:
        for q in sorted(query_ids):
            await s.execute(update(KnowledgeQueryDB).where(
                KnowledgeQueryDB.tenant_id == _tenant(), KnowledgeQueryDB.id == q,
                KnowledgeQueryDB.retained_by == set_id).values(retained_by=holders.get(q)))


async def recent_queries(kb_id: str, *, limit: int = 50) -> list[EvalQuery]:
    """This knowledge base's newest real searches — any surface but the eval's own — with what they returned."""
    from sqlalchemy import select

    from core.knowledge.api_models import EvalQuery, EvalQueryHit
    from core.store import session
    from core.store.models import KnowledgeQueryDB

    async with session() as s:
        rows = (await s.execute(select(KnowledgeQueryDB).where(
            KnowledgeQueryDB.tenant_id == _tenant(), KnowledgeQueryDB.surface != "eval",
        ).order_by(KnowledgeQueryDB.created_at.desc()).limit(RECENT_QUERIES_SCANNED))).scalars().all()
    out = []
    for row in rows:
        if kb_id not in (row.kb_ids or []):
            continue
        trace = row.trace or {}
        hits = [EvalQueryHit(label=h.get("label", ""), chunk_id=h["chunk_id"], doc_id=h.get("doc_id", ""),
                             path=h.get("path", ""), snippet=h.get("snippet", ""), score=h.get("score"))
                for h in trace.get("hits") or [] if h.get("kb_id", kb_id) == kb_id and h.get("chunk_id")]
        out.append(EvalQuery(query_id=row.id, query=(trace.get("request") or {}).get("query", ""),
                             surface=row.surface, created_at=_iso(_aware(row.created_at)) or "", hits=hits,
                             retained_by=row.retained_by))
        if len(out) >= limit:
            break
    return out


async def promote(kb_id: str, set_id: str, body: EvalPromote) -> EvalSet:
    """Make a real search a case, its evidence the passages a person ticked among what it returned."""
    from core.knowledge.api_models import EvalCase, EvalEvidence
    from core.knowledge.chunker import ATTACHMENT_KEY
    from core.knowledge.search import load_chunks
    from core.store import session
    from core.store.models import KnowledgeQueryDB

    record, _ = await _record(kb_id, set_id)
    async with session() as s:
        row = await s.get(KnowledgeQueryDB, body.query_id)
    if row is None or row.tenant_id != _tenant() or kb_id not in (row.kb_ids or []):
        raise EvalNotFound(f"No search {body.query_id!r} of this knowledge base (searches are kept 30 days).")
    trace = row.trace or {}
    returned = {h["chunk_id"]: h for h in trace.get("hits") or [] if h.get("chunk_id")}
    strangers = [c for c in body.chunk_ids if c not in returned]
    if strangers:
        raise ValueError("Only passages this search returned can be marked as its answer.")
    views = await load_chunks(body.chunk_ids)
    evidence = []
    for chunk_id in dict.fromkeys(body.chunk_ids):
        view, hit = views.get(chunk_id), returned[chunk_id]
        if view is not None:
            r = view.row
            evidence.append(EvalEvidence(
                doc_id=r.doc_id, path=r.path or hit.get("path", ""), attachment=(r.meta or {}).get(ATTACHMENT_KEY),
                doc_sha256=r.doc_sha256, char_start=r.char_start, char_end=r.char_end,
                quote=_lead(r.content) or hit.get("snippet") or r.path or "passage"))
        else:  # gone from the index since: its snippet is the quote, and the span is unknown
            quote = (hit.get("snippet") or "").rstrip("…").strip()
            if not quote:
                raise ValueError("A marked passage is no longer in the index and its search kept no text of it.")
            evidence.append(EvalEvidence(doc_id=hit.get("doc_id", ""), path=hit.get("path", ""),
                                         doc_sha256=hit.get("doc_sha256"), char_start=0, char_end=0, quote=quote))
    question = body.question or (trace.get("request") or {}).get("query") or ""
    case = EvalCase(kind="promoted", question=question[:2000], evidence=evidence, query_id=body.query_id)
    from core.knowledge.api_models import EvalSetUpdate

    return await update_set(kb_id, set_id, EvalSetUpdate(
        revision=int(record.get("revision") or 1), cases=[*(record.get("cases") or []), case]))


def _lead(text: str) -> str:
    """The first words of a passage, to `QUOTE_CHARS`, cut at a word."""
    flat = " ".join((text or "").split())
    if len(flat) <= QUOTE_CHARS:
        return flat
    cut = flat[:QUOTE_CHARS]
    return cut[:cut.rfind(" ")] if " " in cut else cut


# ── runs, as lists read them ──────────────────────────────────────────────────

def run_summary(row: Any) -> EvalRunSummary:
    from core.knowledge.api_models import EvalRunSummary

    summary = dict(row.summary or {})
    return EvalRunSummary.model_validate({
        **summary, "id": row.id, "kb_id": row.kb_id, "set_id": row.set_id, "status": row.status,
        "started_by": row.started_by, "case_count": row.case_count, "done_count": row.done_count,
        "est_usd": row.est_usd, "cost_usd": row.cost_usd, "error": row.error,
        "created_at": _iso(_aware(row.created_at)) if row.created_at else None,
        "started_at": _iso(_aware(row.started_at)) if row.started_at else None,
        "finished_at": _iso(_aware(row.finished_at)) if row.finished_at else None})


async def latest_runs(set_ids: list[str]) -> dict[str, EvalRunSummary]:
    """Each set's newest run that is not a skipped one."""
    from sqlalchemy import select
    from sqlalchemy.orm import defer

    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    if not set_ids:
        return {}
    async with session() as s:
        rows = (await s.execute(select(R).options(defer(R.results)).where(
            R.tenant_id == _tenant(), R.set_id.in_(set_ids), R.status != "skipped",
        ).order_by(R.created_at.desc()))).scalars().all()
    out: dict[str, EvalRunSummary] = {}
    for row in rows:
        out.setdefault(row.set_id, run_summary(row))
    return out


async def list_runs(kb_id: str, *, set_id: str | None = None, limit: int = 50) -> list[EvalRunSummary]:
    from sqlalchemy import select
    from sqlalchemy.orm import defer

    from core.store import session
    from core.store.models import KnowledgeEvalRunDB as R

    stmt = select(R).options(defer(R.results)).where(R.tenant_id == _tenant(), R.kb_id == kb_id)
    if set_id:
        stmt = stmt.where(R.set_id == set_id)
    async with session() as s:
        rows = (await s.execute(stmt.order_by(R.created_at.desc()).limit(limit))).scalars().all()
    return [run_summary(row) for row in rows]
