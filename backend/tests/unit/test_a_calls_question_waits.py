"""A phone call's parked question waits while the caller talks on (F486, VOX1f, D273).

A workflow agent's call turns run on the fleet (D251). Its chat job settles a question the conversation was
waiting on as soon as a new turn starts (F247's rule for chat: a new message moves past it), so a caller's next
words after "I've asked someone on the team to approve that" expired the question in the bell before anyone
could answer it, and F474's answer on the call never came. A plain agent's call turns keep it (the call itself
allows one waiting question at a time, V16). The call's turns now keep it on the fleet too (`keep_parked`): a new
turn leaves it waiting, and a second question never takes the first one's place.
"""
from core.store import sessions as store

S, A = "voice_chn_f486_CA" + "6" * 32, "agent_f486"
WAITING = {"approval_id": "ap_1", "tool_name": "refund", "tool_args": {"order": "A1"}, "message": "refund A1",
           "api_turn": {"door": "voice"}}


def _fleet(monkeypatch, events):
    import core.react_engine as re_mod
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

    async def loop(request, server_module, should_stop=None, **_kw):
        for event in events:
            yield event

    async def nothing(*_a, **_kw):
        return None

    async def got(*_a, **_kw):
        return object()
    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(mcp_pool, "acquire", got)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return worker_mod


async def _turn(worker_mod, message, **kwargs):
    import fakeredis.aioredis

    from core.store.engine import get_store
    return await worker_mod.run_agent_chat_job(
        {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": await get_store(), "job_try": 1,
         "job_id": "f486"}, S, A, message, **kwargs)


async def test_a_calls_next_turn_leaves_its_question_waiting(monkeypatch):
    await store.append_turn(S, A, [{"role": "user", "content": "refund A1"},
                                   {"role": "assistant", "content": "I've asked someone on the team."}])
    await store.set_pending_approval(S, A, dict(WAITING))
    worker_mod = _fleet(monkeypatch, [{"type": "final", "response": "We open at nine."}])
    await _turn(worker_mod, "when do you open?", keep_parked=True)
    assert (await store.get_pending_approval(S, A) or {}).get("approval_id") == "ap_1", "still waiting"


async def test_a_chats_next_turn_still_moves_past_it(monkeypatch):
    """F247 unchanged for every other door: a new message refuses the waiting call."""
    await store.append_turn(S, A, [{"role": "user", "content": "refund A1"},
                                   {"role": "assistant", "content": "Waiting."}])
    await store.set_pending_approval(S, A, dict(WAITING))
    worker_mod = _fleet(monkeypatch, [{"type": "final", "response": "We open at nine."}])
    await _turn(worker_mod, "when do you open?")
    assert await store.get_pending_approval(S, A) is None


async def test_a_second_question_never_takes_the_first_ones_place(monkeypatch):
    await store.append_turn(S, A, [{"role": "user", "content": "refund A1"},
                                   {"role": "assistant", "content": "I've asked someone on the team."}])
    await store.set_pending_approval(S, A, dict(WAITING))
    ask = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "refund",
           "tool_args": {"order": "B2"}, "approval_id": "ap_2"}
    worker_mod = _fleet(monkeypatch, [{"type": "_step_progress", "turn": 1}, ask])
    result = await _turn(worker_mod, "and refund B2", keep_parked=True)
    assert result["status"] == "paused"
    assert (await store.get_pending_approval(S, A) or {}).get("approval_id") == "ap_1", "the first still waits"
