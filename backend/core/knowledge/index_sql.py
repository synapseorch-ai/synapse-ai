"""`SqlKnowledgeIndex` — the search index that lives in the engine's own `knowledge_chunks` table (B1a, D158).

The default `KnowledgeIndex` (`seams.DEFAULT_INDEX`) in every deployment that
does not plug its own in: OSS on SQLite, OSS on Postgres. Cloud plugs in
`PgKnowledgeIndex` (pgvector); Qdrant and Pinecone arrive later the same way.

**Rows are the truth.** The pipeline writes each chunk's row (content, context,
the filter fields) before calling `upsert`; `upsert` only writes the embedding
onto rows that already exist, and every delete here deletes rows. So "what the
index holds" and "what the tables hold" can never disagree.

**The keyword leg.**

* SQLite: two FTS5 tables, `knowledge_chunks_fts_en` (Porter stemming) for
  English and `knowledge_chunks_fts` (`unicode61`, diacritics folded) for every
  other language. Each is an *external-content* table over a view of
  `knowledge_chunks` filtered by `lang`, keyed by the chunk's rowid, and kept
  in step by triggers on `knowledge_chunks` — so a document deleted by
  ingestion, or by the foreign key's cascade, leaves nothing behind in the
  index. Scores are `-bm25()`.
* Postgres: a GIN expression index over `textsearch.tsv_expression_sql()`,
  each term through `plainto_tsquery` (bound, so no text ever becomes query
  syntax), the terms OR-ed with `||`, scored by `ts_rank_cd`.

The two dialects rank differently by design (BM25 against cover density), and
two more differences are known and tested: SQLite stems only English (Porter),
where Postgres stems every configured language; and SQLite folds diacritics
("zurich" finds "Zürich"), where Postgres does not — its configurations do not
unaccent, and the frozen tsvector expression has no `unaccent`.

The query text is reduced to its words (`[^\\W_]+`, lowercased, de-duplicated,
at most 32) and the words are OR-ed: a keyword leg feeding fusion and rerank
must match a natural-language question that no single chunk answers word for
word. Both dialects create their schema lazily, once per engine, here and only
here — the one documented dialect exception beside `filters_sql`.

**The vector leg** is an exact cosine scan in pure Python over the float32 BLOBs
(`vectors.unpack`), with the tenant, scopes, filter and path prefix applied in
the same SQL that fetches them. Exact and dependency-free (cloud's API has no
numpy), but slow on a large knowledge base: B2 replaces the scan with numpy
and a per-(KB, snapshot) cache. Nothing in B1 searches vectors outside tests.
"""
from __future__ import annotations

import math
import re
import weakref
from collections.abc import Sequence
from functools import reduce

from sqlalchemy import and_, bindparam, column, delete, false, func, literal_column, or_, select, table
from sqlalchemy import text as sql_text
from sqlalchemy import update

from core.knowledge import vectors
from core.knowledge.errors import IndexDimensionError, KnowledgeIndexError
from core.knowledge.filters_sql import FilterColumns, compile_sql, path_prefix_sql
from core.knowledge.textsearch import normalize_language, tsv_expression_sql
from core.knowledge.types import (
    Candidate, HybridQuery, IndexCapabilities, IndexEntry, IndexStats, LegResult, LexicalQuery, Scope,
    VectorQuery,
)
from core.log import get_logger
from core.store.models import KnowledgeChunkDB

_log = get_logger(__name__)

CAPABILITIES = IndexCapabilities(
    name="sql", version="1", lexical=True, vector=True, filter_pushdown=True, per_leg_scores=True,
    search_mode="exact", max_dims=None, lexical_languages=True,
)

MAX_TERMS = 32
RRF_K = 60
_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_IN_BATCH = 500

_T = KnowledgeChunkDB.__table__
_COLS = FilterColumns.for_table(_T)

FTS_EN = "knowledge_chunks_fts_en"
FTS_OTHER = "knowledge_chunks_fts"
PG_TSV_INDEX = "ix_knowledge_chunks_tsv"


def query_terms(text: str) -> list[str]:
    """The words of a keyword query, in order, lowercased, once each, at most `MAX_TERMS`."""
    seen: dict[str, None] = {}
    for match in _TERM.finditer(text or ""):
        seen.setdefault(match.group(0).lower(), None)
        if len(seen) >= MAX_TERMS:
            break
    return list(seen)


def _fts_table(lang: str) -> str:
    return FTS_EN if normalize_language(lang) == "english" else FTS_OTHER


def _sqlite_ddl() -> list[str]:
    ddl = []
    for name, predicate, tokenize in (
        (FTS_EN, "lang = 'english'", "porter unicode61 remove_diacritics 2"),
        (FTS_OTHER, "lang <> 'english'", "unicode61 remove_diacritics 2"),
    ):
        ddl.append(f"CREATE VIEW IF NOT EXISTS {name}_src AS "
                   f"SELECT id, context, content FROM knowledge_chunks WHERE {predicate}")
        ddl.append(f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5(context, content, "
                   f"content='{name}_src', content_rowid='id', tokenize='{tokenize}')")

    def put(row: str) -> str:
        return (f"INSERT INTO {FTS_EN}(rowid, context, content) SELECT {row}.id, {row}.context, {row}.content "
                f"WHERE {row}.lang = 'english'; "
                f"INSERT INTO {FTS_OTHER}(rowid, context, content) SELECT {row}.id, {row}.context, {row}.content "
                f"WHERE {row}.lang <> 'english';")

    def drop(row: str) -> str:
        return (f"INSERT INTO {FTS_EN}({FTS_EN}, rowid, context, content) "
                f"SELECT 'delete', {row}.id, {row}.context, {row}.content WHERE {row}.lang = 'english'; "
                f"INSERT INTO {FTS_OTHER}({FTS_OTHER}, rowid, context, content) "
                f"SELECT 'delete', {row}.id, {row}.context, {row}.content WHERE {row}.lang <> 'english';")

    ddl.append(f"CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_ai AFTER INSERT ON knowledge_chunks "
               f"BEGIN {put('new')} END")
    ddl.append(f"CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_au AFTER UPDATE OF context, content, lang "
               f"ON knowledge_chunks BEGIN {drop('old')} {put('new')} END")
    ddl.append(f"CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_ad AFTER DELETE ON knowledge_chunks "
               f"BEGIN {drop('old')} END")
    return ddl


def _cosine_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


class SqlKnowledgeIndex:
    """A `KnowledgeIndex` over `knowledge_chunks` (see the module docstring)."""

    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._ready: weakref.WeakSet = weakref.WeakSet()

    # ── plumbing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _session():
        from core.store import session
        return session()

    async def _ensure(self, s) -> str:
        """The dialect's name, after making sure this engine has the keyword leg's schema."""
        bind = s.get_bind()
        dialect = bind.dialect.name
        if bind in self._ready:
            return dialect
        if dialect == "sqlite":
            done = (await s.execute(sql_text(
                "SELECT count(*) FROM sqlite_master WHERE name = 'knowledge_chunks_fts_ad'"))).scalar_one()
            if not done:
                for statement in _sqlite_ddl():
                    await s.execute(sql_text(statement))
                for name in (FTS_EN, FTS_OTHER):  # rows written before the index existed; idempotent
                    await s.execute(sql_text(f"INSERT INTO {name}({name}) VALUES ('rebuild')"))
                await s.commit()
        elif dialect == "postgresql":
            await s.execute(sql_text(
                f"CREATE INDEX IF NOT EXISTS {PG_TSV_INDEX} ON knowledge_chunks USING GIN "
                f"(({tsv_expression_sql()}))"))
            await s.commit()
        else:  # pragma: no cover - the engine supports these two
            raise KnowledgeIndexError(f"The SQL knowledge index does not support {dialect}.")
        self._ready.add(bind)
        return dialect

    @staticmethod
    def _tenant():
        from core.tenancy import get_tenant
        return _T.c.tenant_id == get_tenant()

    @staticmethod
    def _scopes(scopes: Sequence[Scope]):
        if not scopes:
            return false()
        return or_(*[and_(_T.c.kb_id == s.kb_id, _T.c.snapshot_id == s.snapshot_id) for s in scopes])

    @staticmethod
    def _narrow(node, path_prefix: str | None) -> list:
        where = []
        if node is not None:
            where.append(compile_sql(node, _COLS))
        if path_prefix and path_prefix.strip("/"):
            where.append(path_prefix_sql(_T.c.path, path_prefix))
        return where

    # ── writes ─────────────────────────────────────────────────────────────────

    async def upsert(self, entries: Sequence[IndexEntry]) -> int:
        """Write each entry's embedding onto its existing row. Idempotent per chunk id."""
        if not entries:
            return 0
        widths: dict[tuple[str, str], int] = {}
        for e in entries:
            if e.vector is None:
                continue
            key = (e.kb_id, e.snapshot_id)
            if widths.setdefault(key, len(e.vector)) != len(e.vector):
                raise IndexDimensionError(f"snapshot {e.snapshot_id} is being given vectors of "
                                          f"{widths[key]} and {len(e.vector)} dimensions")
        ids = [e.chunk_id for e in entries]
        async with self._session() as s:
            await self._ensure(s)
            for (kb_id, snapshot_id), width in widths.items():
                existing = (await s.execute(
                    select(func.length(_T.c.embedding)).where(
                        self._tenant(), _T.c.kb_id == kb_id, _T.c.snapshot_id == snapshot_id,
                        _T.c.embedding.isnot(None), _T.c.chunk_id.notin_(ids),
                    ).limit(1))).scalar()
                if existing is not None and existing // vectors.BYTES_PER_DIM != width:
                    raise IndexDimensionError(f"snapshot {snapshot_id} holds "
                                              f"{existing // vectors.BYTES_PER_DIM}-dimensional vectors, not {width}")
            found: set[str] = set()
            for i in range(0, len(ids), _IN_BATCH):
                batch = ids[i:i + _IN_BATCH]
                found |= set((await s.execute(select(_T.c.chunk_id).where(
                    self._tenant(), _T.c.chunk_id.in_(batch)))).scalars())
            missing = [cid for cid in ids if cid not in found]
            if missing:
                raise KnowledgeIndexError(f"{len(missing)} chunk rows do not exist (the SQL index writes onto the "
                                          f"rows the pipeline inserted first), e.g. {missing[0]}", retryable=False)
            stmt = update(_T).where(
                self._tenant(), _T.c.chunk_id == bindparam("_cid"), _T.c.kb_id == bindparam("_kb"),
                _T.c.snapshot_id == bindparam("_snap"),
            ).values(embedding=bindparam("_emb"))
            await s.execute(stmt, [
                {"_cid": e.chunk_id, "_kb": e.kb_id, "_snap": e.snapshot_id,
                 "_emb": vectors.pack(e.vector) if e.vector is not None else None}
                for e in entries
            ])
        return len(entries)

    async def _delete(self, *where) -> int:
        async with self._session() as s:
            await self._ensure(s)
            result = await s.execute(delete(_T).where(self._tenant(), *where))
            return int(result.rowcount or 0)

    async def delete_chunks(self, scope: Scope, chunk_ids: Sequence[str]) -> int:
        if not chunk_ids:
            return 0
        removed = 0
        ids = list(chunk_ids)
        for i in range(0, len(ids), _IN_BATCH):
            removed += await self._delete(_T.c.kb_id == scope.kb_id, _T.c.snapshot_id == scope.snapshot_id,
                                          _T.c.chunk_id.in_(ids[i:i + _IN_BATCH]))
        return removed

    async def delete_document(self, kb_id: str, doc_id: str, *, snapshot_id: str | None = None) -> int:
        where = [_T.c.kb_id == kb_id, _T.c.doc_id == doc_id]
        if snapshot_id is not None:
            where.append(_T.c.snapshot_id == snapshot_id)
        return await self._delete(*where)

    async def delete_prefix(self, kb_id: str, path_prefix: str) -> int:
        if not (path_prefix or "").strip("/"):
            raise ValueError("delete_prefix needs a prefix; drop_kb deletes a whole knowledge base")
        return await self._delete(_T.c.kb_id == kb_id, path_prefix_sql(_T.c.path, path_prefix))

    async def drop_snapshot(self, scope: Scope) -> int:
        return await self._delete(_T.c.kb_id == scope.kb_id, _T.c.snapshot_id == scope.snapshot_id)

    async def drop_kb(self, kb_id: str) -> int:
        return await self._delete(_T.c.kb_id == kb_id)

    # ── searches ───────────────────────────────────────────────────────────────

    async def search_lexical(self, query: LexicalQuery) -> LegResult:
        narrowed = query.filter is not None or bool(query.path_prefix and query.path_prefix.strip("/"))
        terms = query_terms(query.text)
        if not terms or not query.scopes or query.limit <= 0:
            return LegResult("lexical", (), filtered=narrowed, exact=True, scanned=0)
        async with self._session() as s:
            dialect = await self._ensure(s)
            if dialect == "sqlite":
                name = _fts_table(query.lang)
                fts = table(name, column("rowid"))
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
                score = (-func.bm25(literal_column(name))).label("score")
                stmt = (select(_T.c.chunk_id, _T.c.doc_id, _T.c.kb_id, _T.c.snapshot_id, score)
                        .select_from(fts.join(_T, _T.c.id == fts.c.rowid))
                        .where(literal_column(name).op("MATCH")(bindparam("_match", match))))
            else:
                config = literal_column(f"'{normalize_language(query.lang)}'::regconfig")
                tsquery = reduce(lambda a, b: a.op("||")(b),
                                 [func.plainto_tsquery(config, bindparam(f"_t{i}", t)) for i, t in enumerate(terms)])
                tsv = literal_column(tsv_expression_sql("knowledge_chunks.lang", "knowledge_chunks.context",
                                                        "knowledge_chunks.content"))
                score = func.ts_rank_cd(tsv, tsquery).label("score")
                stmt = (select(_T.c.chunk_id, _T.c.doc_id, _T.c.kb_id, _T.c.snapshot_id, score)
                        .where(tsv.op("@@", is_comparison=True)(tsquery)))
            stmt = (stmt.where(self._tenant(), self._scopes(query.scopes),
                               *self._narrow(query.filter, query.path_prefix))
                    .order_by(score.desc(), _T.c.chunk_id.asc()).limit(query.limit))
            rows = (await s.execute(stmt)).all()
        return LegResult("lexical", _candidates((r.chunk_id, r.doc_id, r.kb_id, r.snapshot_id, float(r.score))
                                                for r in rows), filtered=narrowed, exact=True)

    async def search_vector(self, query: VectorQuery) -> LegResult:
        narrowed = query.filter is not None or bool(query.path_prefix and query.path_prefix.strip("/"))
        want = [float(x) for x in query.vector]
        if not query.scopes or query.limit <= 0:
            return LegResult("vector", (), filtered=narrowed, exact=True, scanned=0)
        norm = _cosine_norm(want)
        async with self._session() as s:
            await self._ensure(s)
            rows = (await s.execute(
                select(_T.c.chunk_id, _T.c.doc_id, _T.c.kb_id, _T.c.snapshot_id, _T.c.embedding).where(
                    self._tenant(), self._scopes(query.scopes), _T.c.embedding.isnot(None),
                    *self._narrow(query.filter, query.path_prefix)))).all()
        scored = []
        for r in rows:
            have = vectors.unpack(r.embedding)
            if len(have) != len(want):
                raise IndexDimensionError(f"query has {len(want)} dimensions; snapshot {r.snapshot_id} "
                                          f"holds {len(have)}")
            denom = norm * _cosine_norm(have)
            cosine = sum(a * b for a, b in zip(have, want)) / denom if denom else 0.0
            scored.append((r.chunk_id, r.doc_id, r.kb_id, r.snapshot_id, cosine))
        scored.sort(key=lambda p: (-p[4], p[0]))
        return LegResult("vector", _candidates(scored[:query.limit]), filtered=narrowed, exact=True,
                         scanned=len(rows))

    async def search_hybrid(self, query: HybridQuery) -> LegResult:
        """Both legs fused by weighted RRF (`Σ w / (k + rank)`, ties by chunk id). The pipeline uses
        the legs separately (`per_leg_scores`); this answers callers that want one list."""
        lexical = await self.search_lexical(query.lexical)
        vector = await self.search_vector(query.vector)
        scores: dict[str, float] = {}
        seen: dict[str, Candidate] = {}
        for leg in (lexical, vector):
            weight = float(query.weights.get(leg.leg, 1.0))
            for c in leg.candidates:
                scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + weight / (RRF_K + c.rank)
                seen.setdefault(c.chunk_id, c)
        fused = sorted(scores.items(), key=lambda p: (-p[1], p[0]))[:max(query.lexical.limit, query.vector.limit)]
        return LegResult("hybrid", _candidates((cid, seen[cid].doc_id, seen[cid].kb_id, seen[cid].snapshot_id, sc)
                                               for cid, sc in fused),
                         filtered=lexical.filtered and vector.filtered, exact=True,
                         notes=(*lexical.notes, *vector.notes))

    async def stats(self, scope: Scope) -> IndexStats:
        async with self._session() as s:
            await self._ensure(s)
            row = (await s.execute(select(
                func.count(), func.count(_T.c.embedding), func.max(func.length(_T.c.embedding)),
                func.sum(func.length(_T.c.embedding)),
            ).where(self._tenant(), _T.c.kb_id == scope.kb_id, _T.c.snapshot_id == scope.snapshot_id))).one()
        chunks, vectors_, widest, total = row
        return IndexStats(chunks=int(chunks or 0), vectors=int(vectors_ or 0),
                          dims=int(widest) // vectors.BYTES_PER_DIM if widest else None,
                          bytes=int(total) if total else None)


def _candidates(rows) -> tuple[Candidate, ...]:
    return tuple(Candidate(chunk_id=cid, doc_id=doc, kb_id=kb, snapshot_id=snap, score=score, rank=i + 1)
                 for i, (cid, doc, kb, snap, score) in enumerate(rows))
