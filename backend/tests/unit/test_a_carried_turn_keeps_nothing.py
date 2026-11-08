"""A turn the caller went on past before it answered keeps nothing (F643, VOX1f, D273).

When a caller keeps talking before the turn says anything, the call asks the next turn their whole question
(VOX1d: "Hi." then "…the weather in Chennai" is one question). In the call that turn is cancelled and saves
nothing. A workflow agent's turn runs on the fleet, and its job ran on: on dev (2026-10-16, call CAf875ef…) it saved
"Hi." and a greeting nobody heard, after the turn that came next, and its run was charged. Naveen: that job is
stopped and keeps nothing; its run is charged only for steps it finished. A turn that already acted (a tool ran)
keeps its question and calls, as a cut keeps them (F477), so the next turn does not act again.
"""
import asyncio

import fakeredis.aioredis

from core.store import sessions as store
from tests.unit.test_voice_call_loop import FakeSocket, ScriptedHost, _call, _drive, _final, _prompt, saved  # noqa: F401

SID, AID = "voice_chn_f643_CA" + "3" * 32, "agent_f643"
RUN = "run_f643"


class _Carrying(ScriptedHost):
    def __init__(self, *scripts):
        super().__init__(*scripts)
        self.carried = 0

    async def carry_turn(self, call):
        self.carried += 1


class TestTheCall:
    async def test_words_the_next_turn_takes_drop_the_turn_that_heard_them(self, saved):
        never = asyncio.Event()
        host = _Carrying([("wait", never), _final("never")], [_final("It is sunny in Chennai.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("Hi."), 0.05, _prompt("Weather in Chennai?"), settle=0.2)
        assert [t["text"] for t in host.turns] == ["Hi.", "Hi. Weather in Chennai?"]
        assert host.carried == 1 and saved["turns"] == []

    async def test_a_turn_that_acted_is_kept_not_dropped(self, saved):
        never = asyncio.Event()
        host = _Carrying([{"type": "tool_execution", "tool_name": "book"},
                          {"type": "tool_result", "tool_name": "book"}, ("wait", never), _final("never")],
                         [_final("Done.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("book it"), 0.05, _prompt("hello?"), settle=0.2)
        assert host.carried == 0 and saved["turns"] == [("book it", "", ["book"])]

    async def test_words_after_an_answer_began_drop_nothing(self, saved):
        never = asyncio.Event()
        host = _Carrying([{"type": "answer_delta", "text": "It is sunny, ", "turn": 1}, ("wait", never),
                          _final("never")], [_final("Okay.")])
        socket = FakeSocket()
        await _drive(_call(host, socket), socket, _prompt("weather?"), 0.1, _prompt("never mind"), settle=0.2)
        assert host.carried == 0


class TestTheDrop:
    async def test_it_names_one_turn_and_a_stop_names_the_session(self):
        from core.scale.pubsub import is_chat_cancelled, is_turn_dropped, publish_turn_drop

        redis = fakeredis.aioredis.FakeRedis()
        await publish_turn_drop(redis, SID, "v:CA3:1")
        assert await is_turn_dropped(redis, SID, "v:CA3:1")
        assert not await is_turn_dropped(redis, SID, "v:CA3:2"), "the next turn runs on"
        assert not await is_turn_dropped(redis, SID, None)
        assert not await is_chat_cancelled(redis, SID)


def _fleet(monkeypatch, loop):
    import core.react_engine as re_mod
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

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


async def _running_run():
    """The turn's run inside its first step: the step that finished before it keeps its entry."""
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    done = {"step_id": "route", "step_name": "Route", "step_type": "agent", "status": "completed",
            "duration_seconds": 1.0}
    await SharedState(OrchestrationRun(run_id=RUN, orchestration_id="orch_f643", status="running",
                                       current_step_id="answer", step_history=[done])).checkpoint()


async def _job(worker_mod, redis, chat_turn, message="Hi."):
    from core.store.engine import get_store
    return await worker_mod.run_agent_chat_job(
        {"redis": redis, "session_factory": await get_store(), "job_try": 1, "job_id": f"f643{chat_turn}"},
        SID, AID, message, chat_turn=chat_turn, keep_parked=True)


class TestTheJob:
    async def test_a_dropped_turn_keeps_nothing_and_its_run_ends_as_never_needed(self, monkeypatch):
        from core.orchestration.run_error import run_error
        from core.orchestration.state import SharedState
        from core.scale.pubsub import publish_turn_drop
        from core.scale.worker import CARRIED_ON

        await store.append_turn(SID, AID, [{"role": "user", "content": "hello"},
                                           {"role": "assistant", "content": "Hi there."}])
        await _running_run()

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "orchestration_start", "run_id": RUN}
            await asyncio.sleep(30)
            yield {"type": "final", "response": "Hello! How can I assist you today?"}

        worker_mod = _fleet(monkeypatch, loop)
        redis = fakeredis.aioredis.FakeRedis()
        job = asyncio.create_task(_job(worker_mod, redis, "v:CA3:1"))
        await asyncio.sleep(0.05)
        await publish_turn_drop(redis, SID, "v:CA3:1")
        result = await asyncio.wait_for(job, 5)

        assert result["status"] == "dropped"
        assert [m["content"] for m in (await store.get(SID, AID))["messages"]] == ["hello", "Hi there."]
        run = (await SharedState.restore(RUN)).run
        assert run.status == "cancelled" and run_error(run) == CARRIED_ON
        assert [e["step_id"] for e in run.step_history] == ["route"], "the step it was in is not priced"
        assert not await redis.get(f"synapse:drop:chat:{SID}:v:CA3:1"), "its drop is spent"

    async def test_the_next_turn_beside_it_runs_on(self, monkeypatch):
        from core.scale.pubsub import publish_turn_drop

        started = asyncio.Event()

        async def loop(request, server_module, should_stop=None, **_kw):
            if request.message == "Hi.":
                started.set()
                await asyncio.sleep(30)
            yield {"type": "final", "response": f"answer to {request.message}"}

        worker_mod = _fleet(monkeypatch, loop)
        redis = fakeredis.aioredis.FakeRedis()
        cut = asyncio.create_task(_job(worker_mod, redis, "v:CA3:1", "Hi."))
        await started.wait()
        await publish_turn_drop(redis, SID, "v:CA3:1")
        nxt = await asyncio.wait_for(_job(worker_mod, redis, "v:CA3:2", "Hi. Weather in Chennai?"), 5)
        assert (await asyncio.wait_for(cut, 5))["status"] == "dropped"
        assert nxt["status"] != "dropped"
        messages = (await store.get(SID, AID))["messages"]
        assert [m["content"] for m in messages] == ["Hi. Weather in Chennai?", "answer to Hi. Weather in Chennai?"]

    async def test_a_dropped_turn_that_acted_is_kept_with_its_calls(self, monkeypatch):
        from core.scale.pubsub import publish_turn_drop

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "tool_execution", "tool_name": "book", "args": {"slot": "noon"}}
            yield {"type": "tool_result", "tool_name": "book", "result": "booked"}
            await asyncio.sleep(30)
            yield {"type": "final", "response": "never"}

        worker_mod = _fleet(monkeypatch, loop)
        redis = fakeredis.aioredis.FakeRedis()
        job = asyncio.create_task(_job(worker_mod, redis, "v:CA3:1", "book noon"))
        await asyncio.sleep(0.05)
        await publish_turn_drop(redis, SID, "v:CA3:1")
        result = await asyncio.wait_for(job, 5)

        assert result["status"] != "dropped"
        messages = (await store.get(SID, AID))["messages"]
        assert messages[0]["content"] == "book noon", "what it did is kept, so the next turn does not do it again"
