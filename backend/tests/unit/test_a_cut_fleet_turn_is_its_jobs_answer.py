"""A fleet turn the caller cut is one record: the job's answer, marked as heard (F486, VOX1f, D273).

The fleet's chat job saves a workflow agent's turn whatever the call does (V14), so the call no longer saves a cut
copy (`VoiceHost.cut_turn`). The job stamps its answer with the turn's `chat_turn`, and the host marks exactly that
answer interrupted at what the caller heard, once the job has saved it — never another turn's.
"""
import fakeredis.aioredis

from core.store import sessions as store

SID, AID = "voice_chn_f486c_CA" + "7" * 32, "agent_f486c"


async def test_the_answer_with_that_turn_is_cut_and_noted_and_no_other():
    await store.append_turn(SID, AID, [{"role": "user", "content": "weather?"},
                                       {"role": "assistant", "content": "It is sunny in Delhi. Anything else?",
                                        "chat_turn": "v:CA7:3"}])
    await store.append_turn(SID, AID, [{"role": "user", "content": "and the price?"},
                                       {"role": "assistant", "content": "It costs $19.99.", "chat_turn": "v:CA7:4"}])
    assert await store.cut_answer(SID, AID, chat_turn="v:CA7:3", heard="It is sunny in Delhi.",
                                  answered_approval={"id": "ap_1", "approved": True})
    messages = (await store.get(SID, AID))["messages"]
    assert messages[1]["content"] == "It is sunny in Delhi." and messages[1]["interrupted"] is True
    assert messages[1]["answered_approval"] == {"id": "ap_1", "approved": True}
    assert messages[3] == {"role": "assistant", "content": "It costs $19.99.", "chat_turn": "v:CA7:4"}
    assert not await store.cut_answer(SID, AID, chat_turn="v:CA7:9", heard="")


async def test_the_fleet_job_stamps_its_answer_with_its_turn(monkeypatch):
    import core.react_engine as re_mod
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod
    from core.store.engine import get_store

    async def loop(request, server_module, should_stop=None, **_kw):
        yield {"type": "final", "response": "We open at nine."}

    async def nothing(*_a, **_kw):
        return None

    async def got(*_a, **_kw):
        return object()
    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(mcp_pool, "acquire", got)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    await worker_mod.run_agent_chat_job(
        {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": await get_store(), "job_try": 1,
         "job_id": "f486c"}, SID, AID, "when do you open?", chat_turn="v:CA7:5")
    answer = (await store.get(SID, AID))["messages"][-1]
    assert answer["content"] == "We open at nine." and answer["chat_turn"] == "v:CA7:5"
