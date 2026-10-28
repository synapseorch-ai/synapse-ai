"""
The small document collections, tenant-scoped.

Repos, database configs, API keys, personal details and messaging channels were
five JSON files behind five `JsonStore` singletons, each bound to a path at
import time. None of them is queried by anything but its own key, so they share
one table rather than getting five schemas — see `CollectionDB`.

The API is deliberately list-shaped, because that is what every caller wants
and what the JSON files provided: `load(name)` returns the items in insertion
order, `save(name, items)` replaces them. The one difference from the files is
that a replace is a diff rather than a truncate-and-rewrite, so two writers do
not silently drop each other's rows for collections that grow.

Singletons — personal details is one document, not a list — use `load_one` /
`save_one`, which store it under an empty key.

Stored references
-----------------
An embedder may keep a credential outside this table and store a *reference* to
it in the document — the value in the JSONB is an address, not a secret. Those
documents are read by code that expects the real value (`tools/sql_agent.py`
hands `connection_string` straight to `create_engine`), so the swap has to
happen on the way out of the store rather than at every call site.

`set_document_resolver()` is that seam. Nothing is registered by default, so the
shipped product reads exactly what it wrote. It is the sixth registration
alongside `set_store`, `set_resource_provider`, `set_job_tenant_resolver`,
`set_api_key_provider`, `set_settings_provider` and `set_blob_store` — same
shape, same reason: the engine describes the hole and the embedder fills it.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete, select

from core.tenancy import get_tenant

logger = logging.getLogger(__name__)

#: Installed by an embedder that stores credentials outside this table.
_document_resolver: Callable[[dict], Awaitable[dict]] | None = None


def set_document_resolver(resolver: Callable[[dict], Awaitable[dict]] | None) -> None:
    """Install a transform applied to every document read from this table.

    Takes one document, returns it with any stored references replaced by their
    values. Called on the read path only: `save` writes what it is given, so the
    embedder is responsible for putting references in rather than secrets.
    """
    global _document_resolver
    _document_resolver = resolver


async def _resolved(document: Any) -> Any:
    """One document, through the resolver if there is one.

    A resolver that raises is allowed to: a reference that cannot be resolved
    means a credential is missing, and returning the document with the address
    still in it would hand `create_engine` a `synsec://` URL and produce a
    confusing driver error instead of a clear one.

    That argument is about the document being *used*. `load` applies it to a
    list, where letting one failure escape hides every sibling — so it skips and
    logs instead. `load_one` keeps this behaviour, because there the broken
    document is the whole answer.
    """
    if _document_resolver is None or not isinstance(document, dict):
        return document
    return await _document_resolver(document)


async def load(name: str) -> list[dict]:
    """Every item in `name`, oldest first — minus any that cannot be resolved.

    **One unresolvable document does not fail the list.** It used to: `load`
    resolved eagerly and let the first failure escape, so a single stale
    reference took every sibling with it. What the caller then saw was not an
    error about that document — `tools/sql_agent.py` catches broadly and returns
    `[]` — but "no databases are configured", for a workspace with two of them,
    one of which was fine.

    That is the wrong trade for a *list*. The argument in `_resolved` still
    holds for the document being used, and `load_one` still lets it through
    unchanged: there, the broken document is the answer. Here it is one row
    among several, and hiding the working ones to report the broken one is a
    strictly worse outcome than the reverse.

    A skipped document is logged at WARNING with its key, because the failure
    now has no other symptom — it is a row that quietly stops appearing. Using
    the missing item still fails loudly at the point of use, which is where a
    clear error belongs.
    """
    from core.store import session
    from core.store.models import CollectionDB

    async with session() as s:
        rows = (
            await s.execute(
                select(CollectionDB)
                .where(
                    CollectionDB.tenant_id == get_tenant(),
                    CollectionDB.collection == name,
                )
                .order_by(CollectionDB.created_at, CollectionDB.key)
            )
        ).scalars().all()

    items: list[dict] = []
    for row in rows:
        try:
            items.append(await _resolved(row.value))
        except Exception:
            logger.warning(
                "collection %r: skipping %r, its stored reference could not be resolved",
                name,
                row.key,
                exc_info=True,
            )
    return items


async def save(name: str, items: list[dict], key_field: str = "id") -> None:
    """Replace `name` with `items`, keyed by `key_field`.

    Items without the key field fall back to their position, which keeps
    list-shaped collections that never had ids working unchanged.
    """
    from core.store import session, upsert
    from core.store.models import CollectionDB

    tenant = get_tenant()
    keys: list[str] = []

    async with session() as s:
        for index, item in enumerate(items):
            key = str(item.get(key_field) or index) if isinstance(item, dict) else str(index)
            keys.append(key)
            await upsert(
                s,
                CollectionDB,
                values={
                    "tenant_id": tenant,
                    "collection": name,
                    "key": key,
                    "value": item,
                },
                index_elements=["tenant_id", "collection", "key"],
                update=["value"],
            )

        stmt = delete(CollectionDB).where(
            CollectionDB.tenant_id == tenant,
            CollectionDB.collection == name,
        )
        if keys:
            stmt = stmt.where(CollectionDB.key.notin_(keys))
        await s.execute(stmt)


async def get_item(name: str, key: str) -> dict | None:
    """One item of `name` by its key, as stored; None when there is none."""
    from core.store import session
    from core.store.models import CollectionDB

    async with session() as s:
        row = (
            await s.execute(
                select(CollectionDB).where(
                    CollectionDB.tenant_id == get_tenant(),
                    CollectionDB.collection == name,
                    CollectionDB.key == key,
                )
            )
        ).scalar_one_or_none()
    return row.value if row is not None else None


async def put_item(name: str, key: str, item: dict) -> None:
    """Write one item of `name` and nothing else (`save` replaces the whole list): for collections whose items
    two people may change at once, each on its own (the browser's recordings, F1142)."""
    from core.store import session, upsert
    from core.store.models import CollectionDB

    async with session() as s:
        await upsert(
            s,
            CollectionDB,
            values={"tenant_id": get_tenant(), "collection": name, "key": key, "value": item},
            index_elements=["tenant_id", "collection", "key"],
            update=["value"],
        )


async def delete_item(name: str, key: str) -> bool:
    """Remove one item of `name`. False when there was none."""
    from core.store import session
    from core.store.models import CollectionDB

    async with session() as s:
        result = await s.execute(
            delete(CollectionDB).where(
                CollectionDB.tenant_id == get_tenant(),
                CollectionDB.collection == name,
                CollectionDB.key == key,
            )
        )
    return bool(result.rowcount)


async def load_one(name: str, default: dict | None = None, *, resolve: bool = True) -> dict:
    """A singleton document, or `default` when nothing is stored.

    `resolve=False` returns it as stored, references and all: what
    `core.variables` compares against to know which values were secrets (F854).
    """
    from core.store import session
    from core.store.models import CollectionDB

    async with session() as s:
        row = (
            await s.execute(
                select(CollectionDB).where(
                    CollectionDB.tenant_id == get_tenant(),
                    CollectionDB.collection == name,
                    CollectionDB.key == "",
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return dict(default or {})
    return await _resolved(row.value) if resolve else row.value


async def save_one(name: str, document: dict) -> None:
    from core.store import session, upsert
    from core.store.models import CollectionDB

    async with session() as s:
        await upsert(
            s,
            CollectionDB,
            values={
                "tenant_id": get_tenant(),
                "collection": name,
                "key": "",
                "value": document,
            },
            index_elements=["tenant_id", "collection", "key"],
            update=["value"],
        )
