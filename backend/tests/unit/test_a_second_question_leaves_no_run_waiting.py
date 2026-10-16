"""A second question a call does not ask leaves no workflow run waiting for it (F644, VOX1f, D273).

A call keeps one question waiting at a time (F486, V16): while it waits, a second question a turn stops on is not
asked, and the call says so. A workflow agent's turn asks through its run, though, and that run stayed `paused`
for good — measured on dev 2026-10-16 (call CAf875ef…): `waiting_for_human` with no bell item and nothing that
could answer it, its slot held and a credit hold on it until the hold lapsed. Naveen: refused, as before, and its
run ends, settled, with nothing charged — the step that asked leaves no entry for a host to price.
"""
from core.store import sessions as store

S, A = "voice_chn_f644_CA" + "4" * 32, "agent_f644"
RUN = "run_f644"
WAITING = {"approval_id": "ap_1", "tool_name": "weather_today", "tool_args": {"city": "Mumbai"},
           "message": "weather in Mumbai", "api_turn": {"door": "voice"}}
ASK = {"type": "human_input_required", "kind": "tool_approval", "tool_name": "weather_today",
       "tool_args": {"city": "Mumbai"}, "approval_id": "ap_2", "run_id": RUN}


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
         "job_id": "f644"}, S, A, message, **kwargs)


async def _paused_run(run_id=RUN, **kw):
    """The turn's run as the engine leaves it at a tool approval: paused at the agent step that asked."""
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    asked = {"step_id": "answer", "step_name": "Answer", "step_type": "agent", "status": "paused",
             "started_at": "2026-10-15T19:03:37Z"}
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_f644", status="paused",
                                       current_step_id="answer", waiting_for_human=True,
                                       step_history=[asked], **kw)).checkpoint()


async def _run(run_id=RUN):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


async def _waiting_call():
    await store.append_turn(S, A, [{"role": "user", "content": "weather in Mumbai"},
                                   {"role": "assistant", "content": "I've asked someone on the team."}])
    await store.set_pending_approval(S, A, dict(WAITING))


async def test_the_run_of_a_question_the_call_did_not_ask_ends(monkeypatch):
    from core.orchestration.run_error import run_error
    from core.scale.worker import NOT_ASKED

    await _waiting_call()
    await _paused_run()
    worker_mod = _fleet(monkeypatch, [{"type": "orchestration_start", "run_id": RUN}, ASK])
    result = await _turn(worker_mod, "and in Delhi?", keep_parked=True, workflow_run_id=RUN)

    assert result["status"] == "paused", "the turn itself is as before: the call says it can't ask"
    assert (await store.get_pending_approval(S, A) or {}).get("approval_id") == "ap_1", "the first still waits"
    run = await _run()
    assert run.status == "cancelled" and run.waiting_for_human is False
    assert run_error(run) == NOT_ASKED, "its page says why"
    assert not [e for e in run.step_history if e.get("status") == "paused"], "nothing left waiting"
    assert run.step_history == [], "the step that asked leaves no entry: nothing for a host to price"


async def test_the_first_question_s_run_still_waits(monkeypatch):
    """The question the call did ask is untouched: its run stays paused until the bell answers it."""
    await store.append_turn(S, A, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."}])
    await _paused_run()
    worker_mod = _fleet(monkeypatch, [{"type": "orchestration_start", "run_id": RUN}, ASK])
    result = await _turn(worker_mod, "weather in Mumbai", keep_parked=True, workflow_run_id=RUN)

    assert result["status"] == "paused"
    assert (await store.get_pending_approval(S, A) or {}).get("approval_id") == "ap_2", "this one is asked"
    run = await _run()
    assert run.status == "paused" and run.waiting_for_human is True
    assert [e["status"] for e in run.step_history] == ["paused"]


async def test_a_plain_agents_second_question_has_no_run_to_end(monkeypatch):
    """A plain agent's turn starts no run: the refusal is all there is, as before."""
    await _waiting_call()
    ask = {k: v for k, v in ASK.items() if k != "run_id"}
    worker_mod = _fleet(monkeypatch, [{"type": "_step_progress", "turn": 1}, ask])
    result = await _turn(worker_mod, "and in Delhi?", keep_parked=True)

    assert result["status"] == "paused"
    assert (await store.get_pending_approval(S, A) or {}).get("approval_id") == "ap_1"


async def test_a_run_the_turn_did_not_start_is_left_alone(monkeypatch):
    """Only the runs this turn started (`orchestration_start`, not nested): another conversation's paused run,
    or one this turn merely names, keeps waiting."""
    await _waiting_call()
    await _paused_run("run_other")
    ask = {**ASK, "run_id": "run_other"}
    worker_mod = _fleet(monkeypatch, [ask])
    await _turn(worker_mod, "and in Delhi?", keep_parked=True)

    run = await _run("run_other")
    assert run.status == "paused" and run.waiting_for_human is True
