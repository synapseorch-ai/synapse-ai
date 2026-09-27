"""
The usage event log and the model rate card.

Two tables with opposite tenancy, in one module because one is read to price
the other.

`usage_logs` is tenant data and append-only: one row per LLM call, written on
the per-turn path. As a JSON file every call loaded the whole list, appended
and wrote it all back, so logging a call cost more the longer the install had
been running — and two concurrent turns could lose one of the two records.

`model_pricing` is **not** tenant data. What a model costs is the same number
for every tenant, so a tenant column would give each org a private copy of one
rate card and mean an operator updating a price reached nobody. See
`ModelPricingDB`.

Why the summaries still aggregate in Python
-------------------------------------------
`get_usage_logs` is a real query here — filtered, ordered and limited by the
database. The two summary functions in `core/usage_tracker.py` are not; they
stream rows from this module and group them in Python, as they always have.
That is deliberate: they collect `models_used` and `agents_used` as *sets*, and
set aggregation is the one part of this that SQLite and Postgres do not spell
the same way (`group_concat` versus `string_agg`). `core/store/models.py` says
not to reach for dialect-specific SQL, and these run on a settings screen
rather than the hot path, so the portable answer is the right one.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select

from core.tenancy import get_tenant

#: Namespace for the pricing table in the read-through cache. Priced on every
#: LLM call, changed almost never.
_PRICING_NS = "model_pricing"


def _ts(dt: datetime | None) -> str:
    """The millisecond ISO stamp the API has always emitted."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


#: The keys an LLM-call record carries, in their original order.
_USAGE_KEYS = (
    "model", "provider", "session_id", "agent_id", "source", "run_id", "tool_name",
    "input_tokens", "output_tokens", "total_tokens", "context_chars", "estimated_cost",
    "latency_seconds", "cache_read_tokens", "cache_write_tokens", "estimated_savings",
    "response_cache_hit",
)

#: A compaction record's own keys. It has never carried provider, tool_name,
#: context_chars or the cache counters, and the frontend distinguishes the two
#: kinds of row by which keys are present — so the two shapes stay distinct.
_COMPACTION_KEYS = (
    "event_type", "source", "session_id", "agent_id", "run_id", "model",
    "input_tokens", "output_tokens", "total_tokens", "estimated_cost", "latency_seconds",
)


def _to_dict(row) -> dict:
    if row.event_type == "compaction":
        record = {"timestamp": _ts(row.timestamp)}
        record.update({k: getattr(row, k) for k in _COMPACTION_KEYS})
        record.update(row.details or {})
        return record

    record = {"timestamp": _ts(row.timestamp)}
    record.update({k: getattr(row, k) for k in _USAGE_KEYS})
    record["response_cache_hit"] = bool(record["response_cache_hit"])
    return record


#: Columns a usage row may set, so an imported record cannot inject one.
_COLUMNS = frozenset({
    "timestamp", "event_type", "source", "model", "provider", "session_id", "agent_id",
    "run_id", "tool_name", "input_tokens", "output_tokens", "total_tokens", "context_chars",
    "estimated_cost", "latency_seconds", "cache_read_tokens", "cache_write_tokens",
    "estimated_savings", "response_cache_hit", "details",
})


def record_values(record: dict) -> dict:
    """A `usage_logs.json` record as a row.

    Used by the importer. Anything that is not a column — the compaction
    event's own fields — is collected into `details`, which is where the live
    writer puts them too.
    """
    values = {k: v for k, v in record.items() if k in _COLUMNS and k != "details"}
    extra = {k: v for k, v in record.items() if k not in _COLUMNS}
    if extra:
        values["details"] = extra

    stamp = record.get("timestamp")
    if isinstance(stamp, str) and stamp:
        try:
            values["timestamp"] = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            values.pop("timestamp", None)
    return values


# ---------------------------------------------------------------------------
# Usage log
# ---------------------------------------------------------------------------

async def append(values: dict) -> None:
    """Insert one usage row. One INSERT, no read."""
    from core.store import session
    from core.store.models import UsageLogDB

    async with session() as s:
        s.add(UsageLogDB(tenant_id=get_tenant(), **values))


async def query(
    limit: int = 100,
    offset: int = 0,
    session_id: str | None = None,
    source: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict]:
    """Usage records, filtered and paged by the database.

    Per-session and per-run reads come back oldest-first, because the usage
    screen shows the context delta from one turn to the next. Everything else
    is newest-first. `id` breaks ties on `timestamp`, which is only accurate to
    the millisecond and so is not unique under load.
    """
    from core.store import session
    from core.store.models import UsageLogDB

    stmt = select(UsageLogDB).where(UsageLogDB.tenant_id == get_tenant())
    if session_id:
        stmt = stmt.where(UsageLogDB.session_id == session_id)
    if run_id:
        # A run's calls include the runs nested under it (F251): its Usage row
        # used to miss them, and its cost at the end summed its own alone.
        # `autoescape`: run ids are full of `_`, LIKE's any-one-character.
        stmt = stmt.where(or_(UsageLogDB.run_id == run_id,
                              UsageLogDB.run_id.startswith(f"{run_id}__", autoescape=True)))
    if source:
        stmt = stmt.where(UsageLogDB.source == source)
    if agent_id:
        stmt = stmt.where(UsageLogDB.agent_id == agent_id)

    if session_id or run_id:
        stmt = stmt.order_by(UsageLogDB.timestamp, UsageLogDB.id)
    else:
        stmt = stmt.order_by(UsageLogDB.timestamp.desc(), UsageLogDB.id.desc())

    async with session() as s:
        rows = (await s.execute(stmt.offset(offset).limit(limit))).scalars().all()
    return [_to_dict(r) for r in rows]


async def all_records() -> list[dict]:
    """Every record for this tenant, oldest first — what the summaries group."""
    from core.store import session
    from core.store.models import UsageLogDB

    async with session() as s:
        rows = (
            await s.execute(
                select(UsageLogDB)
                .where(UsageLogDB.tenant_id == get_tenant())
                .order_by(UsageLogDB.timestamp, UsageLogDB.id)
            )
        ).scalars().all()
    return [_to_dict(r) for r in rows]


async def agent_summary(agent_id: str) -> dict:
    """Totals for one agent, aggregated by the database.

    The exception to this module's "summaries group in Python" note, and for
    the reason that note gives: this one collects no sets. It is `sum` and
    `count` over a `group by model`, which SQLite and Postgres spell
    identically, so there is no reason to stream every row an agent has ever
    written into the process to add them up.
    """
    from core.store import session
    from core.store.models import UsageLogDB

    scope = [
        UsageLogDB.tenant_id == get_tenant(),
        UsageLogDB.agent_id == agent_id,
        # Compaction events share this table and are not model calls — the
        # usage screen's own totals exclude them the same way
        # (`usage_tracker.get_usage_summary`). Spelled as an explicit NULL test
        # because `event_type != 'compaction'` is NULL, not true, for the LLM
        # rows that leave it unset — which is every row that counts.
        or_(UsageLogDB.event_type.is_(None), UsageLogDB.event_type != "compaction"),
    ]

    totals_stmt = select(
        func.count(UsageLogDB.id),
        func.coalesce(func.sum(UsageLogDB.input_tokens), 0),
        func.coalesce(func.sum(UsageLogDB.output_tokens), 0),
        func.coalesce(func.sum(UsageLogDB.total_tokens), 0),
        func.coalesce(func.sum(UsageLogDB.cache_read_tokens), 0),
        func.coalesce(func.sum(UsageLogDB.cache_write_tokens), 0),
        func.coalesce(func.sum(UsageLogDB.estimated_cost), 0.0),
        func.coalesce(func.sum(UsageLogDB.estimated_savings), 0.0),
        func.max(UsageLogDB.timestamp),
        func.min(UsageLogDB.timestamp),
    ).where(*scope)

    by_model_stmt = (
        select(
            UsageLogDB.model,
            func.count(UsageLogDB.id),
            func.coalesce(func.sum(UsageLogDB.total_tokens), 0),
            func.coalesce(func.sum(UsageLogDB.estimated_cost), 0.0),
        )
        .where(*scope)
        .group_by(UsageLogDB.model)
        .order_by(func.coalesce(func.sum(UsageLogDB.estimated_cost), 0.0).desc())
    )

    async with session() as s:
        row = (await s.execute(totals_stmt)).one()
        models = (await s.execute(by_model_stmt)).all()

    def _iso(value):
        return value.isoformat() if value is not None else None

    return {
        "agent_id": agent_id,
        "calls": int(row[0] or 0),
        "input_tokens": int(row[1] or 0),
        "output_tokens": int(row[2] or 0),
        "total_tokens": int(row[3] or 0),
        "cache_read_tokens": int(row[4] or 0),
        "cache_write_tokens": int(row[5] or 0),
        "estimated_cost": float(row[6] or 0.0),
        "estimated_savings": float(row[7] or 0.0),
        "last_used": _iso(row[8]),
        "first_used": _iso(row[9]),
        "by_model": [
            {
                "model": m or "unknown",
                "calls": int(calls or 0),
                "total_tokens": int(tokens or 0),
                "estimated_cost": float(cost or 0.0),
            }
            for m, calls, tokens, cost in models
        ],
    }


async def clear() -> int:
    """Delete this tenant's usage log. Returns the number of rows removed."""
    from core.store import session
    from core.store.models import UsageLogDB

    async with session() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(UsageLogDB)
                .where(UsageLogDB.tenant_id == get_tenant())
            )
        ).scalar() or 0
        await s.execute(delete(UsageLogDB).where(UsageLogDB.tenant_id == get_tenant()))
    return int(count)


# ---------------------------------------------------------------------------
# Pricing — not tenant-scoped, on purpose
# ---------------------------------------------------------------------------

async def load_pricing() -> dict:
    """The rate card as `{model: entry}`, cached."""
    from core.store import cache

    return await cache.get_or_load(_PRICING_NS, "*", _load_pricing_uncached)


async def _load_pricing_uncached() -> dict:
    from core.store import session
    from core.store.models import ModelPricingDB

    async with session() as s:
        rows = (await s.execute(select(ModelPricingDB))).scalars().all()
    return {r.model: r.entry for r in rows}


async def save_pricing(table: dict) -> None:
    """Replace the rate card with `table`."""
    from core.store import cache, session, upsert
    from core.store.models import ModelPricingDB

    async with session() as s:
        for model, entry in table.items():
            await upsert(
                s, ModelPricingDB,
                values={"model": model, "entry": entry},
                index_elements=["model"],
                update=["entry"],
            )
        if table:
            await s.execute(delete(ModelPricingDB).where(ModelPricingDB.model.notin_(list(table))))

    # Not tenant-scoped, so neither is the invalidation.
    cache.invalidate(_PRICING_NS, tenant=None)


async def seed_pricing(defaults: dict) -> int:
    """Insert the models that are missing. Returns how many were added.

    Never updates an existing row: an operator's correction, or a price feed's
    write, must not be undone by the next restart.
    """
    from core.store import cache, session
    from core.store.models import ModelPricingDB

    async with session() as s:
        known = set((await s.execute(select(ModelPricingDB.model))).scalars().all())
        missing = {m: e for m, e in defaults.items() if m not in known}
        for model, entry in missing.items():
            s.add(ModelPricingDB(model=model, entry=entry))

    if missing:
        cache.invalidate(_PRICING_NS, tenant=None)
    return len(missing)
