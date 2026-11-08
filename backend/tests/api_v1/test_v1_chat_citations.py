"""An answer's `[K#]` labels resolve over v1 (P1a, D176, Naveen's call 3).

An agent with knowledge bases answers with labels, and the engine resolves them (`knowledge_citations`,
just before `final`). v1 dropped that event, so an API caller got labels it could not resolve. Now the
stream passes the turn's three Knowledge frames, and the sync answer carries `citations` and
`unresolved_citations` on every return path (`[]` when there are none) — additive, as v1 grows.
A delegate's frames stay out: its labels are its own turn's, not the answer's.
"""
from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events

CITATION = {
    "label": "[K1]", "kb_id": "kb_000000000001", "doc_id": "kdoc_1", "chunk_id": "kch_1", "path": "kb/acme.md",
    "source": "upload", "source_url": None, "title": "Acme", "page_start": 2, "page_end": 2, "char_start": 10,
    "char_end": 80, "heading_path": ["Fees"], "snippet": "Hosting is billed monthly.", "sha256": "ab" * 32,
    "doc_version": 1, "indexed_at": "2026-10-02T00:00:00+00:00", "snapshot_id": "ksnap_1",
    "quote_check": {"quote": "billed monthly", "matched": True},
}
SEARCH = {"type": "knowledge_search", "turn": 1, "query_id": "kq_1", "kb_ids": ["kb_000000000001"],
          "query": "hosting fee", "filtered": False, "hit_count": 2, "labels": ["[K1]", "[K2]"], "degraded": [],
          "cost_usd": 0.0, "latency_ms": 4}
READ = {"type": "knowledge_read", "turn": 2, "label": "[K1]", "chunk_id": "kch_1", "doc_id": "kdoc_1",
        "path": "kb/acme.md", "window": 1}
CITED = {"type": "knowledge_citations", "citations": [CITATION], "unresolved": ["[K7]"]}
DELEGATE = {"delegate_from": "agent_x", "delegate_agent_name": "Researcher"}
ANSWER = "Hosting is billed monthly [K1]."


def _sse(text: str) -> list[dict]:
    return data_events(text.split("\n\n"))


def _engine(monkeypatch, events):
    import core.react_engine as re
    monkeypatch.setattr(re, "run_react_loop", E.gen_from(events))


class TestStream:
    async def test_the_turns_knowledge_frames_come_before_its_answer(self, client, api_key, seed_agent,
                                                                     monkeypatch):
        await seed_agent()
        _engine(monkeypatch, [
            {**SEARCH, "turn": 1, "query_id": "kq_delegate", **DELEGATE},
            {**CITED, "citations": [], "unresolved": ["[K9]"], **DELEGATE},
            E.tool_execution("search_knowledge", {"query": "hosting fee"}), SEARCH, READ, CITED, E.final(ANSWER)])
        resp = await client.post("/api/v1/chat/stream", json={"message": "fees?"}, headers=api_key["headers"])
        events = _sse(resp.text)
        kinds = [e["type"] for e in events]
        assert kinds == ["session", "tool_execution", "knowledge_search", "knowledge_read", "knowledge_citations",
                         "response", "done"]
        search, read, cited = events[2:5]
        assert search == SEARCH
        assert read == READ
        assert cited == {"type": "knowledge_citations", "citations": [CITATION], "unresolved_citations": ["[K7]"]}

    async def test_only_the_contract_s_fields_pass(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _engine(monkeypatch, [{**SEARCH, "secret": "x"}, {**READ, "text": "the whole passage"}, E.final("ok")])
        events = _sse((await client.post("/api/v1/chat/stream", json={"message": "q"},
                                         headers=api_key["headers"])).text)
        assert events[1] == SEARCH and events[2] == READ


class TestSync:
    async def test_an_answer_carries_its_citations(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _engine(monkeypatch, [{**CITED, "citations": [], **DELEGATE}, SEARCH, READ, CITED, E.final(ANSWER)])
        body = (await client.post("/api/v1/chat", json={"message": "fees?"}, headers=api_key["headers"])).json()
        assert (body["status"], body["response"]) == ("completed", ANSWER)
        assert body["citations"] == [CITATION] and body["unresolved_citations"] == ["[K7]"]

    async def test_an_answer_without_knowledge_has_none(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _engine(monkeypatch, [E.final("hello")])
        body = (await client.post("/api/v1/chat", json={"message": "hi"}, headers=api_key["headers"])).json()
        assert body["citations"] == [] and body["unresolved_citations"] == []

    async def test_a_pause_carries_the_fields_too(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _engine(monkeypatch, [{"type": "human_input_required", "orch_step_id": "h", "prompt": "Ship it?",
                               "fields": [], "run_id": "run_orch_1"}])
        body = (await client.post("/api/v1/chat", json={"message": "go"}, headers=api_key["headers"])).json()
        assert body["status"] == "paused"
        assert body["citations"] == [] and body["unresolved_citations"] == []
