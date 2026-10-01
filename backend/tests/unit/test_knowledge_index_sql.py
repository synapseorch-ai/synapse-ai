"""`SqlKnowledgeIndex` (B1a, D158): KB0's conformance suite on SQLite always, on Postgres when one is given,
plus what the suite does not reach — the keyword leg's filters and languages, FTS kept right by triggers,
rows written before the index existed, and the refusals.

Postgres runs when `SYNAPSE_TEST_POSTGRES_URL` names a throwaway database (a `pgvector/pgvector:pg18`
container, never the shared :55432).
"""
from __future__ import annotations

import os
from dataclasses import replace

import pytest
from sqlalchemy import insert, select, text

from core.knowledge.errors import IndexDimensionError, KnowledgeIndexError
from core.knowledge.filters import evaluate, parse_filter
from core.knowledge.index_sql import FTS_EN, FTS_OTHER, SqlKnowledgeIndex, query_terms
from core.knowledge.testing import conformance, corpus
from core.knowledge.testing.filter_cases import CASES as FILTER_CASES
from core.knowledge.types import HybridQuery, IndexEntry, LexicalQuery, VectorQuery
from core.scale.context import set_resource_provider
from core.store.models import Base, KnowledgeChunkDB, KnowledgeDocumentDB
from core.tenancy import get_tenant, tenant_scope

_PG_URL = os.environ.get("SYNAPSE_TEST_POSTGRES_URL", "")
_TABLES = ("knowledge_documents", "knowledge_chunks")


class _Provider:
    async def resolve_mcp_servers(self):
        return []


@pytest.fixture(params=["sqlite", "postgres"])
async def dialect(request, monkeypatch):
    """The store every `core.store.session()` opens: conftest's per-test SQLite file, or a Postgres."""
    if request.param == "sqlite":
        yield "sqlite"
        return
    if not _PG_URL:
        pytest.skip("SYNAPSE_TEST_POSTGRES_URL is not set")
    import core.store.engine as store_mod
    from core.store import build_engine, build_session_factory
    from core.store.engine import normalise_db_url

    engine = build_engine(normalise_db_url(_PG_URL))
    tables = [Base.metadata.tables[n] for n in _TABLES]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=list(reversed(tables))))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    monkeypatch.setattr(store_mod, "_session_factory", build_session_factory(engine))
    yield "postgres"
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=list(reversed(tables))))
    await engine.dispose()


async def _prepare(entries) -> None:
    """Insert the rows `entries` stand for, in the current tenant — what the pipeline does before `upsert`."""
    from core.store import session

    documents, chunks = corpus.records_for(list(entries), get_tenant())
    async with session() as s:
        have = set((await s.execute(select(KnowledgeDocumentDB.id).where(
            KnowledgeDocumentDB.id.in_([d["id"] for d in documents])))).scalars())
        fresh = [d for d in documents if d["id"] not in have]
        if fresh:
            await s.execute(insert(KnowledgeDocumentDB.__table__), fresh)
        await s.execute(insert(KnowledgeChunkDB.__table__), chunks)


@pytest.fixture
def harness(dialect):
    set_resource_provider(_Provider())
    yield conformance.IndexHarness(tenant=tenant_scope, prepare=_prepare)
    set_resource_provider(None)


# ── KB0's suite ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", conformance.CASES, ids=lambda c: c.name)
async def test_conformance(case, harness):
    if not case.applies(SqlKnowledgeIndex.capabilities):
        pytest.skip(f"{case.name} tests a capability this index has")
    assert await conformance.run_case(case, SqlKnowledgeIndex, harness)


def test_every_case_but_the_missing_capability_ones_applies():
    skipped = {c.name for c in conformance.CASES if not c.applies(SqlKnowledgeIndex.capabilities)}
    assert skipped == {"a_missing_leg_says_so", "no_pushdown_says_so", "hybrid_answers"}


# ── the keyword leg ────────────────────────────────────────────────────────────

#: One word from each of the corpus's sixteen chunks: an OR query that matches all of them.
_EVERY_CHUNK = "master termination renewal vertrag kündigung invoice late quarterly risks notes ticket pricing " \
               "enterprise note parent"


async def test_the_keyword_leg_filters_inside_the_query(harness):
    entries = corpus.entries(corpus.SCOPE_A1)
    await _prepare(entries)
    index = SqlKnowledgeIndex()
    await index.upsert(entries)
    everything = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), _EVERY_CHUNK, "simple", None, 50))
    assert len(everything.candidates) == 16
    for name, flt, _expected in FILTER_CASES:
        node = parse_filter(flt)
        result = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), _EVERY_CHUNK, "simple", node, 50))
        assert result.filtered, name
        want = {e.chunk_id for e in entries if evaluate(node, e.fields)}
        assert {c.chunk_id for c in result.candidates} == want, name


async def test_english_is_stemmed_and_other_languages_are_not(harness, dialect):
    english = [replace(e, lang="english") for e in corpus.entries(corpus.SCOPE_A1)]
    await _prepare(english)
    plain = corpus.entries(corpus.SCOPE_B1, doc_offset=100)
    await _prepare(plain)
    index = SqlKnowledgeIndex()
    stemmed = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "terminating renewals", "english",
                                                      None, 5))
    assert {c.chunk_id for c in stemmed.candidates} >= {corpus.entries(corpus.SCOPE_A1)[i].chunk_id for i in (1, 2)}
    literal = await index.search_lexical(LexicalQuery((corpus.SCOPE_B1,), "terminating renewals", "simple",
                                                      None, 5))
    assert literal.candidates == (), "no stemming outside English"
    folded = await index.search_lexical(LexicalQuery((corpus.SCOPE_B1,), "zurich kundigung", "simple", None, 5))
    # A known dialect difference: SQLite's unicode61 folds diacritics; Postgres's text-search configurations
    # do not, and the frozen tsvector expression has no `unaccent`.
    if dialect == "sqlite":
        assert folded.candidates, "SQLite folds diacritics: 'zurich' finds 'Zürich'"
    else:
        assert folded.candidates == (), "Postgres does not fold diacritics"
    accented = await index.search_lexical(LexicalQuery((corpus.SCOPE_B1,), "zürich", "simple", None, 5))
    assert accented.candidates, "the accented word is found on both"


async def test_rows_written_before_the_index_existed_are_found(harness):
    await _prepare(corpus.entries(corpus.SCOPE_A1))  # no index call yet: the schema does not exist
    result = await SqlKnowledgeIndex().search_lexical(
        LexicalQuery((corpus.SCOPE_A1,), "churn", "simple", None, 5))
    assert [c.chunk_id for c in result.candidates] == [corpus.entries(corpus.SCOPE_A1)[7].chunk_id]


async def test_a_cascade_delete_leaves_nothing_in_the_keyword_index(harness, dialect):
    from core.store import session

    entries = corpus.entries(corpus.SCOPE_A1)
    await _prepare(entries)
    index = SqlKnowledgeIndex()
    await index.upsert(entries)
    async with session() as s:  # ingestion deletes the document row; the foreign key takes its chunks
        await s.execute(KnowledgeDocumentDB.__table__.delete().where(KnowledgeDocumentDB.id == corpus.doc_id(4)))
    gone = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "churn revenue risks", "simple", None, 5))
    assert gone.candidates == ()
    assert (await index.stats(corpus.SCOPE_A1)).chunks == 14
    if dialect == "sqlite":
        async with session() as s:
            for name in (FTS_EN, FTS_OTHER):
                await s.execute(text(f"INSERT INTO {name}({name}) VALUES ('integrity-check')"))


async def test_a_changed_chunk_is_reindexed_by_its_trigger(harness):
    from core.store import session

    entries = corpus.entries(corpus.SCOPE_A1)
    await _prepare(entries)
    index = SqlKnowledgeIndex()
    await index.upsert(entries)
    async with session() as s:
        await s.execute(KnowledgeChunkDB.__table__.update().where(
            KnowledgeChunkDB.chunk_id == entries[7].chunk_id).values(content="Walrus migration notes.",
                                                                       lang="english"))
    assert (await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "churn", "simple", None, 5))
            ).candidates == ()
    moved = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "migrating", "english", None, 5))
    assert [c.chunk_id for c in moved.candidates] == [entries[7].chunk_id]


def test_query_terms_are_words_only():
    assert query_terms('NEAR(renewal "payment") -x a:b') == ["near", "renewal", "payment", "x", "a", "b"]
    assert query_terms("snake_case Zürich zürich") == ["snake", "case", "zürich"]
    assert query_terms("!!! ... ()") == []
    assert len(query_terms(" ".join(f"w{i}" for i in range(100)))) == 32


# ── the rest ───────────────────────────────────────────────────────────────────

async def test_upsert_refuses_rows_that_do_not_exist(harness):
    entry = corpus.entries(corpus.SCOPE_A1)[0]
    with pytest.raises(KnowledgeIndexError):
        await SqlKnowledgeIndex().upsert([entry])


async def test_a_batch_of_mixed_widths_is_refused_before_any_write(harness):
    entries = corpus.entries(corpus.SCOPE_A1)[:2]
    await _prepare(entries)
    mixed = [entries[0], replace(entries[1], vector=[1.0] * (corpus.DIMS + 2))]
    with pytest.raises(IndexDimensionError):
        await SqlKnowledgeIndex().upsert(mixed)
    assert (await SqlKnowledgeIndex().stats(corpus.SCOPE_A1)).vectors == 0


async def test_an_unembedded_upsert_clears_the_vector(harness):
    entries = corpus.entries(corpus.SCOPE_A1)
    await _prepare(entries)
    index = SqlKnowledgeIndex()
    await index.upsert(entries)
    await index.upsert([IndexEntry(**{**entries[0].__dict__, "vector": None})])
    assert (await index.stats(corpus.SCOPE_A1)).vectors == 14


async def test_hybrid_fuses_both_legs_by_rrf(harness):
    await _prepare(corpus.all_entries())
    index = SqlKnowledgeIndex()
    await index.upsert(corpus.all_entries())
    lexical = LexicalQuery((corpus.SCOPE_A1,), "renewal invoice", "simple", None, 10)
    vector = VectorQuery((corpus.SCOPE_A1,), corpus.direction(2), None, 10)
    result = await index.search_hybrid(HybridQuery(lexical, vector, {"lexical": 1.0, "vector": 1.0}))
    assert result.leg == "hybrid" and result.candidates
    assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))
    top = result.candidates[0].chunk_id
    assert top == corpus.entries(corpus.SCOPE_A1)[5].chunk_id, "first on both legs, first fused"
    assert all(c.snapshot_id == corpus.SNAP_A1 for c in result.candidates)


# ── B2a (D163): the numpy vector leg ───────────────────────────────────────────

def _random_entries(count: int, dims: int, seed: int = 7) -> list[IndexEntry]:
    """`count` chunks of KB A's first snapshot with seeded random vectors, two of them the same vector."""
    import random

    rng = random.Random(seed)
    base = corpus.entries(corpus.SCOPE_A1)[0]
    out = []
    for n in range(count):
        vector = [rng.uniform(-1.0, 1.0) for _ in range(dims)]
        out.append(replace(base, chunk_id="kch_" + f"{n + 1:032x}", vector=vector))
    out[-1] = replace(out[-1], vector=list(out[0].vector))  # a tie, broken by chunk id
    return out


def _reference(entries, query, limit):
    """The scan B1a shipped: float32 storage, float64 cosine, ties by chunk id."""
    from core.knowledge import vectors

    scored = [(e.chunk_id, corpus.cosine(vectors.unpack(vectors.pack(e.vector)), list(query))) for e in entries]
    scored.sort(key=lambda p: (-p[1], p[0]))
    return scored[:limit]


async def test_the_numpy_scan_answers_what_the_pure_python_one_did(harness):
    import random

    entries = _random_entries(150, 48)
    query = [random.Random(3).uniform(-1, 1) for _ in range(48)]
    with harness.tenant("t1"):
        index = SqlKnowledgeIndex()
        await harness.prepare(entries)
        await index.upsert(entries)
        result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), query, None, 20))
    want = _reference(entries, query, 20)
    assert [c.chunk_id for c in result.candidates] == [cid for cid, _ in want]
    for got, (_, score) in zip(result.candidates, want):
        assert abs(got.score - score) < 1e-9
    assert result.exact and result.scanned == 150


async def test_a_snapshot_s_matrix_is_read_once_then_kept(harness, monkeypatch):
    import core.knowledge.index_sql as index_sql

    reads = []
    real = index_sql._block
    monkeypatch.setattr(index_sql, "_block", lambda rows, snap: reads.append(len(rows)) or real(rows, snap))
    with harness.tenant("t1"):
        index = SqlKnowledgeIndex()
        await harness.prepare(corpus.all_entries())
        await index.upsert(corpus.all_entries())
        query = VectorQuery((corpus.SCOPE_A1,), corpus.direction(0), None, 5)
        first = await index.search_vector(query)
        second = await index.search_vector(query)
    assert reads == [15], "read once (15 embedded chunks), then kept"
    assert first == second and index._vectors.nbytes > 0


async def test_a_write_from_another_process_is_seen_at_the_next_search(harness):
    """Another replica, or the worker that indexed: its rows change the fingerprint, and the matrix is read again."""
    with harness.tenant("t1"):
        searcher, writer = SqlKnowledgeIndex(), SqlKnowledgeIndex()
        await harness.prepare(corpus.entries(corpus.SCOPE_A1))
        await writer.upsert(corpus.entries(corpus.SCOPE_A1))
        query = VectorQuery((corpus.SCOPE_A1,), corpus.direction(7), None, 50)
        before = await searcher.search_vector(query)
        extra = replace(corpus.entries(corpus.SCOPE_A1)[0], chunk_id="kch_" + "0" * 32, vector=corpus.direction(7))
        await harness.prepare([extra])
        await writer.upsert([extra])
        after = await searcher.search_vector(query)
    assert extra.chunk_id not in [c.chunk_id for c in before.candidates]
    assert after.candidates[0].chunk_id == extra.chunk_id and len(after.candidates) == len(before.candidates) + 1


async def test_this_process_s_own_writes_drop_what_it_kept(harness):
    with harness.tenant("t1"):
        index = SqlKnowledgeIndex()
        await harness.prepare(corpus.all_entries())
        await index.upsert(corpus.all_entries())
        for scope in (corpus.SCOPE_A1, corpus.SCOPE_B1):
            await index.search_vector(VectorQuery((scope,), corpus.direction(0), None, 3))
        kept = index._vectors.nbytes
        await index.drop_snapshot(corpus.SCOPE_A1)
        assert 0 < index._vectors.nbytes < kept, "only the dropped snapshot's matrix goes"
        await index.drop_kb(corpus.KB_B)
        assert index._vectors.nbytes == 0


async def test_a_snapshot_too_big_to_keep_is_scanned_in_batches(harness, monkeypatch):
    import core.knowledge.index_sql as index_sql

    entries = _random_entries(40, 16)
    query = corpus.direction(1, 2, 0.5) + [0.0] * 8
    with harness.tenant("t1"):
        await harness.prepare(entries)
        await SqlKnowledgeIndex().upsert(entries)
        kept = await SqlKnowledgeIndex().search_vector(VectorQuery((corpus.SCOPE_A1,), query, None, 12))
        monkeypatch.setattr(index_sql, "_SCAN_ROWS", 7)
        small = SqlKnowledgeIndex(vector_cache_bytes=100)
        streamed = await small.search_vector(VectorQuery((corpus.SCOPE_A1,), query, None, 12))
    assert [c.chunk_id for c in streamed.candidates] == [c.chunk_id for c in kept.candidates]
    assert streamed.scanned == 40 and small._vectors.nbytes == 0


def test_the_least_recently_searched_matrix_leaves_first():
    import numpy as np

    from core.knowledge.index_sql import _Block, _VectorCache

    def block() -> _Block:
        return _Block(["a"], ["d"], np.zeros((1, 4), dtype="<f4"), np.zeros(1))

    cache = _VectorCache(capacity=2 * block().nbytes)
    cache.put(("t", "kb", "s1"), (1,), block())
    cache.put(("t", "kb", "s2"), (1,), block())
    assert cache.get(("t", "kb", "s1"), (1,)) is not None  # s1 used: s2 is now the oldest
    cache.put(("t", "kb", "s3"), (1,), block())
    assert cache.get(("t", "kb", "s2"), (1,)) is None and cache.get(("t", "kb", "s1"), (1,)) is not None
    assert cache.get(("t", "kb", "s1"), (2,)) is None, "a changed fingerprint is a miss, and the stale one goes"
    assert cache.nbytes == block().nbytes


def test_importing_the_index_does_not_import_numpy():
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2]
    script = (f"import sys; sys.path.insert(0, {str(backend)!r}); import core.knowledge.index_sql; "
              f"print('numpy' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.stdout.strip() == "False", result.stderr[-1500:]
