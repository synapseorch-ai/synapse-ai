"""
An agent's model failure is not its answer (F133).

The ReAct loop caught any exception from the model call, took its text as the
turn's final response, saved it as the agent's reply and yielded it as `final`.
Measured on cloud with an own-key Anthropic call on a key with no credit: the
conversation stored "Claude LLM Error: … Your credit balance is too low …" as
what the agent said, with the session `completed`. So a channel posted it to
whoever was talking to the bot, a widget showed it to an anonymous visitor, a
schedule recorded a success, and an Agent step wrote it into its output key
while the run carried on — D112's class, a failure reported as a success.

Now the turn raises. The conversation keeps the question with a marker for the
answer, as a stopped turn does (D113); the provider's own words reach a member in
the app — the chat page's error and the run log — and nobody else.
"""
import json
import types

import pytest

from _fakes import seed as S
from core.llm_providers import LLMError
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures
from tests.unit.test_human_step_channel import messaging_on, rest, wired  # noqa: F401 — fixtures

WORDS = "Your credit balance is too low to access the Anthropic API."


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


@pytest.fixture
def failing_model(monkeypatch):
    """Every model call the agent loop makes fails, as a provider's refusal does."""
    import core.react_engine as re_mod

    async def refused(*_a, **_kw):
        raise LLMError(f"Claude LLM Error: API error 400: {WORDS}")

    monkeypatch.setattr(re_mod, "llm_generate_response", refused)


class TestAChatTurn:
    async def test_it_raises_and_keeps_the_question_with_a_marker(self, failing_model):
        from core.chat_turns import FAILED_ANSWER
        from core.react_engine import run_agent_step
        from core.session import get_recent_history_messages
        agent = S.make_agent(tools=[], skip_default_tools=True)
        events = []
        with pytest.raises(LLMError, match="credit balance"):
            async for event in run_agent_step(
                message="Say OK.", agent_id=agent["id"], session_id="s_f133", server_module=_server(),
                agent_override=agent, tools_override=[], max_turns=3,
            ):
                events.append(event)

        assert not [e for e in events if e.get("type") in ("final", "error")], (
            "no answer, and no error of its own — the caller reports it once")
        assert await get_recent_history_messages("s_f133", agent_id=agent["id"]) == [
            {"role": "user", "content": "Say OK."},
            {"role": "assistant", "content": FAILED_ANSWER},
        ]

    async def test_a_delegated_turn_saves_nothing(self, failing_model):
        """A hand-off to another agent is work done for it, not a conversation."""
        from core.react_engine import run_agent_step
        from core.session import get_recent_history_messages
        agent = S.make_agent(tools=[], skip_default_tools=True)
        with pytest.raises(LLMError):
            async for _ in run_agent_step(
                message="Say OK.", agent_id=agent["id"], session_id="s_f133_d", server_module=_server(),
                agent_override=agent, tools_override=[], max_turns=3, persist_turn=False,
            ):
                pass
        assert await get_recent_history_messages("s_f133_d", agent_id=agent["id"]) == []


class TestAnAgentStep:
    async def test_it_fails_the_run(self, driver, failing_model):
        await S.seed_agents([S.make_agent(id="agent_f133", tools=[], skip_default_tools=True)])
        orch = S.make_orchestration(id="orch_f133", entry_step_id="ask", steps=[
            {"id": "ask", "name": "Ask", "type": "agent", "agent_id": "agent_f133",
             "prompt_template": "go", "output_key": "reply", "next_step_id": "after", "max_turns": 2},
            {"id": "after", "name": "After", "type": "print", "print_content": "ran",
             "output_key": "o_after", "next_step_id": None},
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_f133") == "failed"
        run = await _run("run_f133")
        assert WORDS not in str(run.shared_state.get("reply")), "the error is not the step's output"
        assert "o_after" not in run.shared_state, "the next step did not run"
        (entry,) = [h for h in run.step_history if h.get("step_id") == "ask"]
        assert entry["status"] == "failed" and WORDS[:40] in entry["error"], (
            "the run log keeps the provider's words for the workspace")


class TestOnTheFleet:
    async def test_the_session_fails_with_the_question_and_a_marker(self, monkeypatch):
        from core.chat_turns import FAILED_ANSWER
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        import core.react_engine as re_mod
        upserts, sent = [], []

        async def history(*_a):
            return [{"role": "user", "content": "first"}, {"role": "assistant", "content": "first answer"}]

        async def upsert(_sf, _sid, _aid, status, messages):
            upserts.append((status, list(messages)))

        async def nothing(*_a, **_kw):
            return None

        async def acquire(*_a, **_kw):
            return object()

        async def failing_loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "status", "message": "working"}
            raise LLMError(f"Claude LLM Error: API error 400: {WORDS}")

        async def webhook(**kwargs):
            sent.append(kwargs["payload"])

        import core.scale.webhook as webhook_mod
        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", failing_loop)
        monkeypatch.setattr(webhook_mod, "deliver_webhook", webhook)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        import fakeredis.aioredis
        redis = fakeredis.aioredis.FakeRedis()
        with pytest.raises(LLMError):
            await worker_mod.run_agent_chat_job(
                {"redis": redis, "session_factory": object()}, "chat_f133", None, "Say OK.",
                webhook_url="https://example.test/hook")

        status, messages = upserts[-1]
        assert status == "failed"
        assert messages == [
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "Say OK."}, {"role": "assistant", "content": FAILED_ANSWER},
        ]
        events = [json.loads(f[b"data"]) for _, f in await redis.xrange("synapse:chat:chat_f133:events")]
        assert [e["type"] for e in events][-2:] == ["error", "done"]
        assert not [e for e in events if e["type"] in ("final", "response")]
        (payload,) = sent
        assert payload["status"] == "failed" and WORDS not in json.dumps(payload), (
            "an API caller's webhook says the turn failed, without the provider's words")


class TestWhoSeesTheProvidersWords:
    async def test_not_a_v1_caller(self, client, api_key, seed_agent, failing_model):
        await seed_agent()
        sync = await client.post("/api/v1/chat", json={"message": "hi"}, headers=api_key["headers"])
        assert sync.status_code == 500 and WORDS not in sync.text
        stream = await client.post("/api/v1/chat/stream", json={"message": "hi"},
                                   headers=api_key["headers"])
        assert WORDS not in stream.text
        assert "The agent encountered an error processing your request." in stream.text

    async def test_not_a_widget_visitor(self, client, seed_agent, failing_model):
        from core.chat_turns import FAILED_ANSWER
        from core.chat_widgets import limits
        limits.reset()
        agent = await seed_agent()
        widget = (await client.post("/api/widgets", json={
            "name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"]})).json()
        token = (await client.post("/api/widget/session", json={
            "public_key": widget["public_key"], "origin": "https://acme.com"})).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        turn = await client.post("/api/widget/chat", json={"message": "hi"}, headers=headers)
        assert WORDS not in turn.text
        history = await client.get("/api/widget/history", headers=headers)
        assert WORDS not in history.text and FAILED_ANSWER in history.text
        limits.reset()

    async def test_a_member_on_the_chat_page_does(self, client, seed_agent, failing_model):
        agent = await seed_agent()
        stream = await client.post("/chat/stream", json={"message": "hi", "agent_id": agent["id"]})
        assert WORDS in stream.text, "the page's failed-turn banner says what went wrong"
        assert '"type": "response"' not in stream.text.replace("'", '"'), "and does not show it as the answer"

    async def test_not_someone_on_a_channel(self, wired, failing_model):
        from core.messaging import store as channel_store
        agent = S.make_agent(id="agent_f133_chan", tools=[], skip_default_tools=True)
        await S.seed_agents([agent])
        await channel_store.set_assignments(wired.channel["id"], [agent["id"]])
        await wired.adapter._dispatch("c1", "hi", "u1")

        (reply,) = wired.adapter.sent
        assert WORDS not in reply
        assert reply == "⚠️ Something went wrong answering that. Please try again."


class TestASchedule:
    async def test_the_agent_run_is_logged_failed(self, failing_model, monkeypatch):
        import core.agent_logger as agent_logger_mod
        from core.scheduler import ScheduleManager
        ended = []
        real = agent_logger_mod.AgentLogger

        class Recording(real):
            def run_end(self, status):
                ended.append(status)
                super().run_end(status)

        monkeypatch.setattr(agent_logger_mod, "AgentLogger", Recording)
        agent = S.make_agent(id="agent_f133_sched", tools=[], skip_default_tools=True)
        await S.seed_agents([agent])
        manager = ScheduleManager()
        manager._server_module = _server()
        sched_log = types.SimpleNamespace(run_id="schedulerun_f133", log_event=lambda ev: None)

        with pytest.raises(LLMError):
            await manager._run_agent_schedule(
                {"id": "sched_f133", "name": "Daily", "target_id": agent["id"], "prompt": "go"}, sched_log)
        assert ended == ["failed"]


class TestTheBuilderOnTheChatPage:
    async def test_a_failed_run_is_not_reported_done(self, monkeypatch):
        """A builder-typed agent's run that failed ended its chat turn "Done."."""
        import core.routes.builder as builder
        from core.chat_turns import FAILED_ANSWER
        from core.session import get_recent_history_messages

        async def failed_run(_req, _server):
            yield {"type": "step_error", "orch_step_id": "s", "error": "An internal error occurred."}
            yield {"type": "orchestration_complete", "status": "failed", "final_state": {}}
            yield {"type": "final", "response": f"Orchestration failed. Error: LLMError: {WORDS}"}

        monkeypatch.setattr(builder, "run_builder_stream", failed_run)
        request = types.SimpleNamespace(message="build me a thing", session_id="s_f133_b",
                                        agent_id="agent_builder_x", images=[])
        frames = [f async for f in builder.run_builder_stream_compat(request, _server())]
        text = "".join(frames)
        assert "Done." not in text
        assert '"type": "error"' in text and text.rstrip().endswith('"type": "done"}')
        assert await get_recent_history_messages("s_f133_b", agent_id="agent_builder_x") == [
            {"role": "user", "content": "build me a thing"},
            {"role": "assistant", "content": FAILED_ANSWER},
        ]
