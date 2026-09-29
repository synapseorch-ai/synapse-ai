"""A saved chat turn keeps its thinking and its reasoning, so a reopened chat shows them (U60).

The stress pass reopened 14 turns and none had its THINKING or REASONING
panel: the data is in the stream (`llm_reasoning`, `llm_thought`) and no save
path kept it. SP7 restored the tools a turn used (`calls`); this is the rest.

The saved assistant entry gains `reasoning` and `thoughts` — lists of text in
turn order, as the chat page builds them live — written only when there is
some, the newest 20 of each, each at most 4,000 characters. They are for a
person reading the conversation back, never history sent to the model.
"""
from types import SimpleNamespace

from _fakes import seed as S
from _fakes.fake_llm import tool_call

LOOKUP = {"type": "function", "function": {"name": "lookup", "description": "look",
                                           "parameters": {"type": "object", "properties": {}}}}


def _server():
    return SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


async def _run(fake_llm, session_id, script, **kwargs):
    from core.react_engine import run_agent_step
    agent = kwargs.pop("agent", None) or S.make_agent(tools=["all"], skip_default_tools=True)

    async def executor(name, args):
        return "found it"

    fake_llm.script(script)
    events = [e async for e in run_agent_step(
        message="look it up", agent_id=agent["id"], session_id=session_id, server_module=_server(),
        agent_override=agent, tools_override=[LOOKUP], tool_executor=executor, **kwargs)]
    return agent, events


class TestTheEngineSavesThem:
    async def test_a_finished_turn_keeps_both_in_order(self, fake_llm):
        from core.session import _get_conversation_history
        agent, _ = await _run(fake_llm, "s_u60a", [
            "[REASONING]I should look it up.[/REASONING]" + tool_call("lookup"),
            "[REASONING]The tool found it.[/REASONING]It is found.",
        ])
        [turn] = await _get_conversation_history("s_u60a", agent["id"])
        assert turn["reasoning"] == ["I should look it up.", "The tool found it."]
        assert len(turn["thoughts"]) == 2
        assert '"lookup"' in turn["thoughts"][0]
        assert turn["thoughts"][1] == "It is found."

    async def test_a_turn_with_none_keeps_the_old_shape(self, fake_llm):
        from core.store import sessions as store
        agent, _ = await _run(fake_llm, "s_u60b", ["Hello."])
        rows = [r for r in await store.load() if r.get("session_id") == "s_u60b"]
        answer = rows[0]["messages"][-1]
        # `llm_thought` is the answer itself here — one thought, kept.
        assert "reasoning" not in answer
        assert answer.get("thoughts") == ["Hello."]

    async def test_each_is_bounded(self, fake_llm):
        from core.session import _get_conversation_history
        long = "x" * 10_000
        agent, _ = await _run(fake_llm, "s_u60c", [f"[REASONING]{long}[/REASONING]Done."])
        [turn] = await _get_conversation_history("s_u60c", agent["id"])
        kept = turn["reasoning"][0]
        assert kept.startswith("x" * 4_000) and "more characters" in kept and len(kept) < 4_100

    async def test_a_failed_turn_keeps_what_it_thought(self, fake_llm):
        from core.chat_turns import FAILED_ANSWER
        from core.session import _get_conversation_history
        import pytest
        from core.llm_providers import LLMError
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        with pytest.raises(LLMError):
            await _run(fake_llm, "s_u60d", ["[REASONING]hmm[/REASONING]", "[REASONING]still[/REASONING]"],
                       agent=agent)
        [turn] = await _get_conversation_history("s_u60d", agent["id"])
        assert turn["assistant"] == FAILED_ANSWER
        assert turn["reasoning"] == ["hmm", "still"]

    async def test_a_resumed_turn_keeps_what_it_thought_before_the_pause(self, fake_llm):
        from core.session import _get_conversation_history
        agent, _ = await _run(fake_llm, "s_u60e", ["[REASONING]After.[/REASONING]Done."], resume_state={
            "turn": 1, "context_text": "User Request: look it up\n",
            "reasoning": ["Before the pause."], "thoughts": ['{"tool": "lookup", "arguments": {}}'],
        })
        [turn] = await _get_conversation_history("s_u60e", agent["id"])
        assert turn["reasoning"] == ["Before the pause.", "After."]
        assert turn["thoughts"][0].startswith('{"tool"')

    async def test_the_snapshot_carries_them_for_a_pause(self, fake_llm):
        _agent, events = await _run(fake_llm, "s_u60f", [
            "[REASONING]First.[/REASONING]" + tool_call("lookup"), "Done."], progress_events=True)
        [snap] = [e for e in events if e.get("type") == "_step_progress"]
        assert snap["reasoning"] == ["First."]
        assert '"lookup"' in snap["thoughts"][0]

    async def test_they_are_never_sent_to_the_model(self, fake_llm):
        from core.session import get_recent_history_messages
        agent, _ = await _run(fake_llm, "s_u60g", ["[REASONING]SECRET PLAN[/REASONING]Hi."])
        messages = await get_recent_history_messages("s_u60g", agent["id"], tool_digest=True)
        assert "SECRET PLAN" not in str(messages)


class TestTheHistoryRoute:
    async def test_a_reopened_turn_carries_them(self, client, fake_llm):
        agent = S.make_agent(id="agent_u60_h", tools=["all"], skip_default_tools=True)
        await _run(fake_llm, "s_u60h", ["[REASONING]Think.[/REASONING]Answer."], agent=agent)
        resp = await client.get("/api/sessions/s_u60h/history", params={"agent_id": agent["id"]})
        [turn] = resp.json()["turns"]
        assert turn["reasoning"] == ["Think."] and turn["thoughts"] == ["Answer."]


class TestTheFleet:
    async def test_its_saved_turn_keeps_them_and_not_a_delegates(self, monkeypatch):
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
            yield {"type": "llm_reasoning", "reasoning": "Mine.", "turn": 1}
            yield {"type": "llm_reasoning", "reasoning": "The delegate's.", "turn": 1,
                   "delegate_from": "lead"}
            yield {"type": "llm_thought", "thought": "A step's.", "turn": 1, "orch_step_id": "s1"}
            yield {"type": "llm_thought", "thought": "Done.", "turn": 1}
            yield {"type": "final", "response": "Done."}

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
            "u60-fleet", "agent_x", "go", resume_state={"turn": 1, "context_text": "x",
                                                        "reasoning": ["Before."]})
        answer = upserts[-1][1][-1]
        assert answer["reasoning"] == ["Before.", "Mine."]
        assert answer["thoughts"] == ["Done."]
