"""A1's test kit: Knowledge switched on, indexing faked, files in the Vault.

`FakeIndexer` stands in for B1's `pipeline.index_document` (the frozen hand-off)
and `FakeIndex` for the search index's delete calls, so ingestion is tested on
its own — before and after B1 lands. `install_fake_indexing` resolves the
entrypoint and B1's snapshot function to the fakes; `unbuilt_indexing` makes
both resolve to nothing, the state before B1.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from core.knowledge.pipeline_contract import INDEX_ENTRYPOINT, IndexContext, IndexOutcome
from core.knowledge.status import StageReport
from core.knowledge.types import Usage

SNAPSHOT = "ksnap_" + "5" * 32
SNAPSHOT_ENTRYPOINT = "core.knowledge.snapshots:serving_snapshot"


def use_saved_settings(monkeypatch) -> None:
    """Read settings from the store, as a server does (`core/server.py` installs this provider
    at import); a bare unit test otherwise sees only the shipped defaults. Restored afterwards."""
    import core.config as config
    from core import settings_runtime

    monkeypatch.setattr(config, "_settings_provider", config._settings_provider)
    settings_runtime.install_provider()


async def enable_knowledge(**more: Any) -> None:
    """Switch Knowledge on (after `use_saved_settings`), with any other settings in `more`."""
    from core.routes.settings import save_settings

    await save_settings({"knowledge_enabled": True, **more})


def write_vault(path: str, data: bytes | str) -> None:
    from core.storage import get_blob_store

    store = get_blob_store()
    if isinstance(data, str):
        store.put(f"vault/{path}", data)
    else:
        store.put_bytes(f"vault/{path}", data)


class FakeIndexer:
    """`index_document`: records every context, answers `indexed`, or raises `raises`.

    `during` runs inside the call — to race the job (a delete, a newer write)."""

    def __init__(self, *, raises: BaseException | None = None, status: str = "indexed",
                 status_reason: str | None = None, during: Callable[[IndexContext], Any] | None = None):
        self.contexts: list[IndexContext] = []
        self.raises = raises
        self.status = status
        self.status_reason = status_reason
        self.during = during

    async def __call__(self, ctx: IndexContext) -> IndexOutcome:
        self.contexts.append(ctx)
        if self.during is not None:
            result = self.during(ctx)
            if asyncio.iscoroutine(result):
                await result
        if self.raises is not None:
            raise self.raises
        usage = [Usage(stage="embed", cost_source="local", model="hash", provider="fake", input_tokens=7,
                       est_usd=0.001)]
        return IndexOutcome(status=self.status, status_reason=self.status_reason, chunks=len(ctx.parsed.blocks),
                            tokens=7, stages=[StageReport(stage="chunk", status="ok"),
                                              StageReport(stage="index", status="ok")], usage=usage)


def install_fake_indexing(monkeypatch, indexer: FakeIndexer, *, snapshot: str = SNAPSHOT) -> None:
    from core.knowledge import seams

    real = seams._load

    async def serving_snapshot(kb):
        return snapshot

    fakes = {INDEX_ENTRYPOINT: indexer, SNAPSHOT_ENTRYPOINT: serving_snapshot}
    monkeypatch.setattr(seams, "_load", lambda dotted: fakes[dotted] if dotted in fakes else real(dotted))


def unbuilt_indexing(monkeypatch) -> None:
    from core.knowledge import seams

    real = seams._load
    gone = {INDEX_ENTRYPOINT, SNAPSHOT_ENTRYPOINT, seams.DEFAULT_INDEX}
    monkeypatch.setattr(seams, "_load", lambda dotted: None if dotted in gone else real(dotted))


class FakeIndex:
    """The two deletes ingestion calls on a `KnowledgeIndex`; `fail` makes them raise."""

    def __init__(self, fail: BaseException | None = None):
        self.fail = fail
        self.deleted: list[tuple[str, str]] = []
        self.dropped: list[str] = []

    async def delete_document(self, kb_id: str, doc_id: str, *, snapshot_id: str | None = None) -> int:
        if self.fail:
            raise self.fail
        self.deleted.append((kb_id, doc_id))
        return 1

    async def drop_kb(self, kb_id: str) -> int:
        if self.fail:
            raise self.fail
        self.dropped.append(kb_id)
        return 1
