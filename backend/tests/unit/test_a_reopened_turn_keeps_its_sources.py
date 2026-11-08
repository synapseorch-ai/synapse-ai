"""A chat turn saved on the fleet keeps its answer's Sources, as the in-process save does (F333).

B1b saves the root turn's `knowledge_citations` on the assistant entry
(`citations`, `unresolved_citations`), so a reopened answer draws its Sources
again. The fleet's chat job (`core/scale/worker.py::run_agent_chat_job`)
rewrites the session's messages at the end of every turn from what it built
itself — content, calls, reasoning, thoughts — and the Sources were not among
them: cloud, and OSS in scale mode, lost them on every turn.

The job keeps the root turn's event (never a delegate's or a workflow step's,
whose labels are another turn's), and never for a visitor, as in-process.
"""
from __future__ import annotations

CITATION = {"label": "K1", "kb_id": "kb_1", "doc_id": "kdoc_1", "path": "contracts/acme.md",
            "quote": "Either party may terminate", "quote_check": {"quote": "Either party may terminate",
                                                                  "matched": True, "method": "exact"}}


async def _run_on_the_fleet(monkeypatch, events, **job_kwargs):
    """Run `run_agent_chat_job` over a scripted engine; the messages it saved."""
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod
    import core.react_engine as re_mod
    upserts = []

    async def history(*_a):
        return []

    async def upsert(_sf, _sid, _aid, status, messages):
        upserts.append((status, [dict(m) for m in messages]))

    async def nothing(*_a, **_kw):
        return None

    async def acquire(*_a, **_kw):
        return object()

    async def loop(request, server_module, should_stop=None, **_kw):
        for event in events:
            yield event

    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(worker_mod, "_load_chat_history", history)
    monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

    import fakeredis.aioredis
    await worker_mod.run_agent_chat_job(
        {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": None, "job_try": 1, "job_id": "j"},
        "f333-fleet", "agent_x", "When can either side end it?", **job_kwargs)
    assert upserts, "the job saved nothing"
    return upserts[-1]


class TestTheFleetKeepsThem:
    async def test_the_answers_sources_are_saved(self, monkeypatch):
        status, messages = await _run_on_the_fleet(monkeypatch, [
            {"type": "knowledge_citations", "citations": [CITATION], "unresolved": ["K9"]},
            {"type": "final", "response": "Either party may end it on notice [K1]."},
        ])
        answer = messages[-1]
        assert status == "completed"
        assert answer["role"] == "assistant"
        assert answer["citations"] == [CITATION]
        assert answer["unresolved_citations"] == ["K9"]

    async def test_a_turn_without_sources_keeps_the_old_shape(self, monkeypatch):
        _status, messages = await _run_on_the_fleet(monkeypatch, [{"type": "final", "response": "Hi."}])
        assert "citations" not in messages[-1]
        assert "unresolved_citations" not in messages[-1]

    async def test_an_empty_event_saves_nothing(self, monkeypatch):
        _status, messages = await _run_on_the_fleet(monkeypatch, [
            {"type": "knowledge_citations", "citations": [], "unresolved": []},
            {"type": "final", "response": "Nothing to cite."},
        ])
        assert "citations" not in messages[-1]

    async def test_a_delegates_or_a_steps_sources_are_not_the_answers(self, monkeypatch):
        _status, messages = await _run_on_the_fleet(monkeypatch, [
            {"type": "knowledge_citations", "citations": [CITATION], "unresolved": [], "delegate_from": "lead"},
            {"type": "knowledge_citations", "citations": [CITATION], "unresolved": [], "orch_step_id": "s1"},
            {"type": "final", "response": "Done."},
        ])
        assert "citations" not in messages[-1]

    async def test_a_visitors_turn_saves_none(self, monkeypatch):
        """In-process the Sources are never saved for a visitor (they name the owner's files);
        the fleet is the same."""
        from core.visitor import VisitorScope

        scope = VisitorScope(surface="widget", tools=frozenset({"search_knowledge"}))
        _status, messages = await _run_on_the_fleet(monkeypatch, [
            {"type": "knowledge_citations", "citations": [CITATION], "unresolved": []},
            {"type": "final", "response": "On notice."},
        ], visitor=scope.to_dict())
        assert "citations" not in messages[-1]


class TestOneRuleForBothSaves:
    def test_the_in_process_save_and_the_fleet_share_it(self):
        from core.session import citation_fields

        assert citation_fields(None) == {}
        assert citation_fields({"citations": [], "unresolved": []}) == {}
        assert citation_fields({"citations": [CITATION]}) == {"citations": [CITATION]}
        assert citation_fields({"citations": [], "unresolved": ["K2"]}) == {
            "citations": [], "unresolved_citations": ["K2"]}
