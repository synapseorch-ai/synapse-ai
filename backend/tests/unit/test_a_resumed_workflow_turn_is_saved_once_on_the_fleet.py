"""A workflow agent's turn resumed on the fleet is saved once, with its calls (F486, VOX1f, D273).

VOX1f's call 7 (`CA5b4497…`, a one-step workflow agent on a phone line): each approved answer was in the
conversation twice. The run was resumed by the fleet's chat job, and two writers saved it: the engine, where a
resumed run ends (F261's owed turn, a bare question and answer), and the job, which F475 made the turn's one writer
(`save_here=False`). The resume did not honour `save_here`. Now the engine takes the owed turn off the run without
saving it when its caller saves, and the job's record — the calls with their results, which the next turn's history
needs (F477) — is the one kept. An answer given anywhere else (in-process chat, the run's page) saves as before.
"""
import fakeredis.aioredis

from _fakes import seed as S
from _fakes.fake_llm import tool_call

SID = "s_f486_resume"
WORKER = "agent_f486_worker"


async def _planner():
    orch = S.make_orchestration(id="orch_f486r", entry_step_id="work", steps=[
        {"id": "work", "name": "Do the work", "type": "agent", "agent_id": WORKER, "prompt_template": "go",
         "output_key": "result", "max_turns": 4, "next_step_id": None}])
    await S.seed_orchestrations([orch])
    planner = S.make_orchestrator_agent("orch_f486r", id="agent_orch_f486r", name="Harbor line flow")
    await S.seed_agents([planner, S.make_agent(id=WORKER, tools=["all"], skip_default_tools=True,
                                               tool_policy={"undeclared": "confirm"})])
    return planner


def _fleet(monkeypatch):
    import core.react_engine as re_mod
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

    real = re_mod.run_agent_step

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == WORKER:
            async def executor(name, args):
                return f"ran-{name}"
            kwargs.setdefault("tools_override", [{"type": "function", "function": {
                "name": "danger", "description": "d", "parameters": {"type": "object", "properties": {}}}}])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    async def nothing(*_a, **_kw):
        return None

    async def got(*_a, **_kw):
        import types
        return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})
    monkeypatch.setattr(re_mod, "run_agent_step", wrapped)
    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(mcp_pool, "acquire", got)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return worker_mod


async def _job(worker_mod, agent_id, message, **kwargs):
    from core.store.engine import get_store
    return await worker_mod.run_agent_chat_job(
        {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": await get_store(), "job_try": 1,
         "job_id": "f486r"}, SID, agent_id, message, **kwargs)


async def test_an_answer_resumed_on_the_fleet_is_saved_once_with_its_calls(monkeypatch, fake_llm):
    from core.chat_approval import resume_state_for
    from core.store import sessions as store

    planner = await _planner()
    worker_mod = _fleet(monkeypatch)
    fake_llm.script([tool_call("danger"), "worker: done"])

    first = await _job(worker_mod, planner["id"], "clean up")
    assert first["status"] == "paused"
    pending = await store.get_pending_approval(SID, planner["id"])
    assert pending, "the question waits"

    second = await _job(worker_mod, planner["id"], "clean up",
                        resume_state=resume_state_for(pending, approve=True))
    assert second["status"] == "completed", second

    rows = {r["agent_id"]: r.get("messages") or [] for r in await store.load() if r["session_id"] == SID}
    pairs = [(m["role"], m["content"]) for m in rows[planner["id"]]]
    assert pairs == [("user", "clean up"), ("assistant", "worker: done")], pairs
