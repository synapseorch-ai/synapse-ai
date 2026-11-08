"""A call's own tool ends the turn wherever the turn runs, and only the call keeps its record (F642, VOX1f, D273).

A call's `end_call` and `transfer_call` end the turn as they are called: the call speaks the goodbye or hand-off
line and saves it, with no second model call to word it (V17). In the call that held because the call stopped
reading the turn. A workflow agent's turn runs on the fleet, where nothing stopped it: on dev (2026-10-16, call
CAf875ef…) the step made one more model call ("Thank you for calling. Have a great day!", ~2 s and a credit) and the
job saved it beside the call's own record — every goodbye twice. Naveen: the engine ends the turn at the call's
tool (`ends_turn` on its handler), and the job leaves the record to the call.
"""
import types

import fakeredis.aioredis

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.store import sessions as store
from core.visitor import VisitorScope
from tests.unit.test_voice_engine_seams import _hang_up_tool, _run, one_tool  # noqa: F401 — fixture

SID, AID = "voice_chn_f642_CA" + "2" * 32, "agent_f642"


class TestTheEngine:
    async def test_a_tool_that_ends_the_turn_has_no_model_call_after_it(self, fake_llm, one_tool):
        from core.messaging.voice import tools as voice_tools

        events = await _run(fake_llm, [tool_call("end_call", goodbye="Bye now."), "Thank you for calling."],
                            S.make_agent(tools=["all"]), VisitorScope("channel", frozenset()),
                            voice_tools.for_the_fleet([voice_tools.END_CALL]))
        assert len(fake_llm.calls) == 1, "nothing after the goodbye is written"
        assert [e["tool_name"] for e in events if e.get("type") == "tool_result"] == ["end_call"]
        finals = [e.get("response") for e in events if e.get("type") == "final"]
        assert finals and "Thank you" not in finals[-1]

    async def test_any_other_callers_tool_hands_back_to_the_model(self, fake_llm, one_tool):
        """VOX1a's seam, unchanged for a handler that does not end the turn."""
        seen: list = []
        await _run(fake_llm, [tool_call("end_call"), "Goodbye."], S.make_agent(tools=["all"]),
                   VisitorScope("channel", frozenset()), [_hang_up_tool(seen)])
        assert len(fake_llm.calls) == 2 and seen == [{}]

    async def test_a_workflow_agents_step_ends_at_the_goodbye(self, fake_llm):
        import core.react_engine as re_mod
        from core.messaging.voice import tools as voice_tools
        from core.models import ChatRequest

        step_agent = S.make_agent(id="agent_f642_step", tools=["all"], skip_default_tools=True)
        orch = S.make_orchestration(id="orch_f642", entry_step_id="answer", steps=[
            {"id": "answer", "name": "Answer", "type": "agent", "agent_id": step_agent["id"],
             "prompt_template": "{state.user_input}", "output_key": "answer", "max_turns": 3,
             "next_step_id": None}])
        await S.seed_orchestrations([orch])
        flow = S.make_orchestrator_agent("orch_f642", id="agent_orch_f642", name="Harbor line flow")
        await S.seed_agents([flow, step_agent])
        fake_llm.script([tool_call("end_call", goodbye="Thanks for calling."), "Thank you for calling. Bye!"])
        events = [e async for e in re_mod.run_react_loop(
            ChatRequest(message="That's all. Bye.", session_id="s_f642", agent_id=flow["id"]),
            types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={}),
            extra_tools=voice_tools.for_the_fleet([voice_tools.END_CALL]))]
        assert len(fake_llm.calls) == 1, "the step stopped at the goodbye"
        assert any(e.get("type") == "tool_execution" and e.get("tool_name") == "end_call" for e in events)
        assert not any("Thank you for calling" in str(e.get("response") or "") for e in events)


def test_the_calls_tools_end_their_turn_here_and_on_the_fleet():
    from core.messaging.voice import tools as voice_tools

    call = types.SimpleNamespace(pending_action=None)
    for _tool, handler in voice_tools.build(call, can_transfer=True):
        assert handler.ends_turn is True
    for _tool, handler in voice_tools.for_the_fleet([voice_tools.END_CALL, voice_tools.TRANSFER_CALL]):
        assert handler.ends_turn is True


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


async def _job(worker_mod, message, chat_turn):
    from core.store.engine import get_store
    return await worker_mod.run_agent_chat_job(
        {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": await get_store(), "job_try": 1,
         "job_id": f"f642{chat_turn}"}, SID, AID, message, chat_turn=chat_turn, keep_parked=True,
        caller_tools=["end_call", "transfer_call"])


class TestTheJob:
    async def test_a_turn_the_calls_tool_ended_is_the_calls_to_save(self, monkeypatch):
        await store.append_turn(SID, AID, [{"role": "user", "content": "weather?"},
                                           {"role": "assistant", "content": "Sunny."}])
        worker_mod = _fleet(monkeypatch, [
            {"type": "tool_execution", "tool_name": "end_call", "args": {"goodbye": "Bye now."}},
            {"type": "tool_result", "tool_name": "end_call", "preview": "Ending the call"},
            {"type": "final", "response": "Ending the call; your goodbye is spoken for you."},
        ])
        result = await _job(worker_mod, "That's all. Bye.", "v:CA2:7")
        assert result["status"] == "completed"
        assert [m["content"] for m in (await store.get(SID, AID))["messages"]] == ["weather?", "Sunny."]

    async def test_a_turn_it_did_not_end_is_saved_as_ever(self, monkeypatch):
        worker_mod = _fleet(monkeypatch, [{"type": "final", "response": "We open at nine."}])
        await _job(worker_mod, "when do you open?", "v:CA2:8")
        answer = (await store.get(SID, AID))["messages"][-1]
        assert answer["content"] == "We open at nine." and answer["chat_turn"] == "v:CA2:8"
