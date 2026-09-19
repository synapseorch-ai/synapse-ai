"""
A human step that names a messaging channel asks there, and a reply answers it (F54).

Neither half worked. The step armed an in-memory Future and threw away the task
awaiting it; the method meant to send the prompt only logged; a reply resolved
the Future, whose result went nowhere — so the adapter answered "Resuming the
workflow…" and the run stayed paused. The browser cleared the Future only when
it sent a `step_id`, which OSS's canvas never did, so the next message in the
channel was swallowed for up to an hour.

Now the channel lives with the run (the engine records it at the pause) and a
reply looks for the newest run waiting on that channel in the store, answering
it through the same path as the app's Answer control — so it survives a
restart and works for a run a worker ran. Sending is a seam a process installs;
cloud installs none and sends its own way.
"""
import asyncio
import functools
import types
from unittest.mock import AsyncMock

import pytest

from _fakes import seed as S
from core.messaging import store as channel_store
from core.messaging.base import NO_AGENT_MESSAGE
from core.messaging.manager import MessagingManager, send_human_prompt
from core.orchestration.human_prompts import HUMAN_CHANNEL_KEY, set_human_prompt_sender
from tests.unit.test_messaging_agent_scope import _adapter


@pytest.fixture
def rest(monkeypatch):
    """Telegram's REST send, recorded."""
    import core.messaging.senders as senders
    sent: list[tuple[str, str]] = []

    async def record(credentials, chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setitem(senders._RAW_SENDERS, "telegram", record)
    return sent


@pytest.fixture
def messaging_on(monkeypatch):
    import core.config as config
    real = config.load_settings
    monkeypatch.setattr(config, "load_settings", lambda *a, **k: {**(real(*a, **k) or {}), "messaging_enabled": True})


@pytest.fixture
async def wired(rest, messaging_on):
    """What the API server wires: a manager, a running adapter, the sender."""
    ch = await channel_store.save_channel(
        {"name": "Ops", "platform": "telegram", "credentials": {"bot_token": "1:x"}})
    await channel_store.set_default_conversation(ch["id"], "c1")
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    manager = MessagingManager(server_module=server)
    server.app = types.SimpleNamespace(state=types.SimpleNamespace(
        server_module=server, messaging_manager=manager,
        redis=None, arq_redis=None, pg_session_factory=None,
    ))
    adapter = _adapter(ch, manager)
    manager._adapters[ch["id"]] = adapter
    set_human_prompt_sender(functools.partial(send_human_prompt, manager=manager))
    try:
        yield types.SimpleNamespace(channel=ch, server=server, manager=manager, adapter=adapter, rest=rest)
    finally:
        set_human_prompt_sender(None)


def _asking(orch_id, channel_id, next_step="p"):
    return S.make_orchestration(id=orch_id, entry_step_id="h", steps=[
        {"id": "h", "name": "Approve", "type": "human", "human_prompt": "Ship it?",
         "human_channel_id": channel_id, "output_key": "approval", "next_step_id": next_step},
        {"id": "p", "name": "Show", "type": "print", "print_content": "got {state.approval}",
         "output_key": "final", "next_step_id": None},
    ])


async def _run_to_pause(orch, server, run_id, also=()):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    await S.seed_orchestrations([orch, *also])  # saving replaces the whole set
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]
    assert any(e.get("type") == "human_input_required" for e in events)


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


async def _settled(run_id, status="completed"):
    for _ in range(100):
        if (await _run(run_id)).status == status:
            return await _run(run_id)
        await asyncio.sleep(0.02)
    return await _run(run_id)


class TestTheQuestionIsAsked:
    async def test_the_prompt_goes_to_the_channels_conversation(self, wired):
        await _run_to_pause(_asking("orch_ask", wired.channel["id"]), wired.server, "run_ask")

        assert len(wired.rest) == 1
        chat_id, text = wired.rest[0]
        assert chat_id == "c1"
        assert "Ship it?" in text
        assert (await _run("run_ask")).shared_state[HUMAN_CHANNEL_KEY] == wired.channel["id"]

    async def test_a_nested_step_asks_once(self, wired):
        child = _asking("orch_child", wired.channel["id"])
        parent = S.make_orchestration(id="orch_outer", entry_step_id="s", steps=[
            {"id": "s", "name": "Delegate", "type": "agent", "agent_id": "orch_agent",
             "prompt_template": "go", "output_key": "result", "next_step_id": None}])
        await S.seed_agents([S.make_orchestrator_agent("orch_child", id="orch_agent")])

        await _run_to_pause(parent, wired.server, "run_outer", also=[child])

        assert len(wired.rest) == 1
        assert (await _run("run_outer")).shared_state[HUMAN_CHANNEL_KEY] == wired.channel["id"]

    async def test_nothing_is_sent_where_no_sender_is_installed(self, wired):
        """An embedder that delivers channel messages its own way installs none."""
        set_human_prompt_sender(None)
        await _run_to_pause(_asking("orch_quiet", wired.channel["id"]), wired.server, "run_quiet")

        assert wired.rest == []

    async def test_nothing_is_sent_while_messaging_is_off(self, wired, monkeypatch):
        import core.config as config
        monkeypatch.setattr(config, "load_settings", lambda *a, **k: {"messaging_enabled": False})
        await _run_to_pause(_asking("orch_off", wired.channel["id"]), wired.server, "run_off")

        assert wired.rest == []


class TestAReplyAnswers:
    async def test_the_reply_resumes_the_run_with_the_answer(self, wired, fake_llm):
        await _run_to_pause(_asking("orch_reply", wired.channel["id"]), wired.server, "run_reply")

        await wired.adapter._dispatch("c1", "yes, ship", "u1")

        assert wired.adapter.sent == ["✅ Answer received — resuming the workflow."]
        run = await _settled("run_reply")
        assert run.status == "completed"
        assert run.shared_state["approval"] == "yes, ship"
        assert HUMAN_CHANNEL_KEY not in run.shared_state

    async def test_a_nested_run_is_answered_through_its_parent(self, wired, fake_llm):
        child = _asking("orch_child2", wired.channel["id"])
        parent = S.make_orchestration(id="orch_outer2", entry_step_id="s", steps=[
            {"id": "s", "name": "Delegate", "type": "agent", "agent_id": "orch_agent2",
             "prompt_template": "go", "output_key": "result", "next_step_id": None}])
        await S.seed_agents([S.make_orchestrator_agent("orch_child2", id="orch_agent2")])
        await _run_to_pause(parent, wired.server, "run_outer2", also=[child])

        await wired.adapter._dispatch("c1", "approved", "u1")

        run = await _settled("run_outer2")
        assert run.status == "completed"
        assert run.shared_state["result"] == "got approved"

    async def test_a_message_when_nothing_waits_is_an_ordinary_message(self, wired):
        await wired.adapter._dispatch("c1", "hello", "u1")

        assert wired.adapter.sent == [NO_AGENT_MESSAGE]

    async def test_a_run_waiting_on_another_channel_is_not_answered(self, wired):
        await _run_to_pause(_asking("orch_else", "some-other-channel"), wired.server, "run_else")

        await wired.adapter._dispatch("c1", "yes", "u1")

        assert wired.adapter.sent == [NO_AGENT_MESSAGE]
        assert (await _run("run_else")).status == "paused"

    async def test_a_question_answered_in_the_app_is_not_answered_again(self, wired, fake_llm):
        from core.orchestration.answers import answer_paused_run
        await _run_to_pause(
            _asking("orch_twice", wired.channel["id"], next_step=None), wired.server, "run_twice")
        await answer_paused_run(wired.server.app.state, "run_twice", "from the app")
        await _settled("run_twice")

        await wired.adapter._dispatch("c1", "from the channel", "u1")

        assert wired.adapter.sent == [NO_AGENT_MESSAGE]
        assert (await _run("run_twice")).shared_state["approval"] == "from the app"

    async def test_a_run_a_worker_ran_is_resumed_by_a_worker(self, wired, fake_redis):
        from core.store import get_store, upsert
        from core.store.models import OrchestrationRunDB
        async with (await get_store())() as session:
            await upsert(session, OrchestrationRunDB, values={
                "run_id": "run_fleet", "orchestration_id": "orch_x", "status": "paused",
                "waiting_for_human": True, "current_step_id": "h", "worker_id": "worker-a",
                "shared_state": {HUMAN_CHANNEL_KEY: wired.channel["id"]},
            }, index_elements=["run_id"])
            await session.commit()
        state = wired.server.app.state
        state.redis, state.arq_redis, state.pg_session_factory = fake_redis, AsyncMock(), await get_store()

        await wired.adapter._dispatch("c1", "go ahead", "u1")

        args, kwargs = state.arq_redis.enqueue_job.call_args
        assert args[0] == "resume_orchestration_job"
        assert kwargs["run_id"] == "run_fleet"
        assert kwargs["human_response"] == {"response": "go ahead"}


async def _asked_seconds_ago(run_id, seconds):
    """Move a paused run's question back in time, as a clock would have."""
    import time as _time
    from sqlalchemy import select
    from core.store import session
    from core.store.models import OrchestrationRunDB
    async with session() as s:
        row = (await s.execute(select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id))).scalar_one()
        history = [dict(e) for e in row.step_history]
        for entry in history:
            if entry.get("status") == "paused":
                entry["started_at"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - seconds))
        row.step_history = history
        await s.commit()


def _asking_briefly(orch_id, channel_id, timeout=60):
    orch = _asking(orch_id, channel_id)
    orch["steps"][0]["human_timeout_seconds"] = timeout
    return orch


class TestAQuestionExpires:
    """A channel question is answerable for its step's `human_timeout_seconds`.

    The setting had no reader in OSS once F54 removed the Future that held it.
    Cloud already used it: a reply after the window is refused in a sentence and
    the run keeps waiting for an answer in the app (F93, D109). OSS does the same.
    """

    async def test_a_late_reply_is_refused_and_the_run_keeps_waiting(self, wired, fake_llm):
        from core.messaging.base import QUESTION_EXPIRED_MESSAGE
        await _run_to_pause(_asking_briefly("orch_late", wired.channel["id"]), wired.server, "run_late")
        await _asked_seconds_ago("run_late", 120)

        await wired.adapter._dispatch("c1", "yes, ship", "u1")

        assert wired.adapter.sent == [QUESTION_EXPIRED_MESSAGE]
        run = await _run("run_late")
        assert run.status == "paused" and run.waiting_for_human
        assert "approval" not in run.shared_state

    async def test_the_app_can_still_answer_an_expired_question(self, wired, fake_llm):
        from core.orchestration.answers import answer_paused_run
        await _run_to_pause(_asking_briefly("orch_late_app", wired.channel["id"]), wired.server, "run_late_app")
        await _asked_seconds_ago("run_late_app", 120)
        await wired.adapter._dispatch("c1", "from the channel", "u1")

        await answer_paused_run(wired.server.app.state, "run_late_app", "from the app")

        run = await _settled("run_late_app")
        assert run.status == "completed"
        assert run.shared_state["approval"] == "from the app"

    async def test_a_reply_inside_the_window_is_answered(self, wired, fake_llm):
        await _run_to_pause(_asking_briefly("orch_prompt", wired.channel["id"]), wired.server, "run_prompt")
        await _asked_seconds_ago("run_prompt", 30)

        await wired.adapter._dispatch("c1", "yes", "u1")

        assert wired.adapter.sent == ["✅ Answer received — resuming the workflow."]
        assert (await _settled("run_prompt")).shared_state["approval"] == "yes"

    async def test_a_nested_question_goes_by_the_sub_runs_own_pause(self, wired, fake_llm):
        child = _asking_briefly("orch_child3", wired.channel["id"])
        parent = S.make_orchestration(id="orch_outer3", entry_step_id="s", steps=[
            {"id": "s", "name": "Delegate", "type": "agent", "agent_id": "orch_agent3",
             "prompt_template": "go", "output_key": "result", "next_step_id": None}])
        await S.seed_agents([S.make_orchestrator_agent("orch_child3", id="orch_agent3")])
        await _run_to_pause(parent, wired.server, "run_outer3", also=[child])
        await _asked_seconds_ago((await _run("run_outer3")).nested_run_id, 120)

        await wired.adapter._dispatch("c1", "approved", "u1")

        from core.messaging.base import QUESTION_EXPIRED_MESSAGE
        assert wired.adapter.sent == [QUESTION_EXPIRED_MESSAGE]
        assert (await _run("run_outer3")).status == "paused"


class TestTheAppsAnswer:
    async def test_a_run_that_is_not_waiting_is_refused(self, client):
        from core.store import get_store, upsert
        from core.store.models import OrchestrationRunDB
        async with (await get_store())() as session:
            await upsert(session, OrchestrationRunDB, values={
                "run_id": "run_done", "orchestration_id": "orch_x", "status": "completed",
            }, index_elements=["run_id"])
            await session.commit()

        resp = await client.post("/api/orchestrations/runs/run_done/human-input", json={"response": "late"})

        assert resp.status_code == 409

    async def test_an_unknown_run_is_not_found(self, client):
        resp = await client.post("/api/orchestrations/runs/nope/human-input", json={"response": "x"})

        assert resp.status_code == 404


class TestATeamsReplyAnswers:
    """F94. Teams had its own turn handler — a copy of the agent half of
    `_dispatch` — and never reached the half that answers a run waiting on the
    channel, so a reply to a human step's question went to the agent as chat
    and the run stayed paused. WhatsApp and the webhook transports go through
    `_dispatch` and always worked."""

    async def test_a_teams_reply_answers_the_run_waiting_on_the_channel(
        self, wired, fake_llm, monkeypatch
    ):
        import sys

        from core.messaging.adapters.teams import TeamsAdapter

        # The Bot Framework SDK is an optional install; the turn only needs its
        # message factory to wrap a string.
        fake_core = types.ModuleType("botbuilder.core")
        fake_core.MessageFactory = types.SimpleNamespace(text=lambda text: text)
        monkeypatch.setitem(sys.modules, "botbuilder", types.ModuleType("botbuilder"))
        monkeypatch.setitem(sys.modules, "botbuilder.core", fake_core)

        ch = await channel_store.save_channel(
            {"name": "Teams", "platform": "teams", "credentials": {"app_id": "a", "app_password": "p"}})
        adapter = TeamsAdapter(ch, wired.manager)
        wired.manager._adapters[ch["id"]] = adapter
        await _run_to_pause(_asking("orch_teams", ch["id"]), wired.server, "run_teams")

        said: list[str] = []
        turn = types.SimpleNamespace(
            activity=types.SimpleNamespace(
                text="yes, ship",
                conversation=types.SimpleNamespace(id="19:conv"),
                from_property=types.SimpleNamespace(id="u1"),
            ),
            send_activity=AsyncMock(side_effect=lambda message: said.append(message)),
        )
        await adapter._on_turn(turn)

        assert said == ["✅ Answer received — resuming the workflow."]
        run = await _settled("run_teams")
        assert run.status == "completed"
        assert run.shared_state["approval"] == "yes, ship"
