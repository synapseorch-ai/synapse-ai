"""The metadata filter in SQL agrees with the Python evaluator — on SQLite always, on Postgres when one is given.

One table of hand-checked cases (`filter_cases.CASES`) and 300 seeded random
filters run over the corpus loaded into the real `knowledge_chunks` table.
Postgres runs for real when `SYNAPSE_TEST_POSTGRES_URL` names one (the nightly
gate, and KB0's throwaway pgvector container); otherwise its SQL is compiled and
checked for the type guards that keep one odd row from failing the query.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from core.knowledge import filters as F
from core.knowledge.filters_sql import FilterColumns, compile_sql, path_prefix_sql
from core.knowledge.testing import corpus
from core.knowledge.testing.filter_cases import CASES, random_filters
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB

TABLE = KnowledgeChunkDB.__table__
COLS = FilterColumns.for_table(TABLE)
ENTRIES = corpus.entries(corpus.SCOPE_A1)
ORDINAL = {e.chunk_id: i for i, e in enumerate(ENTRIES)}


async def _load(session_factory, tenant: str = "default") -> None:
    documents, chunks = corpus.records_for(ENTRIES, tenant)
    async with session_factory() as s:
        for d in documents:
            s.add(KnowledgeDocumentDB(**d))
        await s.flush()
        for c in chunks:
            s.add(KnowledgeChunkDB(**c))
        await s.commit()


async def _select(session_factory, node) -> frozenset[int]:
    stmt = select(KnowledgeChunkDB.chunk_id).where(compile_sql(node, COLS))
    async with session_factory() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return frozenset(ORDINAL[r] for r in rows)


@pytest.fixture
async def sqlite_factory():
    from core.store import build_engine, build_session_factory, init_db
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = build_session_factory(engine)
    await _load(factory)
    yield factory
    await engine.dispose()


@pytest.mark.parametrize("name,flt,expected", CASES, ids=[c[0] for c in CASES])
async def test_sqlite_selects_the_hand_checked_rows(sqlite_factory, name, flt, expected):
    assert await _select(sqlite_factory, F.parse_filter(flt)) == expected


async def test_sqlite_agrees_with_python_on_random_filters(sqlite_factory):
    rows = corpus.rows()
    for flt in random_filters():
        node = F.parse_filter(flt)
        want = frozenset(r["_id"] for r in rows if F.evaluate(node, r))
        assert await _select(sqlite_factory, node) == want, flt


async def test_no_filter_selects_everything(sqlite_factory):
    assert await _select(sqlite_factory, None) == frozenset(range(16))


async def test_path_prefix_is_component_wise(sqlite_factory):
    stmt = select(KnowledgeChunkDB.chunk_id).where(path_prefix_sql(KnowledgeChunkDB.path, "contracts"))
    async with sqlite_factory() as s:
        got = {ORDINAL[r] for r in (await s.execute(stmt)).scalars().all()}
    assert got == {0, 1, 2, 3, 4}


def _pg(flt) -> str:
    return str(compile_sql(F.parse_filter(flt), COLS).compile(dialect=postgresql.dialect(),
                                                               compile_kwargs={"literal_binds": True}))


class TestPostgresSql:
    def test_a_number_is_read_only_where_it_is_one(self):
        sql = _pg({"year": {"$gte": 2024}})
        assert "jsonb_typeof(knowledge_chunks.meta -> 'year') = 'number'" in sql
        assert "::float8" in sql
        assert "CAST((knowledge_chunks.meta ->> 'year') AS FLOAT)" not in sql  # the bare cast that fails a query

    def test_strings_order_by_code_point(self):
        assert 'COLLATE "C"' in _pg({"region": {"$gte": "EU"}})
        assert 'COLLATE "C"' in _pg({"path": {"$lt": "m"}})

    def test_a_list_is_searched_not_compared(self):
        sql = _pg({"tags": "legal"})
        assert "jsonb_typeof(knowledge_chunks.meta -> 'tags') = 'array'" in sql
        assert "@> jsonb_build_array(CAST('legal' AS text))" in sql

    def test_every_leaf_is_two_valued(self):
        sql = _pg({"$not": {"a": {"$gt": 1}}})
        assert sql.startswith("NOT coalesce(")

    def test_sqlite_sql_has_its_own_guards(self):
        sql = str(compile_sql(F.parse_filter({"year": {"$gte": 2024}}), COLS).compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        assert "json_type(knowledge_chunks.meta, '$.\"year\"') IN ('integer', 'real')" in sql


_PG_URL = os.environ.get("SYNAPSE_TEST_POSTGRES_URL", "")


@pytest.fixture
async def pg_factory():
    if not _PG_URL:
        pytest.skip("SYNAPSE_TEST_POSTGRES_URL is not set")
    from core.store import build_engine, build_session_factory
    from core.store.engine import normalise_db_url
    from core.store.models import Base

    engine = build_engine(normalise_db_url(_PG_URL))
    tables = [Base.metadata.tables[n] for n in ("knowledge_documents", "knowledge_chunks")]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=list(reversed(tables))))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = build_session_factory(engine)
    await _load(factory)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=list(reversed(tables))))
    await engine.dispose()


@pytest.mark.parametrize("name,flt,expected", CASES, ids=[c[0] for c in CASES])
async def test_postgres_selects_the_hand_checked_rows(pg_factory, name, flt, expected):
    assert await _select(pg_factory, F.parse_filter(flt)) == expected


async def test_postgres_agrees_with_python_on_random_filters(pg_factory):
    rows = corpus.rows()
    for flt in random_filters():
        node = F.parse_filter(flt)
        want = frozenset(r["_id"] for r in rows if F.evaluate(node, r))
        assert await _select(pg_factory, node) == want, flt
