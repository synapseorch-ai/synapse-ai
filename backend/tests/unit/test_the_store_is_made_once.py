"""The store is made once, however many callers ask for it first (F1529).

`get_store()` makes the process's engine on first use and creates the tables (`init_db`). It looked for a store and
then awaited that work, so callers arriving together all found none: each built an engine and ran `create_all` on the
same SQLite file, and all but one failed with "table orchestrations already exists". The server never meets this, as
its lifespan reads the settings from the store before it serves. Anything that drives the engine without the
lifespan does: the nightly's stress test runs twelve chats at once on a fresh database and failed from 2.0 on (the
store came with 2.0, 4200e192). A first use is now one flight that the rest wait for, and one that failed is not
kept, so the next caller tries again.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

import core.store.engine as store


@pytest.fixture
def built(monkeypatch):
    """Each engine `get_store` builds, in order."""
    made = []
    real = store.build_engine

    def counting(url, *args, **kwargs):
        made.append(real(url, *args, **kwargs))
        return made[-1]

    monkeypatch.setattr(store, "build_engine", counting)
    return made


@pytest.fixture
def fails_first(monkeypatch):
    """`init_db` fails on its first call, as a full disk or a locked file would, then works."""
    real = store.init_db
    calls = []

    async def once(engine):
        calls.append(engine)
        if len(calls) == 1:
            raise RuntimeError("disk I/O error")
        await real(engine)

    monkeypatch.setattr(store, "init_db", once)
    return calls


async def _tables_are_there() -> None:
    async with store.session() as s:
        assert (await s.execute(text("SELECT count(*) FROM orchestrations"))).scalar() == 0


async def test_twenty_first_callers_share_one_store(built):
    try:
        factories = await asyncio.gather(*(store.get_store() for _ in range(20)))
        assert len(built) == 1
        assert all(factory is factories[0] for factory in factories)
        await _tables_are_there()
    finally:
        await store.reset_store()


async def test_a_failed_first_use_is_not_kept_and_the_next_caller_tries_again(built, fails_first):
    try:
        with pytest.raises(RuntimeError, match="disk I/O error"):
            await store.get_store()
        assert store._session_factory is None and store._engine is None
        assert await store.get_store() is store._session_factory
        assert len(built) == 2
        await _tables_are_there()
    finally:
        await store.reset_store()


async def test_callers_waiting_on_a_first_use_that_fails_go_on_to_one_store(built, fails_first):
    try:
        results = await asyncio.gather(*(store.get_store() for _ in range(5)), return_exceptions=True)
        failed = [r for r in results if isinstance(r, BaseException)]
        assert [str(e) for e in failed] == ["disk I/O error"]
        made = {id(r) for r in results if not isinstance(r, BaseException)}
        assert len(made) == 1 and len(built) == 2
    finally:
        await store.reset_store()


def test_each_event_loop_waits_on_a_lock_of_its_own(built):
    """The suite clears the store between tests without `reset_store` (conftest's `_isolate_run_checkpoints`), and
    each test has a loop of its own: a lock one loop waited on must not refuse the next loop's callers."""
    async def together():
        return await asyncio.gather(store.get_store(), store.get_store())

    try:
        first = asyncio.run(together())
        store._engine, store._session_factory = None, None
        second = asyncio.run(together())
        assert first[0] is first[1] and second[0] is second[1] and first[0] is not second[0]
        assert len(built) == 2
    finally:
        for engine in built:
            asyncio.run(engine.dispose())
        store._engine, store._session_factory = None, None
