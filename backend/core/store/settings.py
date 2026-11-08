"""
Settings from the store, scoped to the tenant.

This replaces `core/scale/llm_keys.py`, which loaded every setting from
Postgres — with no tenant filter — and wrote them into `os.environ` as
`SYNAPSE_SETTING_*`, where `load_settings()` read them back out. Two things were
wrong with that beyond the missing filter:

* **Process-global.** `inject_llm_env` ran once at worker startup and only set
  a variable that was not already set, so a worker's settings were fixed for
  its lifetime and identical for every job it ran. One shared fleet cannot work
  that way.
* **Every provider key ended up in the process environment**, where it is
  visible in `/proc/<pid>/environ` and inherited by every subprocess the worker
  spawns — which for this engine includes stdio MCP servers and the transform
  sandbox.

Now settings are read per call, for the tenant in context, and handed to the
caller as a dict. Nothing is written to the environment.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from core.tenancy import get_tenant


async def load_settings_for_tenant() -> dict:
    """The current tenant's settings, overlaid on the shipped defaults.

    Overlaid rather than returned bare so a caller always sees the full key
    set. `load_settings()` has ~85 keys and its callers index them directly;
    a partial dict turns a missing row into a KeyError somewhere unrelated.
    """
    from core.config import default_settings
    from core.store import session
    from core.store.models import SettingDB

    settings = default_settings()
    async with session() as s:
        rows = (
            await s.execute(select(SettingDB).where(SettingDB.tenant_id == get_tenant()))
        ).scalars().all()

    for row in rows:
        try:
            settings[row.key] = json.loads(row.value)
        except (TypeError, ValueError):
            # Stored unencoded by an older build, or hand-edited. Take it raw
            # rather than dropping the setting silently.
            settings[row.key] = row.value
    return settings


async def save_setting(key: str, value) -> None:
    """Write one setting for the current tenant."""
    from core.store import session, upsert
    from core.store.models import SettingDB

    async with session() as s:
        await upsert(
            s,
            SettingDB,
            values={"tenant_id": get_tenant(), "key": key, "value": json.dumps(value)},
            index_elements=["tenant_id", "key"],
        )


async def delete_setting(key: str) -> None:
    """Remove one setting, returning it to the shipped default."""
    from sqlalchemy import delete

    from core.store import session
    from core.store.models import SettingDB

    async with session() as s:
        await s.execute(
            delete(SettingDB).where(
                SettingDB.tenant_id == get_tenant(),
                SettingDB.key == key,
            )
        )


async def save_many(settings: dict) -> None:
    """Write a whole settings dict, storing only what differs from the defaults.

    The settings route reads the current settings, applies the request on top,
    and saves the result — so it hands over all ~85 keys, most of them untouched
    defaults. Writing those verbatim would materialise a row per default on the
    first save and pin the install to *today's* defaults forever: a later change
    to `_DEFAULTS` would never reach it, because every key already has a row
    overriding it.

    So a value equal to its default deletes any row instead of writing one.
    This is exactly symmetric with the read side, which overlays stored rows
    onto `default_settings()`.
    """
    from core.config import default_settings

    defaults = default_settings()
    for key, value in settings.items():
        if key in defaults and value == defaults[key]:
            await delete_setting(key)
        else:
            await save_setting(key, value)
