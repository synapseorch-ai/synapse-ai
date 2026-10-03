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

**The vector leg** (B2a, D163) is an exact cosine scan with numpy over the
float32 BLOBs (`numpy.frombuffer(blob, "<f4")`, the bytes `vectors.pack` wrote).
Each (tenant, KB, snapshot)'s matrix is kept in memory, up to
`VECTOR_CACHE_BYTES` across all of them (least recently used first out), and
checked before every search against one aggregate of the rows: their count,
highest id and id sum. A row written by another process (a worker, a second
replica) changes it and the matrix is read again; this process's own writes
drop it at once. A snapshot too big to keep is read and scored in batches.
Scores are float64 over the float32 values, as the scan before this did, and
ties break by chunk id. The filter and path prefix are one SQL query for the ids
they allow, so a top-k is never padded with rows they exclude. numpy is
imported inside the leg: cloud's API process imports this module and has none.

**A document being deleted is in neither leg** (F335). Its chunks stay in the
table until its delete job runs (and a document mid-index when its file was
deleted writes them after the tombstone), so both legs leave out every chunk
whose document is `deleting`: the keyword leg inside its query, the vector leg
by a mask on each search — never on the kept matrix, whose fingerprint reads
chunk rows only and does not see a document's status change. Only `deleting`:
a document being indexed, or the old version during a re-index, is still found.
"""
from __future__ import annotations

import re
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import reduce
from typing import Any

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
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB

_log = get_logger(__name__)

CAPABILITIES = IndexCapabilities(
    name="sql", version="1", lexical=True, vector=True, filter_pushdown=True, per_leg_scores=True,
    search_mode="exact", max_dims=None, lexical_languages=True,
)

MAX_TERMS = 32
RRF_K = 60
_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_IN_BATCH = 500

#: Bytes of embeddings the vector leg keeps in memory, across every (KB, snapshot) it has searched.
VECTOR_CACHE_BYTES = 256 * 1024 * 1024
#: Rows read and scored at a time when a snapshot is too big to keep, and rows scored per float64 pass.
_SCAN_ROWS = 4096

_T = KnowledgeChunkDB.__table__
_D = KnowledgeDocumentDB.__table__
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


# ── the vector leg's matrices ─────────────────────────────────────────────────

@dataclass
class _Block:
    """Some of one snapshot's embedded rows: ids, documents, float32 vectors, and their norms in float64."""

    ids: list[str]
    docs: list[str]
    matrix: Any  # numpy (n, d) float32
    norms: Any  # numpy (n,) float64

    @property
    def nbytes(self) -> int:
        return int(self.matrix.nbytes + self.norms.nbytes)


def _block(rows: Sequence[Any], snapshot_id: str) -> _Block:
    """Rows of (chunk_id, doc_id, embedding) as one block. Mixed widths are refused, as `upsert` refuses them."""
    import numpy as np

    widths = {len(r.embedding) for r in rows}
    if len(widths) > 1 or any(w % vectors.BYTES_PER_DIM for w in widths):
        raise IndexDimensionError(f"snapshot {snapshot_id} holds vectors of more than one width")
    dims = widths.pop() // vectors.BYTES_PER_DIM
    matrix = np.frombuffer(b"".join(r.embedding for r in rows), dtype="<f4").reshape(len(rows), dims)
    norms = np.empty(len(rows), dtype=np.float64)
    for start in range(0, len(rows), _SCAN_ROWS):
        part = matrix[start:start + _SCAN_ROWS].astype(np.float64)
        norms[start:start + _SCAN_ROWS] = np.sqrt((part * part).sum(axis=1))
    return _Block([r.chunk_id for r in rows], [r.doc_id for r in rows], matrix, norms)


def _cosines(block: _Block, want: Any, want_norm: float, rows: Any | None) -> Any:
    """Cosine of `want` with the block's rows (all, or the positions `rows`), in float64, a batch at a time."""
    import numpy as np

    count = block.matrix.shape[0] if rows is None else len(rows)
    out = np.empty(count, dtype=np.float64)
    for start in range(0, count, _SCAN_ROWS):
        stop = start + _SCAN_ROWS
        pick = slice(start, stop) if rows is None else rows[start:stop]
        # Each row summed on its own, never `@`: BLAS may score two equal rows a bit apart (its kernels differ by
        # a row's place in the batch), and then a tie breaks by noise instead of by chunk id.
        dots = (block.matrix[pick].astype(np.float64) * want).sum(axis=1)
        denom = block.norms[pick] * want_norm
        with np.errstate(divide="ignore", invalid="ignore"):
            out[start:stop] = np.where(denom > 0, dots / np.where(denom > 0, denom, 1.0), 0.0)
    return out


def _best(block: _Block, scope: Scope, want: Any, want_norm: float, limit: int,
          allowed: set[str] | None, gone: set[str] | frozenset[str] = frozenset(),
          ) -> tuple[list[tuple[str, str, str, str, float]], int]:
    """The block's `limit` best rows (with every row tied at the cut, for chunk id to decide), and how many it
    scored. `allowed` are the chunk ids a filter lets through (None: every one); `gone` the documents being
    deleted, whose rows are never scored."""
    import numpy as np

    if allowed is None and not gone:
        rows = None
        count = len(block.ids)
    else:
        keep = ((allowed is None or cid in allowed) and doc not in gone for cid, doc in zip(block.ids, block.docs))
        rows = np.nonzero(np.fromiter(keep, dtype=bool, count=len(block.ids)))[0]
        count = len(rows)
    if count == 0:
        return [], 0
    scores = _cosines(block, want, want_norm, rows)
    k = min(limit, count)
    if k < count:
        kth = np.partition(scores, count - k)[count - k]
        keep = np.nonzero(scores >= kth)[0]
    else:
        keep = np.arange(count)
    at = (lambda i: i) if rows is None else (lambda i: int(rows[i]))
    return ([(block.ids[at(i)], block.docs[at(i)], scope.kb_id, scope.snapshot_id, float(scores[i]))
             for i in keep], count)


class _VectorCache:
    """Each (tenant, KB, snapshot)'s block with the fingerprint it was read at; least recently used out first."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._blocks: OrderedDict[tuple[str, str, str], tuple[tuple, _Block]] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple[str, str, str], fingerprint: tuple) -> _Block | None:
        found = self._blocks.get(key)
        if found is None:
            return None
        if found[0] != fingerprint:
            self._pop(key)
            return None
        self._blocks.move_to_end(key)
        return found[1]

    def put(self, key: tuple[str, str, str], fingerprint: tuple, block: _Block) -> None:
        self._pop(key)
        if block.nbytes > self.capacity:
            return
        self._blocks[key] = (fingerprint, block)
        self._bytes += block.nbytes
        while self._bytes > self.capacity:
            self._pop(next(iter(self._blocks)))

    def drop(self, tenant: str, kb_id: str, snapshot_id: str | None = None) -> None:
        for key in [k for k in self._blocks if k[0] == tenant and k[1] == kb_id
                    and (snapshot_id is None or k[2] == snapshot_id)]:
            self._pop(key)

    def _pop(self, key: tuple[str, str, str]) -> None:
        found = self._blocks.pop(key, None)
        if found is not None:
            self._bytes -= found[1].nbytes

    @property
    def nbytes(self) -> int:
        return self._bytes


class SqlKnowledgeIndex:
    """A `KnowledgeIndex` over `knowledge_chunks` (see the module docstring)."""

    capabilities = CAPABILITIES

    def __init__(self, *, vector_cache_bytes: int = VECTOR_CACHE_BYTES) -> None:
        self._ready: weakref.WeakSet = weakref.WeakSet()
        self._vectors = _VectorCache(vector_cache_bytes)

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
    def _deleting(scopes: Sequence[Scope]):
        """The ids of the scopes' documents being deleted (F335): `(tenant_id, kb_id, status)` is indexed."""
        from core.tenancy import get_tenant
        return select(_D.c.id).where(_D.c.tenant_id == get_tenant(),
                                     _D.c.kb_id.in_(sorted({s.kb_id for s in scopes})), _D.c.status == "deleting")

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
        for kb_id, snapshot_id in {(e.kb_id, e.snapshot_id) for e in entries}:
            self._forget(kb_id, snapshot_id)
        return len(entries)

    def _forget(self, kb_id: str, snapshot_id: str | None = None) -> None:
        """This process wrote: the kept matrices it touched are read again on the next search."""
        from core.tenancy import get_tenant
        self._vectors.drop(get_tenant(), kb_id, snapshot_id)

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
        self._forget(scope.kb_id, scope.snapshot_id)
        return removed

    async def delete_document(self, kb_id: str, doc_id: str, *, snapshot_id: str | None = None) -> int:
        where = [_T.c.kb_id == kb_id, _T.c.doc_id == doc_id]
        if snapshot_id is not None:
            where.append(_T.c.snapshot_id == snapshot_id)
        removed = await self._delete(*where)
        self._forget(kb_id, snapshot_id)
        return removed

    async def delete_prefix(self, kb_id: str, path_prefix: str) -> int:
        if not (path_prefix or "").strip("/"):
            raise ValueError("delete_prefix needs a prefix; drop_kb deletes a whole knowledge base")
        removed = await self._delete(_T.c.kb_id == kb_id, path_prefix_sql(_T.c.path, path_prefix))
        self._forget(kb_id)
        return removed

    async def drop_snapshot(self, scope: Scope) -> int:
        removed = await self._delete(_T.c.kb_id == scope.kb_id, _T.c.snapshot_id == scope.snapshot_id)
        self._forget(scope.kb_id, scope.snapshot_id)
        return removed

    async def drop_kb(self, kb_id: str) -> int:
        removed = await self._delete(_T.c.kb_id == kb_id)
        self._forget(kb_id)
        return removed

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
                               _T.c.doc_id.notin_(self._deleting(query.scopes)),
                               *self._narrow(query.filter, query.path_prefix))
                    .order_by(score.desc(), _T.c.chunk_id.asc()).limit(query.limit))
            rows = (await s.execute(stmt)).all()
        return LegResult("lexical", _candidates((r.chunk_id, r.doc_id, r.kb_id, r.snapshot_id, float(r.score))
                                                for r in rows), filtered=narrowed, exact=True)

    async def search_vector(self, query: VectorQuery) -> LegResult:
        narrowed = query.filter is not None or bool(query.path_prefix and query.path_prefix.strip("/"))
        if not query.scopes or query.limit <= 0:
            return LegResult("vector", (), filtered=narrowed, exact=True, scanned=0)
        import numpy as np

        want = np.asarray([float(x) for x in query.vector], dtype=np.float64)
        want_norm = float(np.sqrt(want @ want))
        found: list[tuple[str, str, str, str, float]] = []
        scanned = 0
        async with self._session() as s:
            await self._ensure(s)
            gone = set((await s.execute(self._deleting(query.scopes))).scalars())
            allowed = None
            narrow = self._narrow(query.filter, query.path_prefix)
            if narrow:
                allowed = set((await s.execute(select(_T.c.chunk_id).where(
                    self._tenant(), self._scopes(query.scopes), _T.c.embedding.isnot(None), *narrow))).scalars())
            for scope in dict.fromkeys(query.scopes):
                async for block in self._snapshot_blocks(s, scope):
                    if block.matrix.shape[1] != len(want):
                        raise IndexDimensionError(f"query has {len(want)} dimensions; snapshot {scope.snapshot_id} "
                                                  f"holds {block.matrix.shape[1]}")
                    best, count = _best(block, scope, want, want_norm, query.limit, allowed, gone)
                    found.extend(best)
                    scanned += count
        found.sort(key=lambda p: (-p[4], p[0]))
        return LegResult("vector", _candidates(found[:query.limit]), filtered=narrowed, exact=True,
                         scanned=scanned)

    async def _snapshot_blocks(self, s, scope: Scope) -> AsyncIterator[_Block]:
        """One snapshot's embedded rows: its kept matrix if still current, else read (and kept if it fits)."""
        from core.tenancy import get_tenant

        where = (self._tenant(), _T.c.kb_id == scope.kb_id, _T.c.snapshot_id == scope.snapshot_id,
                 _T.c.embedding.isnot(None))
        count, highest, total = (await s.execute(
            select(func.count(), func.max(_T.c.id), func.sum(_T.c.id)).where(*where))).one()
        if not count:
            return
        key = (get_tenant(), scope.kb_id, scope.snapshot_id)
        fingerprint = (int(count), int(highest), int(total))
        kept = self._vectors.get(key, fingerprint)
        if kept is not None:
            yield kept
            return
        width = (await s.execute(select(func.length(_T.c.embedding)).where(*where).limit(1))).scalar() or 0
        columns = (_T.c.id, _T.c.chunk_id, _T.c.doc_id, _T.c.embedding)
        if int(count) * (int(width) + 8) <= self._vectors.capacity:
            rows = (await s.execute(select(*columns).where(*where).order_by(_T.c.id))).all()
            block = _block(rows, scope.snapshot_id)
            self._vectors.put(key, fingerprint, block)
            yield block
            return
        last = None
        while True:  # too big to keep: a batch at a time, by id
            stmt = select(*columns).where(*where).order_by(_T.c.id).limit(_SCAN_ROWS)
            if last is not None:
                stmt = stmt.where(_T.c.id > last)
            rows = (await s.execute(stmt)).all()
            if not rows:
                return
            yield _block(rows, scope.snapshot_id)
            last = rows[-1].id

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
