"""F417: a workflow agent's chat turn can run its workflow under the enqueuer's id.

Cloud charged a workflow reached through chat — the app, the widget, a channel,
the chat API, an agent schedule — as a chat turn, by its minutes: the run the
engine minted for itself had no meter, so C2's ten quick steps cost 10 credits
as a run and 1 through chat. Cloud now admits that run before the turn is
queued (its row, its meter and its hold) and hands the fleet's chat job the
id; the job passes it to the run. Unset, the run mints its own id, as before.
"""
from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import _print
from tests.unit.test_a_workflow_agent_sees_what_chat_sends import _server


async def _workflow_agent(orch_id):
    orch = S.make_orchestration(id=orch_id, entry_step_id="p", steps=[_print("p", "done", key="out")])
    await S.seed_orchestrations([orch])
    agent = S.make_orchestrator_agent(orch_id, id=f"agent_{orch_id}")
    await S.seed_agents([agent])
    return agent


async def _record(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


class TestTheRunsId:
    async def test_given_one_the_run_takes_it(self):
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        agent = await _workflow_agent("orch_f417a")
        request = ChatRequest(message="go", session_id="sess_f417a", agent_id=agent["id"])
        request._workflow_run_id = "run_orch_f417a_given"
        events = [e async for e in run_react_loop(request, _server())]

        started = [e["run_id"] for e in events if e.get("type") == "orchestration_start"]
        assert started == ["run_orch_f417a_given"], events
        run = await _record("run_orch_f417a_given")
        assert run.status == "completed" and run.shared_state["out"] == "done"

    async def test_without_one_it_mints_its_own(self):
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        agent = await _workflow_agent("orch_f417b")
        events = [e async for e in run_react_loop(
            ChatRequest(message="go", session_id="sess_f417b", agent_id=agent["id"]), _server())]
        [run_id] = [e["run_id"] for e in events if e.get("type") == "orchestration_start"]
        assert run_id.startswith("run_orch_f417b_")

    async def test_no_request_body_can_name_it(self):
        from core.models import ChatRequest

        request = ChatRequest.model_validate({"message": "hi", "_workflow_run_id": "run_someone_elses"})
        assert request._workflow_run_id is None


class TestTheFleetChatJob:
    async def test_it_hands_the_enqueuers_id_to_the_turn(self, monkeypatch):
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        import core.react_engine as re_mod

        seen = []

        async def nothing(*_a, **_kw):
            return None

        async def history(*_a):
            return []

        async def acquire(*_a, **_kw):
            return object()

        async def loop(request, server_module, should_stop=None, **_kw):
            seen.append(request._workflow_run_id)
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", nothing)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        import fakeredis.aioredis
        ctx = {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": None, "job_try": 1, "job_id": "j"}
        await worker_mod.run_agent_chat_job(ctx, "s_f417", "agent_x", "go", workflow_run_id="run_given")
        await worker_mod.run_agent_chat_job(ctx, "s_f417", "agent_x", "go")

        assert seen == ["run_given", None]
