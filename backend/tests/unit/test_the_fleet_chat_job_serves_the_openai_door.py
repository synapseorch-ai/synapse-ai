"""The fleet's chat job, as the OpenAI-compatible endpoint drives it in cloud (OAI1).

Cloud runs every chat turn on the fleet. A turn from the OpenAI door carries
its own conversation (`history_override`, `caller_system`), keeps no Chat row
(`save_turn=False`), names itself in the agent's log (`log_source`) and wants
its tokens (`report_usage`): one `turn_usage` event before `done`.
"""
import json


def _patch(monkeypatch, loop):
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod
    import core.react_engine as re_mod

    rows = []

    async def nothing(*_a, **_kw):
        return None

    async def history(*_a):
        return []

    async def acquire(*_a, **_kw):
        return object()

    async def upsert(*args, **_kw):
        rows.append(args)

    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(worker_mod, "_load_chat_history", history)
    monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return worker_mod, rows


async def _events(redis, session_id):
    entries = await redis.xrange(f"synapse:chat:{session_id}:events")
    return [json.loads(fields[b"data"]) for _id, fields in entries]


async def test_the_doors_kwargs_reach_the_turn_and_no_row_is_written(monkeypatch):
    seen = {}

    async def loop(request, server_module, should_stop=None, **kw):
        from core import turn_usage
        seen.update(kw)
        turn_usage.add(60, 10, 40, 0)
        turn_usage.add(30, 5, 0, 0)
        yield {"type": "answer_delta", "text": "Hi", "turn": 1}
        yield {"type": "final", "response": "Hi", "streamed": True}

    worker_mod, rows = _patch(monkeypatch, loop)
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis()
    ctx = {"redis": redis, "session_factory": None, "job_try": 1, "job_id": "j1"}
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"}]
    result = await worker_mod.run_agent_chat_job(
        ctx, "oai_f1", "agent_x", "now", history_override=history, caller_system="Be brief.",
        save_turn=False, log_source="openai", report_usage=True)

    assert result["status"] == "completed"
    assert seen["history_override"] == history and seen["caller_system"] == "Be brief."
    assert seen["save_turn"] is False and seen["log_source"] == "openai" and seen["stream_answer"] is True
    assert rows == []   # no Chat row, start to finish
    events = await _events(redis, "oai_f1")
    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["turn_usage", "done"]
    usage = events[-2]
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"]) == (130, 15, 40)


async def test_a_member_turn_is_unchanged_but_streams(monkeypatch):
    seen = {}

    async def loop(request, server_module, should_stop=None, **kw):
        seen.update(kw)
        yield {"type": "final", "response": "ok"}

    worker_mod, rows = _patch(monkeypatch, loop)
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis()
    ctx = {"redis": redis, "session_factory": None, "job_try": 1, "job_id": "j2"}
    await worker_mod.run_agent_chat_job(ctx, "chat_m1", "agent_x", "hi")
    assert seen["stream_answer"] is True and seen["save_turn"] is True and seen["history_override"] is None
    assert [r[3] for r in rows] == ["running", "completed"]
    assert "turn_usage" not in [e["type"] for e in await _events(redis, "chat_m1")]
