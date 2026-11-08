"""Every run log writes an agent's Knowledge events (B1b, D161), and a workflow's Retrieve and Ingest steps (R1, D166).

A log drops an event type it has no branch for, silently. The surface
contract test holds the orchestration log to it; these hold the agent log (a
chat's) and the schedule log too, and pin what a line may carry: labels, paths
and ids — never the passage text itself.
"""
from __future__ import annotations

import pytest

from core.agent_logger import AgentLogger
from core.orchestration.logger import OrchestrationLogger
from core.schedule_logger import ScheduleLogger

SEARCH = {"type": "knowledge_search", "turn": 2, "query_id": "kq_1", "kb_ids": ["kb_a"], "query": "termination",
          "filtered": True, "hit_count": 2, "labels": ["[K1]", "[K2]"], "degraded": ["vector_unavailable"],
          "cost_usd": 0.0, "latency_ms": 12, "delegate_agent_name": "Researcher"}
READ = {"type": "knowledge_read", "turn": 3, "label": "[K1]", "chunk_id": "kch_1", "doc_id": "kdoc_1",
        "path": "contracts/a.pdf", "window": 1}
CITATIONS = {"type": "knowledge_citations", "unresolved": ["[K9]"], "citations": [
    {"label": "[K1]", "path": "contracts/a.pdf", "page_start": 4, "page_end": 5, "snippet": "SECRET PASSAGE TEXT",
     "quote_check": {"quote": "SECRET QUOTE", "matched": False, "method": "normalized"}},
    {"label": "[K2]", "path": "contracts/b.pdf", "snippet": "SECRET PASSAGE TEXT", "quote_check": None}]}
RETRIEVE = {"type": "knowledge_retrieve", "orch_step_id": "find", "step_name": "Find clauses", "query_id": "kq_7",
            "snapshot_ids": ["ksnap_1"], "hit_count": 3, "cost_usd": 0.0012, "latency_ms": 40,
            "degraded": ["vector_unavailable"]}
INGEST = {"type": "knowledge_ingest", "orch_step_id": "keep", "step_name": "Index tickets", "kb_id": "kb_t",
          "documents": [{"doc_id": "kdoc_1", "record_id": "T-1", "status": "queued"},
                        {"doc_id": "kdoc_2", "record_id": "T-2", "status": "indexed"},
                        {"doc_id": "kdoc_3", "record_id": "T-3", "status": "queued"}]}


def _captured(cls):
    log = object.__new__(cls)
    lines: list[str] = []
    log._write = log._write_bg = lines.append
    return log, lines


@pytest.mark.parametrize("cls", [OrchestrationLogger, AgentLogger, ScheduleLogger])
class TestEveryLog:
    def test_a_search(self, cls):
        log, lines = _captured(cls)
        log.log_event(SEARCH)
        [line] = lines
        assert "KNOWLEDGE SEARCH (from Researcher) (turn 2)" in line and '"termination"' in line
        assert "[K1] [K2]" in line and "kq_1" in line and "(filtered)" in line and "vector_unavailable" in line

    def test_a_read(self, cls):
        log, lines = _captured(cls)
        log.log_event(READ)
        assert lines == [lines[0]] and "KNOWLEDGE READ (turn 3): [K1] ±1 contracts/a.pdf (kch_1)" in lines[0]

    def test_citations_without_their_text(self, cls):
        log, lines = _captured(cls)
        log.log_event(CITATIONS)
        [line] = lines
        assert "[K1] contracts/a.pdf pp. 4–5 ✗ quote not found" in line and "[K2] contracts/b.pdf" in line
        assert "unresolved: [K9]" in line
        assert "SECRET" not in line

    def test_a_retrieve_step(self, cls):
        log, lines = _captured(cls)
        log.log_event(RETRIEVE)
        [line] = lines
        assert "RETRIEVE (step 'Find clauses'): 3 passages · kq_7 · ksnap_1 · $0.0012" in line
        assert "degraded: vector_unavailable" in line

    def test_an_ingest_step(self, cls):
        log, lines = _captured(cls)
        log.log_event(INGEST)
        [line] = lines
        assert "INGEST (step 'Index tickets'): 3 records → kb_t · 2 queued, 1 indexed" in line
        log.log_event({**INGEST, "documents": []})
        assert "INGEST (step 'Index tickets'): 0 records → kb_t" in lines[1]


def test_the_schedule_log_keeps_its_plain_style():
    log, lines = _captured(ScheduleLogger)
    log.log_event(READ)
    log.log_event(RETRIEVE)
    log.log_event(INGEST)
    assert lines[0].startswith("  KNOWLEDGE READ") and lines[1].startswith("  RETRIEVE")
    assert lines[2].startswith("  INGEST")
