"""
Agent memory (core/agent_memory.py) — the design is retrieval and eviction.

What must be true (each settled before the code existed):

  * eager is the index only — MEMORY.md rides a memory-enabled agent's system
    prompt and refreshes after writes; bodies come through read_memory;
  * search goes through the MemoryIndex seam — lexical over the markdown by
    default, replaceable by a vector backend without the engine changing;
  * memory does not only grow: index overflow archives least-recently-touched
    lines (searchable, no longer eager) and sessions hard-cap with the oldest
    deleted;
  * sessions leave a trace without the model remembering to write one — chat
    turns append a cheap note, an orchestration run's end writes a summary;
  * the semantic response cache routes through its backend seam (T1's engine
    half): a registered backend is used, none means the local behaviour.
"""
import json
import types

import pytest

from _fakes import seed as S

from core import agent_memory as am


def _call(tool: str, **args) -> str:
    # Not the shared tool_call() helper: its first positional is the tool
    # name, which collides with the memory tools' own `name` argument.
    return json.dumps({"tool": tool, "arguments": args})


@pytest.fixture(autouse=True)
def _fresh_index():
    am.set_memory_index(None)
    yield
    am.set_memory_index(None)


# ── storage, index, eviction ─────────────────────────────────────────────────

class TestStorageAndIndex:
    async def test_write_read_delete_roundtrip(self):
        name, err = await am.write_memory("agent_a", "Gold Prices", "# Gold\nTrack XAU in USD.")
        assert err is None and name == "gold-prices"
        assert "Track XAU in USD" in am.read_memory("agent_a", "gold-prices")
        assert "- [gold-prices] Gold" in am.read_index("agent_a")

        assert await am.delete_memory("agent_a", "gold-prices") is True
        assert am.read_memory("agent_a", "gold-prices") is None
        assert "gold-prices" not in am.read_index("agent_a")
        assert await am.delete_memory("agent_a", "gold-prices") is False

    async def test_update_moves_index_line_to_end(self):
        await am.write_memory("agent_b", "first", "one")
        await am.write_memory("agent_b", "second", "two")
        await am.write_memory("agent_b", "first", "one, updated")
        lines = [ln for ln in am.read_index("agent_b").splitlines() if ln.strip()]
        assert am._line_name(lines[-1]) == "first"   # touched last, evicted last
        assert len(lines) == 2

    async def test_rejections(self):
        _, err = await am.write_memory("agent_c", "", "content")
        assert "names" in err
        _, err = await am.write_memory("agent_c", "ok-name", "   ")
        assert "content" in err

    async def test_index_overflow_archives_oldest(self, monkeypatch):
        monkeypatch.setattr(am, "MAX_INDEX_LINES", 3)
        for i in range(5):
            await am.write_memory("agent_d", f"mem-{i}", f"content number {i}")
        lines = [ln for ln in am.read_index("agent_d").splitlines() if ln.strip()]
        assert [am._line_name(ln) for ln in lines] == ["mem-2", "mem-3", "mem-4"]
        archive = am._store().get(am._archive_key("agent_d"))
        assert "mem-0" in archive and "mem-1" in archive
        # Archived memories keep their files — searchable, just not eager.
        assert am.read_memory("agent_d", "mem-0") is not None


class TestSessions:
    async def test_notes_and_summary(self):
        am.append_session_note("agent_e", "sess_1", "- did a thing")
        am.append_session_note("agent_e", "sess_1", "- did another")
        content = am._store().get(am._session_key("agent_e", "sess_1"))
        assert content.count("- did") == 2 and "session: sess_1" in content

        await am.write_session_summary("agent_e", "sess_1", "The whole story.", run_id="run_9")
        summary = am._store().get(am._session_key("agent_e", "run_9"))
        assert "run_id: run_9" in summary and "The whole story." in summary

    async def test_session_eviction_deletes_oldest(self, monkeypatch):
        monkeypatch.setattr(am, "MAX_SESSIONS", 3)
        for i in range(5):
            await am.write_session_summary("agent_f", f"s{i}", f"summary {i}")
        keys = am._list_session_keys("agent_f")
        assert len(keys) <= 3
        assert not any(k.endswith("/s0.md") for k in keys)


class TestLexicalSearch:
    async def test_search_ranks_and_labels_kinds(self):
        await am.write_memory("agent_g", "gold-report", "gold gold gold prices weekly")
        await am.write_memory("agent_g", "moon-notes", "the moon is not gold")
        await am.write_session_summary("agent_g", "sess_x", "we researched gold prices")
        hits = await am.search_memory("agent_g", "gold prices")
        assert hits and hits[0]["name"] == "gold-report"      # highest overlap first
        kinds = {h["kind"] for h in hits}
        assert "item" in kinds and "session" in kinds
        assert await am.search_memory("agent_g", "zzz-nothing-matches") == []

    async def test_broken_backend_falls_back_to_lexical(self):
        class Exploding:
            async def upsert(self, *a, **k): ...
            async def delete(self, *a, **k): ...
            async def search(self, *a, **k):
                raise RuntimeError("backend down")

        await am.write_memory("agent_h", "resilience", "fallback keeps search working")
        am.set_memory_index(Exploding())
        hits = await am.search_memory("agent_h", "fallback search")
        assert hits and hits[0]["name"] == "resilience"


# ── engine flow ──────────────────────────────────────────────────────────────

def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _events(fake_llm, script, agent, **kw):
    from core.react_engine import run_agent_step

    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="do the work", agent_id=agent["id"], session_id="s_mem",
        server_module=_server(), agent_override=agent, max_turns=6, **kw,
    )]


class TestEngineFlow:
    async def test_remember_search_read_forget(self, fake_llm):
        agent = S.make_agent(tools=[], memory=True)
        events = await _events(fake_llm, [
            _call("remember", name="fav-index", content="User tracks the NIFTY 50."),
            _call("search_memory", query="NIFTY tracked"),
            _call("read_memory", name="fav-index"),
            _call("forget_memory", name="fav-index"),
            "done",
        ], agent)
        previews = [e.get("preview", "") for e in events if e.get("type") == "tool_result"]
        assert any("Saved memory 'fav-index'" in p for p in previews)
        assert any("match" in p for p in previews)
        assert any(p.startswith("Read memory") for p in previews)
        assert any("Memory deleted" in p for p in previews)
        # The index refreshed in place: after remember, before forget, the
        # system prompt carried the line.
        assert any("fav-index" in str(c) for c in fake_llm.calls[1:4])

    async def test_memory_disabled_agent_gets_neither_tools_nor_block(self, fake_llm):
        agent = S.make_agent(tools=[])
        await _events(fake_llm, ["just the answer"], agent)
        first = str(fake_llm.calls[0])
        assert "### MEMORY" not in first
        assert '"remember"' not in first

    async def test_chat_turn_leaves_a_session_note(self, fake_llm):
        agent = S.make_agent(tools=[], memory=True)
        await _events(fake_llm, ["all done"], agent, source="chat")
        note = am._store().get(am._session_key(agent["id"], am._session_stem("s_mem")))
        assert note is not None and "do the work" in note


class TestRunEndSummary:
    async def test_orchestration_run_writes_summaries_for_memory_agents(self, fake_llm, seed_agent):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        agent = await seed_agent(tools=[], memory=True)
        orch = S.make_orchestration(
            id="orch_mem", entry_step_id="s1",
            steps=[{"id": "s1", "name": "Work", "type": "agent",
                    "agent_id": agent["id"], "next_step_id": None}],
        )
        fake_llm.set_default("step done")
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [ev async for ev in engine.run("go", run_id="run_mem_1")]
        assert any(e.get("type") == "orchestration_complete" for e in events)

        summary = am._store().get(am._session_key(agent["id"], am._session_stem("run_mem_1")))
        assert summary is not None
        assert "run_id: run_mem_1" in summary and "Work" in summary


# ── response-cache seam ──────────────────────────────────────────────────────

class TestResponseCacheSeam:
    async def test_registered_backend_is_routed_to(self):
        from core.cache import response_cache as rc

        calls = []

        class Backend:
            async def get(self, step_id, model, system, user_message, threshold):
                calls.append(("get", step_id))
                return {"text": "from backend", "input_tokens": 0, "output_tokens": 0}

            async def set(self, step_id, model, system, user_message, **kw):
                calls.append(("set", step_id))

        rc.set_response_cache_backend(Backend())
        try:
            hit = await rc.get_semantic_routed("step_1", "m", "sys", "hello")
            assert hit["text"] == "from backend"
            await rc.set_semantic_routed("step_1", "m", "sys", "hello",
                                         text="t", input_tokens=1, output_tokens=2)
            assert calls == [("get", "step_1"), ("set", "step_1")]
        finally:
            rc.set_response_cache_backend(None)

    async def test_no_backend_means_local_behaviour(self):
        from core.cache import response_cache as rc

        rc.set_response_cache_backend(None)
        # No memory store attached in unit tests → local semantic cache is a
        # no-op that returns None rather than raising.
        assert await rc.get_semantic_routed("step_2", "m", "sys", "hello") is None
        await rc.set_semantic_routed("step_2", "m", "sys", "hello",
                                     text="t", input_tokens=1, output_tokens=2)
