"""
A distributed cancel reaches a step in progress.

The cancel signal is a Redis key. The engine polls it between steps; the
worker adapter now watches it while a step runs and cancels the task driving
the engine — the ARQ job — as soon as it appears. `fake_redis` is real
fakeredis, so publish_cancellation / is_cancelled work end to end.
"""
import asyncio
import json
import types

import pytest

from _fakes import seed as S

RUN = "run_cancel"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _agent_orch():
    return S.make_orchestration(
        id="orch_cancel", entry_step_id="s1",
        steps=[{"id": "s1", "name": "Long step", "type": "agent", "agent_id": "a1",
                "output_key": "result", "next_step_id": "s2", "max_turns": 5},
               {"id": "s2", "name": "After", "type": "print", "print_content": "after",
                "output_key": "after", "next_step_id": None}],
    )


async def _adapter(fake_redis, run_id=RUN):
    from core.models_orchestration import Orchestration
    from core.scale.pubsub import RunEventPublisher
    from core.scale.worker_engine_adapter import WorkerEngineAdapter
    from core.store import get_store
    return WorkerEngineAdapter(
        orch=Orchestration.model_validate(_agent_orch()), run_id=run_id,
        worker_server_module=_server(), publisher=RunEventPublisher(fake_redis, run_id),
        session_factory=await get_store(), redis_client=fake_redis,
        worker_id="w-test", job_id="job-test",
    )


async def _stream(fake_redis, run_id=RUN) -> list[dict]:
    entries = await fake_redis.xrange(f"synapse:run:{run_id}:events")
    return [json.loads(fields[b"data"]) for _, fields in entries]


@pytest.fixture
def blocking_agent(monkeypatch):
    """run_agent_step that signals it started, then sleeps as a long step would."""
    import core.react_engine as re_mod
    started = asyncio.Event()
    seen: dict = {}

    async def blocked(**kwargs):
        seen.update(kwargs)
        started.set()
        await asyncio.sleep(30)
        yield {"type": "final", "response": "never"}

    monkeypatch.setattr(re_mod, "run_agent_step", blocked)
    return started, seen


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    import core.scale.worker_engine_adapter as adapter_mod
    monkeypatch.setattr(adapter_mod, "CANCEL_POLL_SECONDS", 0.05)


class TestARequestedCancelInterruptsTheStep:
    async def test_the_run_ends_cancelled_within_a_poll(self, fake_redis, blocking_agent):
        from core.orchestration.state import SharedState
        from core.scale.pubsub import is_cancelled, publish_cancellation
        started, _ = blocking_agent
        adapter = await _adapter(fake_redis)

        job = asyncio.create_task(adapter.run(initial_input="go"))
        await asyncio.wait_for(started.wait(), 5)
        await publish_cancellation(fake_redis, RUN)

        status = await asyncio.wait_for(job, 3)
        assert status == "cancelled"

        run = (await SharedState.restore(RUN)).run
        assert run.status == "cancelled"
        assert run.ended_at is not None
        assert run.current_step_id == "s1"                   # resumable from where it stopped
        assert await is_cancelled(fake_redis, RUN) is False   # signal cleared

        types_ = [e["type"] for e in await _stream(fake_redis)]
        assert types_[-2:] == ["orchestration_error", "done"]
        assert next(e for e in await _stream(fake_redis) if e["type"] == "orchestration_error")["error"] == "Cancelled"
        # The step after the cancelled one never ran.
        assert not any(e.get("orch_step_id") == "s2" for e in await _stream(fake_redis))

    async def test_a_cancel_the_worker_did_not_request_propagates(self, fake_redis, blocking_agent):
        """ARQ's own timeout or shutdown cancels the job task too. That is not
        a user's cancel, and it must reach ARQ as it always did."""
        started, _ = blocking_agent
        adapter = await _adapter(fake_redis)
        job = asyncio.create_task(adapter.run(initial_input="go"))
        await asyncio.wait_for(started.wait(), 5)

        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    async def test_the_watcher_does_not_outlive_the_job(self, fake_redis, monkeypatch):
        import core.react_engine as re_mod

        async def quick(**kwargs):
            yield {"type": "final", "response": "done"}

        monkeypatch.setattr(re_mod, "run_agent_step", quick)
        adapter = await _adapter(fake_redis, run_id="run_quick")
        before = {t for t in asyncio.all_tasks() if not t.done()}
        assert await adapter.run(initial_input="go") == "completed"
        await asyncio.sleep(0)
        lingering = {t for t in asyncio.all_tasks() if not t.done()} - before
        assert not [t for t in lingering if "watch" in repr(t.get_coro())], lingering

    async def test_the_engine_hands_the_hook_to_the_agent_loop(self, fake_redis, monkeypatch):
        """Defence in depth: the agent loop can stop between turns on its own."""
        import core.react_engine as re_mod
        seen: dict = {}

        async def recording(**kwargs):
            seen.update(kwargs)
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(re_mod, "run_agent_step", recording)
        adapter = await _adapter(fake_redis, run_id="run_hook")
        await adapter.run(initial_input="go")
        assert callable(seen.get("should_stop"))


class TestTheCooperativeCheck:
    async def test_should_stop_ends_the_step_without_another_llm_call(self, fake_llm):
        from _fakes.fake_llm import tool_call
        from core.react_engine import run_agent_step

        answers = iter([False, True])

        async def should_stop():
            return next(answers)

        fake_llm.script([tool_call("my_tool", x=1), tool_call("my_tool", x=2), "done"])
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        schema = {"type": "function", "function": {"name": "my_tool", "description": "t",
                                                    "parameters": {"type": "object", "properties": {}}}}

        async def executor(name, args):
            return "out"

        with pytest.raises(asyncio.CancelledError):
            async for _ in run_agent_step(
                message="go", agent_id="a1", session_id="s", server_module=_server(),
                agent_override=agent, tools_override=[schema], tool_executor=executor,
                max_turns=5, should_stop=should_stop,
            ):
                pass
        assert fake_llm.call_count == 1

    async def test_no_hook_means_no_change(self, fake_llm):
        from core.react_engine import run_agent_step
        fake_llm.script(["done"])
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        events = [e async for e in run_agent_step(
            message="go", agent_id="a1", session_id="s", server_module=_server(),
            agent_override=agent, tools_override=[], max_turns=2,
        )]
        assert events[-1]["type"] == "final"
