"""The chat stream carries an agent's Knowledge events to the page (B1b, D161).

The stream rebuilds or passes each event by name and drops the rest, so the
page could never draw a search, a read or an answer's Sources without these.
A delegate's events keep saying whose they are. The widget's own allowlist is
untouched: a visitor sees none of them (`test_widget_routes.py`).
"""
from __future__ import annotations

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events

SEARCH = {"type": "knowledge_search", "turn": 1, "query_id": "kq_1", "kb_ids": ["kb_a"], "query": "fees",
          "filtered": False, "hit_count": 1, "labels": ["[K1]"], "degraded": [], "cost_usd": 0.0, "latency_ms": 3}
READ = {"type": "knowledge_read", "turn": 2, "label": "[K1]", "chunk_id": "kch_1", "doc_id": "kdoc_1",
        "path": "contracts/a.pdf", "window": 1}
CITATIONS = {"type": "knowledge_citations", "unresolved": ["[K9]"],
             "citations": [{"label": "[K1]", "path": "contracts/a.pdf", "page_start": 3}]}


async def test_search_read_and_citations_reach_the_page_before_the_answer(client, monkeypatch):
    import core.routes.chat as chat

    monkeypatch.setattr(chat, "run_react_loop", E.gen_from([
        E.tool_execution("search_knowledge", {"query": "fees"}),
        {**SEARCH, "delegate_from": "agent_main", "delegate_agent_name": "Researcher"},
        E.tool_result("search_knowledge", "1 passage: [K1] contracts/a.pdf"),
        READ,
        CITATIONS,
        E.final("Fees exclude VAT [K1].", intent="chat"),
    ]))
    resp = await client.post("/chat/stream", json={"message": "fees?"})
    assert resp.status_code == 200
    events = data_events(resp.text.split("\n\n"))
    kinds = [e["type"] for e in events]
    assert kinds == ["tool_execution", "knowledge_search", "tool_result", "knowledge_read", "knowledge_citations",
                     "response", "done"]
    search = events[1]
    assert search["labels"] == ["[K1]"] and search["delegate_agent_name"] == "Researcher"
    assert events[4]["citations"] == CITATIONS["citations"] and events[4]["unresolved"] == ["[K9]"]
