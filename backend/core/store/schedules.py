"""
Tenant-scoped CRUD for schedules, plus the one query the scheduler runs.

Schedules were a JSON file read through two separate `JsonStore` singletons —
one in `core/scheduler.py`, one in `core/routes/schedules.py` — each with its
own lock, each rewriting the whole file. Firing a schedule meant two full
rewrites, and the second one (recording `last_run_at` after the run finished)
clobbered anything the API had changed while the run was in flight.

Here a fire is two single-row updates and the tick is one indexed query.

Deliberately not cached
-----------------------
`core/store/cache.py` exists for the per-turn path. Schedules are not on it,
and a stale `next_run_at` is not a slow read — it is a schedule that fires
twice. The default TTL is also exactly the tick interval, which is the worst
possible value here. So every read below goes to the database.

Times
-----
`next_run_at` and `last_run_at` are real timestamp columns, so "overdue" is a
predicate the database evaluates rather than a Python loop over every row. The
API speaks the same `%Y-%m-%dT%H:%M:%SZ` strings it always has; conversion
happens here, at the boundary. Everything stored is UTC — SQLite keeps no
offset, so a non-UTC write would come back as a wrong wall time rather than as
an error.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import delete, select

from core.store.resources import CrossTenantWrite
from core.tenancy import get_tenant

#: Fields promoted out of `definition` into columns. Kept in one place because
#: the row builder strips them and `_to_dict` puts them back.
_PROMOTED = ("id", "enabled", "created_at", "last_run_at", "next_run_at")

#: Everything a save may overwrite. `created_at` is absent on purpose: it lands
#: on insert and never moves, because the list is ordered by it and editing a
#: schedule must not send it to the end.
_MUTABLE = ("name", "enabled", "definition", "next_run_at", "last_run_at")


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse(value) -> datetime | None:
    """An API timestamp string as an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_dict(row) -> dict:
    """The row as the shape the API and the scheduler have always seen."""
    return {
        **(row.definition or {}),
        "id": row.id,
        "enabled": bool(row.enabled),
        "created_at": _iso(row.created_at) or "",
        "last_run_at": _iso(row.last_run_at),
        "next_run_at": _iso(row.next_run_at),
    }


def schedule_values(item: dict, tenant_id: str) -> dict:
    """The row for a schedule dict.

    Shared with `core/store/importer.py`, so an imported install and a natively
    created one produce identical rows.
    """
    values = {
        "id": item["id"],
        "tenant_id": tenant_id,
        "name": item.get("name") or item["id"],
        "enabled": bool(item.get("enabled", True)),
        "definition": {k: v for k, v in item.items() if k not in _PROMOTED},
        "next_run_at": _parse(item.get("next_run_at")),
        "last_run_at": _parse(item.get("last_run_at")),
    }
    created = _parse(item.get("created_at"))
    if created:
        # Only ever an insert value — see `_MUTABLE`. An import that dropped it
        # would restamp every schedule with the migration time and lose the
        # list order the JSON file preserved by position.
        values["created_at"] = created
    return values


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def load() -> list[dict]:
    """Every schedule this tenant owns, oldest first."""
    from core.store import session
    from core.store.models import ScheduleDB

    async with session() as s:
        rows = (
            await s.execute(
                select(ScheduleDB)
                .where(ScheduleDB.tenant_id == get_tenant())
                .order_by(ScheduleDB.created_at, ScheduleDB.id)
            )
        ).scalars().all()
    return [_to_dict(r) for r in rows]


async def get(schedule_id: str) -> dict | None:
    from core.store import session
    from core.store.models import ScheduleDB

    if not schedule_id:
        return None
    async with session() as s:
        row = (
            await s.execute(
                select(ScheduleDB).where(
                    ScheduleDB.id == schedule_id,
                    ScheduleDB.tenant_id == get_tenant(),
                )
            )
        ).scalar_one_or_none()
    return _to_dict(row) if row is not None else None


async def due(now: datetime) -> list[dict]:
    """Enabled schedules whose next run has arrived.

    The whole reason this is a table. `next_run_at IS NULL` means "never
    scheduled", which is a startup concern rather than a tick one — see
    `ScheduleManager._on_startup`.
    """
    from core.store import session
    from core.store.models import ScheduleDB

    async with session() as s:
        rows = (
            await s.execute(
                select(ScheduleDB)
                .where(
                    ScheduleDB.tenant_id == get_tenant(),
                    ScheduleDB.enabled.is_(True),
                    ScheduleDB.next_run_at.isnot(None),
                    ScheduleDB.next_run_at <= now,
                )
                .order_by(ScheduleDB.next_run_at)
            )
        ).scalars().all()
    return [_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def _save_in(s, values: dict) -> None:
    from core.store import upsert
    from core.store.models import ScheduleDB

    # `id` alone is the conflict target, so an upsert of an id belonging to
    # another tenant would rewrite their row. Same check, and same reason, as
    # core/store/resources.py.
    owner = (
        await s.execute(select(ScheduleDB.tenant_id).where(ScheduleDB.id == values["id"]))
    ).scalar_one_or_none()
    if owner is not None and owner != values["tenant_id"]:
        raise CrossTenantWrite(f"schedule {values['id']!r} belongs to another tenant")

    await upsert(s, ScheduleDB, values=values, index_elements=["id"], update=list(_MUTABLE))


#: Told of every schedule `save` writes, after it commits. None here, because
#: `core/scheduler.py` polls `due()` and finds a new row by itself. A deployment
#: that arms each occurrence instead of polling (cloud: a deferred job per
#: `next_run_at`, no cross-tenant scan) installs one, or a schedule the engine
#: writes itself — the builder's `create_schedule`, the operator's
#: `set_schedule_enabled` — is listed, enabled and never fired (F149).
_saved_observer: Callable[[dict], Awaitable[None]] | None = None


def set_saved_observer(observer: Callable[[dict], Awaitable[None]] | None) -> None:
    """Install the callable told of each saved schedule. ``None`` removes it."""
    global _saved_observer
    _saved_observer = observer


async def save(item: dict) -> None:
    """Create or update one schedule."""
    from core.store import session

    if not item.get("id"):
        return
    async with session() as s:
        await _save_in(s, schedule_values(item, get_tenant()))
    # After the commit, not inside it: what the observer starts may read the
    # row from another process. Its failure propagates — a save that could not
    # be armed is not a schedule that works.
    if _saved_observer is not None:
        await _saved_observer(item)


async def set_next_run(schedule_id: str, when: datetime | None) -> None:
    """Advance one schedule's next run. Called before the run starts, so a
    second tick arriving mid-run does not fire it again."""
    await _update_column(schedule_id, "next_run_at", when)


async def set_last_run(schedule_id: str, when: datetime | None) -> None:
    await _update_column(schedule_id, "last_run_at", when)


async def _update_column(schedule_id: str, column: str, when: datetime | None) -> None:
    from sqlalchemy import update

    from core.store import session
    from core.store.models import ScheduleDB

    if not schedule_id:
        return
    async with session() as s:
        await s.execute(
            update(ScheduleDB)
            .where(
                ScheduleDB.id == schedule_id,
                ScheduleDB.tenant_id == get_tenant(),
            )
            .values(**{column: when})
        )


async def delete_one(schedule_id: str) -> bool:
    from core.store import session
    from core.store.models import ScheduleDB

    if not schedule_id:
        return False
    async with session() as s:
        result = await s.execute(
            delete(ScheduleDB).where(
                ScheduleDB.id == schedule_id,
                ScheduleDB.tenant_id == get_tenant(),
            )
        )
    return bool(result.rowcount)


async def replace(items: list[dict]) -> None:
    """Make this tenant's schedules exactly `items`.

    Seeding and import only — never a CRUD route, where an empty request body
    would wipe the tenant.
    """
    from core.store import session
    from core.store.models import ScheduleDB

    tenant = get_tenant()
    keys = []
    async with session() as s:
        for item in items:
            if not item.get("id"):
                continue
            await _save_in(s, schedule_values(item, tenant))
            keys.append(item["id"])

        stmt = delete(ScheduleDB).where(ScheduleDB.tenant_id == tenant)
        if keys:
            stmt = stmt.where(ScheduleDB.id.notin_(keys))
        await s.execute(stmt)
