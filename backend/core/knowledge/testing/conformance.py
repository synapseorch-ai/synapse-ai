"""The cases every `KnowledgeIndex` must pass — the contract, executable.

Plain async functions, no pytest, so OSS's tests (B1: `SqlKnowledgeIndex` on
SQLite and Postgres) and cloud's (Knowledge E1: `PgKnowledgeIndex` on the test
Postgres) run the same list. Each case gets a **fresh, empty** index and an
`IndexHarness`, and raises `AssertionError` on a violation. A case lists the
capabilities it needs; one the index lacks is skipped, and the case that
checks the index *says* it lacks it runs instead.

Usage, in a test file:

    @pytest.mark.parametrize("case", conformance.CASES, ids=lambda c: c.name)
    async def test_conformance(case, make_index, harness):
        await conformance.run_case(case, make_index, harness)
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace

from core.knowledge.errors import IndexDimensionError
from core.knowledge.filters import evaluate, parse_filter
from core.knowledge.seams import KnowledgeIndex
from core.knowledge.testing import corpus
from core.knowledge.testing.filter_cases import CASES as FILTER_CASES
from core.knowledge.types import (
    HybridQuery, IndexCapabilities, IndexEntry, LegResult, LexicalQuery, Scope, VectorQuery,
)


async def _no_prepare(entries: Sequence[IndexEntry]) -> None:
    return None


@dataclass
class IndexHarness:
    """What a case needs from its environment besides the index.

    * `tenant(name)` — a context manager that makes `name` the current tenant
      (OSS: `core.tenancy.tenant_scope` with multi-tenancy switched on; cloud:
      `org_scope` + `tenant_scope` on a real org).
    * `other_tenant` — a second tenant's id, for the isolation case.
    * `prepare(entries)` — for an index that updates rows of its own tables
      (the SQL and Postgres indexes write `embedding` onto existing
      `knowledge_chunks` rows): insert the records for `entries` first, in the
      current tenant. The in-memory reference needs nothing.
    """

    tenant: Callable[[str], AbstractContextManager]
    other_tenant: str = "kb0-other"
    prepare: Callable[[Sequence[IndexEntry]], Awaitable[None]] = field(default=_no_prepare)


@dataclass(frozen=True)
class ConformanceCase:
    name: str
    run: Callable[[KnowledgeIndex, IndexHarness], Awaitable[None]]
    needs: frozenset[str] = frozenset()
    lacks: frozenset[str] = frozenset()

    def applies(self, caps: IndexCapabilities) -> bool:
        return (all(bool(getattr(caps, n)) for n in self.needs)
                and not any(bool(getattr(caps, n)) for n in self.lacks))


async def run_case(case: ConformanceCase, make_index: Callable[[], KnowledgeIndex], harness: IndexHarness) -> bool:
    """Run one case on a fresh index. Returns False (and runs nothing) when it does not apply."""
    index = make_index()
    if not case.applies(index.capabilities):
        return False
    await case.run(index, harness)
    return True


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def _load(index: KnowledgeIndex, harness: IndexHarness, entries: Sequence[IndexEntry]) -> None:
    await harness.prepare(entries)
    await index.upsert(entries)


def _ids(result: LegResult) -> list[str]:
    return [c.chunk_id for c in result.candidates]


def _by_ordinal(scope: Scope, ordinal: int, doc_offset: int = 0) -> str:
    return corpus.entries(scope, doc_offset=doc_offset)[ordinal].chunk_id


def _vector_order(entries: Sequence[IndexEntry], query: Sequence[float]) -> list[tuple[str, float]]:
    scored = [(e.chunk_id, corpus.cosine(list(e.vector), list(query))) for e in entries if e.vector is not None]
    scored.sort(key=lambda p: (-p[1], p[0]))
    return scored


def _well_formed(result: LegResult) -> None:
    ranks = [c.rank for c in result.candidates]
    _check(ranks == list(range(1, len(ranks) + 1)), f"{result.leg}: ranks are 1-based and consecutive, got {ranks}")
    scores = [c.score for c in result.candidates]
    _check(all(a >= b for a, b in zip(scores, scores[1:])), f"{result.leg}: scores must not increase: {scores}")
    ids = _ids(result)
    _check(len(ids) == len(set(ids)), f"{result.leg}: a chunk appears twice")


# ── the cases ──────────────────────────────────────────────────────────────────

async def capabilities_are_declared(index: KnowledgeIndex, harness: IndexHarness) -> None:
    caps = index.capabilities
    _check(isinstance(caps, IndexCapabilities), "capabilities is an IndexCapabilities")
    _check(bool(caps.name) and bool(caps.version), "capabilities name the index and its version")
    _check(caps.search_mode in ("exact", "ann"), "search_mode is exact or ann")
    _check(caps.lexical or caps.vector, "an index has at least one leg")
    _check(isinstance(index, KnowledgeIndex), "the index implements every KnowledgeIndex method")


async def upsert_is_idempotent(index: KnowledgeIndex, harness: IndexHarness) -> None:
    entries = corpus.all_entries()
    await _load(index, harness, entries)
    await index.upsert(entries)
    stats = await index.stats(corpus.SCOPE_A1)
    _check(stats.chunks == 16, f"16 chunks after upserting the same entries twice, got {stats.chunks}")
    _check(stats.vectors == 15, f"15 vectors (one chunk is unembedded), got {stats.vectors}")
    _check(stats.dims == corpus.DIMS, f"{corpus.DIMS} dimensions, got {stats.dims}")


async def lexical_finds_the_words(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    result = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "termination notice", "simple", None, 5))
    _well_formed(result)
    _check(bool(result.candidates), "a keyword search for words in the corpus finds something")
    _check(result.candidates[0].chunk_id == _by_ordinal(corpus.SCOPE_A1, 1),
           "the chunk holding both words ranks first")


async def lexical_is_case_insensitive(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    result = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "INVOICE", "simple", None, 10))
    found = set(_ids(result))
    for ordinal in (5, 9):
        _check(_by_ordinal(corpus.SCOPE_A1, ordinal) in found, f"'INVOICE' finds chunk {ordinal}")


async def lexical_takes_plain_text(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    for text in ('"unbalanced', "invoice*", "NEAR(renewal payment)", "-renewal", "a:b", "'; DROP TABLE x; --",
                 "()", "AND OR NOT", "renewal^2", "{}[]", "\\"):
        result = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), text, "simple", None, 5))
        _well_formed(result)
        _check(len(result.candidates) <= 5, f"limit holds for {text!r}")
    for text in ("", "   "):
        result = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), text, "simple", None, 5))
        _check(not result.candidates, f"an empty query ({text!r}) finds nothing")


async def a_missing_leg_says_so(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    result = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "renewal", "simple", None, 5))
    _check(not result.candidates and "lexical_unavailable" in result.notes,
           "an index without a keyword leg answers empty and notes lexical_unavailable")


async def vector_order_is_exact(index: KnowledgeIndex, harness: IndexHarness) -> None:
    entries = corpus.all_entries()
    await _load(index, harness, entries)
    query = corpus.direction(0, 1, 0.25)
    result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), query, None, 10))
    _well_formed(result)
    expected = _vector_order([e for e in entries if e.snapshot_id == corpus.SNAP_A1], query)[:10]
    if index.capabilities.search_mode == "exact":
        _check(result.exact, "an exact index says its results are exact")
        _check(_ids(result) == [cid for cid, _ in expected], "exact cosine order, ties by chunk id")
        for got, (_, want) in zip(result.candidates, expected):
            _check(math.isclose(got.score, want, abs_tol=1e-5), f"cosine {got.score} vs {want}")
    else:
        top = {cid for cid, _ in expected[:5]}
        recall = len(top & set(_ids(result)[:5])) / 5
        _check(recall >= 0.8, f"an approximate index keeps recall@5 >= 0.8, got {recall}")
        _check("ann_approximate" in result.notes or not result.exact, "an approximate index says so")


async def ties_break_by_chunk_id(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(5), None, 2))
    twins = sorted([_by_ordinal(corpus.SCOPE_A1, 10), _by_ordinal(corpus.SCOPE_A1, 11)])
    _check(_ids(result) == twins, "two chunks with the same score come back in chunk id order")


async def limit_is_respected(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(0), None, 3))
    _check(len(result.candidates) == 3, f"limit 3 returns 3, got {len(result.candidates)}")


async def scopes_are_isolated(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    a2 = await index.search_vector(VectorQuery((corpus.SCOPE_A2,), corpus.direction(0), None, 50))
    _check(bool(a2.candidates) and all(c.snapshot_id == corpus.SNAP_A2 for c in a2.candidates),
           "a search in one snapshot returns only that snapshot's chunks")
    b = await index.search_vector(VectorQuery((corpus.SCOPE_B1,), corpus.direction(0), None, 50))
    _check(bool(b.candidates) and all(c.kb_id == corpus.KB_B for c in b.candidates),
           "a search in one knowledge base returns only its chunks")
    both = await index.search_vector(VectorQuery((corpus.SCOPE_A1, corpus.SCOPE_B1), corpus.direction(0), None, 50))
    _check({c.snapshot_id for c in both.candidates} == {corpus.SNAP_A1, corpus.SNAP_B1},
           "a search over two scopes returns both and nothing else")


async def tenants_are_isolated(index: KnowledgeIndex, harness: IndexHarness) -> None:
    mine = corpus.entries(corpus.SCOPE_A1)
    await _load(index, harness, mine)
    # Distinct document ids: `knowledge_documents.id` is a global key, as real ids (random) always are.
    theirs = [replace(e, text=e.text + " zebracorn secret")
              for e in corpus.entries(corpus.SCOPE_A1, doc_offset=200)]
    with harness.tenant(harness.other_tenant):
        await _load(index, harness, theirs)
        found = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "zebracorn", "simple", None, 5)) \
            if index.capabilities.lexical else None
        if found is not None:
            _check(bool(found.candidates), "the other tenant finds its own chunks")
    stats = await index.stats(corpus.SCOPE_A1)
    _check(stats.chunks == 16, f"one tenant's writes do not add to another's, got {stats.chunks}")
    if index.capabilities.lexical:
        leaked = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "zebracorn", "simple", None, 5))
        _check(not leaked.candidates, "one tenant never finds another's text")
    with harness.tenant(harness.other_tenant):
        await index.drop_kb(corpus.KB_A)
    after = await index.stats(corpus.SCOPE_A1)
    _check(after.chunks == 16, "another tenant's delete does not touch this tenant's chunks")


async def filters_run_inside_the_index(index: KnowledgeIndex, harness: IndexHarness) -> None:
    entries = corpus.entries(corpus.SCOPE_A1)
    await _load(index, harness, entries)
    query = corpus.direction(0, 3, 0.5)
    for name, flt, _expected in FILTER_CASES:
        node = parse_filter(flt)
        result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), query, node, 16))
        _check(result.filtered, f"{name}: an index with filter pushdown says it filtered")
        want = [cid for cid, _ in _vector_order([e for e in entries if evaluate(node, e.fields)], query)]
        _check(_ids(result) == want, f"{name}: the filter selects exactly the matching chunks, in cosine order")
    top = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(7), parse_filter(
        {"doc_type": "contract"}), 3))
    _check(len(top.candidates) == 3, "a filtered top-k is filled when enough chunks match, never padded")
    if index.capabilities.lexical:
        lex = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "data processing", "simple",
                                                      parse_filter({"doc_type": "contract"}), 10))
        _check(_ids(lex) == [_by_ordinal(corpus.SCOPE_A1, 0)], "the keyword leg filters too")


async def path_prefix_runs_inside_the_index(index: KnowledgeIndex, harness: IndexHarness) -> None:
    """CCR-1: a path prefix narrows a leg inside the query, component-wise, like a filter."""
    entries = corpus.entries(corpus.SCOPE_A1)
    await _load(index, harness, entries)
    under_contracts = {_by_ordinal(corpus.SCOPE_A1, n) for n in range(5)}
    if index.capabilities.vector:
        query = corpus.direction(0, 2, 0.5)
        for prefix in ("contracts", "contracts/", "/contracts"):
            result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), query, None, 16, path_prefix=prefix))
            _well_formed(result)
            _check(result.filtered, f"{prefix!r}: an index with filter pushdown says it applied the prefix")
            _check(set(_ids(result)) == under_contracts,
                   f"{prefix!r}: only contracts/…, never contracts2/…, got {len(result.candidates)} chunks")
            if index.capabilities.search_mode == "exact":
                want = [cid for cid, _ in _vector_order([e for e in entries if e.chunk_id in under_contracts], query)]
                _check(_ids(result) == want, f"{prefix!r}: in cosine order")
        top = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(7), None, 3,
                                                    path_prefix="contracts"))
        _check(len(top.candidates) == 3, "a prefixed top-k is filled when enough chunks match, never padded")
        both = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(1), parse_filter(
            {"region": "CH"}), 16, path_prefix="contracts"))
        _check(set(_ids(both)) == {_by_ordinal(corpus.SCOPE_A1, 3), _by_ordinal(corpus.SCOPE_A1, 4)},
               "a prefix and a filter both apply")
    if index.capabilities.lexical:
        lex = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "renewal", "simple", None, 10,
                                                      path_prefix="contracts"))
        _check(lex.filtered and _ids(lex) == [_by_ordinal(corpus.SCOPE_A1, 2)],
               "the keyword leg applies the prefix: 'renewal' under contracts/ is only the Acme chunk")
        other = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "renewal", "simple", None, 10,
                                                        path_prefix="contracts2"))
        _check(_ids(other) == [_by_ordinal(corpus.SCOPE_A1, 5)], "'contracts2' is its own folder")


async def no_pushdown_says_so(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.entries(corpus.SCOPE_A1))
    result = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(0),
                                                   parse_filter({"doc_type": "contract"}), 16))
    _check(not result.filtered, "an index without filter pushdown answers filtered=False, so the caller post-filters")


async def delete_document_is_scoped(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    removed = await index.delete_document(corpus.KB_A, corpus.doc_id(1), snapshot_id=corpus.SNAP_A1)
    _check(removed == 3, f"deleting document 1 in one snapshot removes its 3 chunks, got {removed}")
    _check((await index.stats(corpus.SCOPE_A1)).chunks == 13, "13 left in that snapshot")
    _check((await index.stats(corpus.SCOPE_A2)).chunks == 16, "the other snapshot is untouched")
    await index.delete_document(corpus.KB_A, corpus.doc_id(1))
    _check((await index.stats(corpus.SCOPE_A2)).chunks == 13, "without a snapshot, every snapshot's copy goes")


async def delete_prefix_is_component_wise(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    removed = await index.delete_prefix(corpus.KB_A, "contracts")
    _check(removed == 10, f"'contracts' removes contracts/… in both snapshots (10), got {removed}")
    left = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(2), None, 16))
    _check(_by_ordinal(corpus.SCOPE_A1, 5) in _ids(left), "'contracts' never matches 'contracts2/…'")
    _check((await index.stats(corpus.SCOPE_B1)).chunks == 16, "another knowledge base is untouched")
    try:
        await index.delete_prefix(corpus.KB_A, "/")
    except ValueError:
        pass
    else:
        raise AssertionError("an empty prefix is refused (drop_kb deletes a whole knowledge base)")


async def delete_chunks_and_drops(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    two = [_by_ordinal(corpus.SCOPE_A1, 0), _by_ordinal(corpus.SCOPE_A1, 1)]
    _check(await index.delete_chunks(corpus.SCOPE_A1, two) == 2, "delete_chunks removes what it names")
    _check((await index.stats(corpus.SCOPE_A1)).chunks == 14, "14 left")
    await index.drop_snapshot(corpus.SCOPE_A1)
    _check((await index.stats(corpus.SCOPE_A1)).chunks == 0, "drop_snapshot empties the snapshot")
    _check((await index.stats(corpus.SCOPE_A2)).chunks == 16, "and leaves the others")
    await index.drop_kb(corpus.KB_A)
    _check((await index.stats(corpus.SCOPE_A2)).chunks == 0, "drop_kb empties every snapshot of the KB")
    _check((await index.stats(corpus.SCOPE_B1)).chunks == 16, "and no other KB")


async def mixed_widths_are_refused(index: KnowledgeIndex, harness: IndexHarness) -> None:
    entries = corpus.entries(corpus.SCOPE_A1)
    await _load(index, harness, entries)
    wide = replace(entries[0], chunk_id="kch_" + "f" * 32, vector=[1.0] * (corpus.DIMS + 1))
    try:
        await harness.prepare([wide])
        await index.upsert([wide])
    except IndexDimensionError:
        pass
    else:
        raise AssertionError("a vector of another width in the same snapshot is refused")
    try:
        await index.search_vector(VectorQuery((corpus.SCOPE_A1,), [1.0] * (corpus.DIMS + 1), None, 5))
    except IndexDimensionError:
        pass
    else:
        raise AssertionError("a query of the wrong width is refused")


async def unembedded_chunks_skip_the_vector_leg(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.entries(corpus.SCOPE_A1))
    parent = _by_ordinal(corpus.SCOPE_A1, 15)
    vec = await index.search_vector(VectorQuery((corpus.SCOPE_A1,), corpus.direction(7), None, 16))
    _check(parent not in _ids(vec), "a chunk with no vector never comes back from the vector leg")
    if index.capabilities.lexical:
        lex = await index.search_lexical(LexicalQuery((corpus.SCOPE_A1,), "parent section", "simple", None, 5))
        _check(parent in _ids(lex), "but the keyword leg still finds it")


async def hybrid_answers(index: KnowledgeIndex, harness: IndexHarness) -> None:
    await _load(index, harness, corpus.all_entries())
    lexical = LexicalQuery((corpus.SCOPE_A1,), "renewal invoice", "simple", None, 10)
    vector = VectorQuery((corpus.SCOPE_A1,), corpus.direction(2), None, 10)
    result = await index.search_hybrid(HybridQuery(lexical, vector, {"lexical": 1.0, "vector": 1.0}))
    _well_formed(result)
    _check(result.leg == "hybrid" and bool(result.candidates), "a hybrid search answers as one fused leg")
    _check(all(c.snapshot_id == corpus.SNAP_A1 for c in result.candidates), "within its scope")


CASES: list[ConformanceCase] = [
    ConformanceCase("capabilities_are_declared", capabilities_are_declared),
    ConformanceCase("upsert_is_idempotent", upsert_is_idempotent),
    ConformanceCase("lexical_finds_the_words", lexical_finds_the_words, needs=frozenset({"lexical"})),
    ConformanceCase("lexical_is_case_insensitive", lexical_is_case_insensitive, needs=frozenset({"lexical"})),
    ConformanceCase("lexical_takes_plain_text", lexical_takes_plain_text, needs=frozenset({"lexical"})),
    ConformanceCase("a_missing_leg_says_so", a_missing_leg_says_so, lacks=frozenset({"lexical"})),
    ConformanceCase("vector_order_is_exact", vector_order_is_exact, needs=frozenset({"vector"})),
    ConformanceCase("ties_break_by_chunk_id", ties_break_by_chunk_id, needs=frozenset({"vector"})),
    ConformanceCase("limit_is_respected", limit_is_respected, needs=frozenset({"vector"})),
    ConformanceCase("scopes_are_isolated", scopes_are_isolated, needs=frozenset({"vector"})),
    ConformanceCase("tenants_are_isolated", tenants_are_isolated),
    ConformanceCase("filters_run_inside_the_index", filters_run_inside_the_index,
                    needs=frozenset({"vector", "filter_pushdown"})),
    ConformanceCase("path_prefix_runs_inside_the_index", path_prefix_runs_inside_the_index,
                    needs=frozenset({"filter_pushdown"})),
    ConformanceCase("no_pushdown_says_so", no_pushdown_says_so, needs=frozenset({"vector"}),
                    lacks=frozenset({"filter_pushdown"})),
    ConformanceCase("delete_document_is_scoped", delete_document_is_scoped),
    ConformanceCase("delete_prefix_is_component_wise", delete_prefix_is_component_wise, needs=frozenset({"vector"})),
    ConformanceCase("delete_chunks_and_drops", delete_chunks_and_drops),
    ConformanceCase("mixed_widths_are_refused", mixed_widths_are_refused, needs=frozenset({"vector"})),
    ConformanceCase("unembedded_chunks_skip_the_vector_leg", unembedded_chunks_skip_the_vector_leg,
                    needs=frozenset({"vector"})),
    ConformanceCase("hybrid_answers", hybrid_answers, lacks=frozenset({"per_leg_scores"})),
]
