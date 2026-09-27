"""An orchestration deployed as an agent keeps the conversation (F260).

Every message to an orchestrator agent started a fresh run with nothing of the
conversation in it: the exchange was saved and never read back. "Charged twice,
$49 each" was answered; "what amount did I say?" was met with "I don't have
access to the previous messages" — in chat and on the widget, and a messaging
channel did not even save the turn.

Now the recent turns of the conversation reach the run as `state.history`, and
every model step sees them under "Conversation so far". The conversation is the
caller's own — its session and this agent — so a widget visitor reads only
their own; a schedule and a delegation start fresh, as they always have.
"""
import types

from _fakes import seed as S
from tests.unit.test_a_failed_child_fails_its_parent import _print  # noqa: F401


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


def _answer_step(nxt=None):
    return {"id": "answer", "name": "Answer", "type": "llm", "prompt_template": "Reply to: {state.user_input}",
            "model": "gpt-4o", "output_key": "reply", "next_step_id": nxt}


async def _workflow_agent(orch_id, steps, entry, **kw):
    orch = S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps, **kw)
    await S.seed_orchestrations([orch])
    agent = S.make_orchestrator_agent(orch_id, id=f"agent_{orch_id}", name="Support Triage")
    await S.seed_agents([agent])
    return agent


async def _chat(agent, message, session_id):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop
    events = [e async for e in run_react_loop(
        ChatRequest(message=message, session_id=session_id, agent_id=agent["id"]), _server())]
    return next((e.get("response") for e in reversed(events)
                 if e.get("type") == "final" and not e.get("orch_step_id")), None)


class TestInChat:
    async def test_the_second_message_sees_the_first(self, fake_llm):
        agent = await _workflow_agent("orch_f260", [_answer_step()], "answer")
        fake_llm.script(["Sorry about that — what is the account email?", "You said $49, charged twice."])

        await _chat(agent, "I was charged twice ($49 each)", "sess_f260")
        await _chat(agent, "what amount did I say, and how many times?", "sess_f260")

        prompt = fake_llm.calls[-1]["prompt_msg"]
        assert "CONVERSATION SO FAR" in prompt
        assert "I was charged twice ($49 each)" in prompt
        assert "what is the account email?" in prompt

    async def test_the_run_holds_it_as_state_history(self, fake_llm):
        agent = await _workflow_agent("orch_f260h", [
            _answer_step("count"),
            {"id": "count", "name": "Count", "type": "transform", "transform_code": "result = len(state.history)",
             "output_key": "turns_seen", "next_step_id": None}], "answer", result_key="reply")
        fake_llm.script(["first reply", "second reply"])

        await _chat(agent, "one", "sess_f260h")
        await _chat(agent, "two", "sess_f260h")

        from core.orchestration.state import SharedState
        runs = [r for r in await SharedState.list_runs(limit=10) if r["orchestration_id"] == "orch_f260h"]
        latest = (await SharedState.restore(runs[0]["run_id"])).run
        assert latest.shared_state["history"] == [
            {"role": "user", "content": "one"}, {"role": "assistant", "content": "first reply"}]
        assert latest.shared_state["turns_seen"] == 2

    async def test_another_conversation_is_not_read(self, fake_llm):
        agent = await _workflow_agent("orch_f260s", [_answer_step()], "answer")
        fake_llm.script(["reply A", "reply B"])

        await _chat(agent, "my secret is 4242", "sess_f260_a")
        await _chat(agent, "what did I say?", "sess_f260_b")

        assert "4242" not in fake_llm.calls[-1]["prompt_msg"]

    async def test_a_key_the_orchestration_declares_is_its_own(self, fake_llm):
        agent = await _workflow_agent("orch_f260d", [_answer_step()], "answer",
                                      state_schema={"history": {"type": "str", "default": "mine"}})
        fake_llm.script(["one", "two"])

        await _chat(agent, "hi", "sess_f260d")
        await _chat(agent, "again", "sess_f260d")

        from core.orchestration.state import SharedState
        runs = [r for r in await SharedState.list_runs(limit=10) if r["orchestration_id"] == "orch_f260d"]
        assert (await SharedState.restore(runs[0]["run_id"])).run.shared_state["history"] == "mine"


class TestOnAChannel:
    async def test_a_follow_up_sees_the_first_message(self, wired, fake_llm):
        from core.messaging import store as channel_store
        agent = await _workflow_agent("orch_f260c", [_answer_step()], "answer")
        await channel_store.set_assignments(wired.channel["id"], [agent["id"]])
        fake_llm.script(["What is the account email?", "You said $49."])

        await wired.adapter._dispatch("c1", "charged twice, $49 each", "u1")
        await wired.adapter._dispatch("c1", "what amount did I say?", "u1")

        assert "charged twice, $49 each" in fake_llm.calls[-1]["prompt_msg"]


class TestAScheduleStartsFresh:
    async def test_no_history_is_read(self, fake_llm):
        from core.react_engine import run_orchestrator_agent
        from core.session import _save_conversation_turn
        agent = await _workflow_agent("orch_f260r", [_answer_step()], "answer")
        await _save_conversation_turn(session_id="schedule_x", agent_id=agent["id"], user="old", assistant="old reply")
        fake_llm.script(["fresh"])

        [e async for e in run_orchestrator_agent(agent, "go", _server(), session_id="schedule_x")]

        assert "CONVERSATION SO FAR" not in fake_llm.calls[-1]["prompt_msg"]


from tests.unit.test_human_step_channel import messaging_on, rest, wired  # noqa: E402,F401 — fixtures
