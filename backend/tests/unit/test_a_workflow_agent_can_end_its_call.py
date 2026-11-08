"""A workflow agent on a phone call holds the call's controls (F531, VOX1f, D273).

VOX1f's call 7: a workflow agent's turns run on the fleet, and the call's `end_call`/`transfer_call` act on the live
call in the voice process, so the fleet never offered them; "No. Thanks.", "No." and "Bye." each got a goodbye and the
line stayed open until the caller hung up. Now the call's tools go to the fleet by name (`caller_tools`), declared
there without acting (`voice_tools.for_the_fleet`); a workflow run hands them to its agent steps; and a call of one
is relayed to the call, which acts on it as on its own (`voice_tools.action_from`).
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def test_the_fleets_tools_are_the_calls_and_act_on_nothing():
    import asyncio

    from core.messaging.voice import tools as voice_tools

    here = {t.name: t for t, _ in voice_tools.build(types.SimpleNamespace(pending_action=None), can_transfer=True)}
    there = voice_tools.for_the_fleet([voice_tools.END_CALL, voice_tools.TRANSFER_CALL, "nope"])
    assert [t.name for t, _ in there] == [voice_tools.END_CALL, voice_tools.TRANSFER_CALL]
    for tool, handler in there:
        assert tool.inputSchema == here[tool.name].inputSchema and tool.description == here[tool.name].description
        assert isinstance(asyncio.run(handler({"goodbye": "Bye", "reason": "x"})), str)


def test_a_relayed_call_of_one_is_the_calls_action():
    from core.messaging.voice import tools as voice_tools

    assert voice_tools.action_from({"type": "tool_execution", "tool_name": "end_call",
                                    "args": {"goodbye": "Bye!"}}) == ("end", {"goodbye": "Bye!"})
    assert voice_tools.action_from({"type": "tool_execution", "tool_name": "transfer_call",
                                    "args": {"reason": "billing"}}) == ("transfer", {"reason": "billing"})
    assert voice_tools.action_from({"type": "tool_execution", "tool_name": "shop_search_products"}) is None


async def test_a_workflow_agents_step_holds_the_callers_tools(fake_llm, monkeypatch):
    import core.react_engine as re_mod
    from core.messaging.voice import tools as voice_tools
    from core.models import ChatRequest

    step_agent = S.make_agent(id="agent_f531_step", tools=["all"], skip_default_tools=True)
    orch = S.make_orchestration(id="orch_f531", entry_step_id="answer", steps=[
        {"id": "answer", "name": "Answer", "type": "agent", "agent_id": step_agent["id"],
         "prompt_template": "{state.user_input}", "output_key": "answer", "max_turns": 3, "next_step_id": None}])
    await S.seed_orchestrations([orch])
    flow = S.make_orchestrator_agent("orch_f531", id="agent_orch_f531", name="Harbor line flow")
    await S.seed_agents([flow, step_agent])
    seen: list = []
    real = re_mod.run_agent_step

    async def watched(**kwargs):
        seen.append([t.name for t, _ in kwargs.get("extra_tools") or []])
        async for ev in real(**kwargs):
            yield ev
    monkeypatch.setattr(re_mod, "run_agent_step", watched)
    fake_llm.script([tool_call("end_call", goodbye="Thanks for calling."), "Goodbye."])
    events = [e async for e in re_mod.run_react_loop(
        ChatRequest(message="Bye.", session_id="s_f531", agent_id=flow["id"]),
        types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={}),
        extra_tools=voice_tools.for_the_fleet([voice_tools.END_CALL]))]
    assert seen and seen[0] == [voice_tools.END_CALL], seen
    ran = [e for e in events if e.get("type") == "tool_execution" and e.get("tool_name") == "end_call"]
    assert ran and ran[0].get("args") == {"goodbye": "Thanks for calling."}, "relayed with its goodbye"


async def test_the_fleet_job_offers_the_tools_the_call_named(monkeypatch):
    import fakeredis.aioredis

    import core.react_engine as re_mod
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod
    from core.store.engine import get_store

    seen: list = []

    async def loop(request, server_module, should_stop=None, **kw):
        seen.append([t.name for t, _ in kw.get("extra_tools") or []])
        yield {"type": "final", "response": "Goodbye."}

    async def nothing(*_a, **_kw):
        return None

    async def got(*_a, **_kw):
        return object()
    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(mcp_pool, "acquire", got)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    ctx = {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": await get_store(), "job_try": 1,
           "job_id": "f531"}
    await worker_mod.run_agent_chat_job(ctx, "voice_chn_f531_CA" + "8" * 32, "agent_f531", "Bye.",
                                        caller_tools=["end_call", "transfer_call"])
    await worker_mod.run_agent_chat_job(ctx, "s_f531_chat", "agent_f531", "hi")
    assert seen == [["end_call", "transfer_call"], []]
