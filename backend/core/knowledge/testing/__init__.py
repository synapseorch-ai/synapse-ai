"""Test kit for Knowledge, shipped inside the package so every lane — and cloud,
which imports the engine from `vendor/` — builds against the same fakes.

* `fakes` — a hashing embedder, a reranker, a recording dispatcher, an OCR fake.
* `reference_index` — an in-memory `KnowledgeIndex` that is the conformance
  suite's reference; its capabilities can be turned down to simulate an
  external store.
* `corpus` — a small synthetic corpus with known rankings.
* `filter_cases` — hand-checked filter cases plus a seeded generator.
* `conformance` — the cases every `KnowledgeIndex` must pass: plain async
  functions, no pytest, so a cloud test can run them too.

Nothing outside `testing` may import this package (a test holds that).
"""
