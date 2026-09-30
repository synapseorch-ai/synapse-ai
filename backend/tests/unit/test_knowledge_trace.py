"""The retrieval trace (v1): what it holds, what it may never hold, and its size (D153 #16)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.knowledge import trace as T
from core.knowledge.types import IndexCapabilities

FULL = IndexCapabilities(name="memory", version="1", lexical=True, vector=True, filter_pushdown=True,
                         per_leg_scores=True, search_mode="exact")

EXAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "traces" / "example_v1.json"


def _example() -> dict:
    return json.loads(EXAMPLE.read_text("utf-8"))


def test_the_example_round_trips():
    trace = T.Trace.model_validate(_example())
    assert T.Trace.model_validate_json(trace.model_dump_json()) == trace
    assert trace.trace_version == 1 and trace.hits[0].label == "[K1]"


def test_the_index_is_described_by_its_capabilities():
    info = T.IndexInfo.of(FULL)
    assert info.capabilities["filter_pushdown"] is True and info.name == "memory"


@pytest.mark.parametrize("key", sorted(T.FORBIDDEN_KEYS))
def test_a_trace_never_holds_an_embedding(key):
    data = _example()
    data["request"]["filters"] = {key: [0.1, 0.2]}
    with pytest.raises(ValidationError, match="embeddings"):
        T.Trace.model_validate(data)


def test_the_vector_leg_may_be_named():
    data = _example()
    data["pipeline"]["kb_00000000000a"]["fusion"]["weights"] = {"lexical": 1.0, "vector": 2.0}
    assert T.Trace.model_validate(data).pipeline["kb_00000000000a"].fusion.weights["vector"] == 2.0


def test_a_snippet_is_at_most_200_characters():
    data = _example()
    data["hits"][0]["snippet"] = "x" * 201
    with pytest.raises(ValidationError):
        T.Trace.model_validate(data)


def test_an_oversized_trace_is_refused_and_fit_trims_candidates_not_hits():
    data = _example()
    many = [{"chunk_id": f"kch_{i:032x}", "rank": i + 1, "score": 1.0 / (i + 1)} for i in range(6000)]
    data["legs"][0]["candidates"] = many
    with pytest.raises(ValidationError, match="at most"):
        T.Trace.model_validate(data)
    fitted = T.fit(data)
    trace = T.Trace.model_validate(fitted)
    assert len(trace.legs[0].candidates) < 6000
    assert any(d.code == "truncated" for d in trace.degraded)
    assert len(trace.hits) == len(_example()["hits"])


def test_degraded_codes_are_named():
    assert {"post_filtered", "rerank_unconfigured", "ann_approximate", "truncated"} <= set(T.DEGRADED_CODES)


def test_replay_types():
    report = T.ReplayReport(query_id="kq_1", mode="exact", reproducible=False,
                            checks=[T.ReplayCheck(name="doc_sha_unchanged", ok=False, detail="kdoc_1 changed")],
                            diffs={"hits": T.ReplayDiff(removed=["kch_1"])})
    assert T.ReplayReport.model_validate_json(report.model_dump_json()) == report
