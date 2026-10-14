"""A call turn closed early closes the engine's stream under it, in the same task (F528, VOX1f, D273).

VOX1f's call 1 (`CA5d1e35…`): the call closes a turn's stream when the turn ends early (the caller talks over it, a
goodbye after a tool) with `events.aclose()`, but the host's `run_turn`/`resume_turn` passed `run_react_loop`'s events
on with `async for … yield`, so nothing closed the engine's generator. The event loop's finaliser closed it later in a
new task: a ContextVar reset there raises "was created in a different Context" (the voice log had two), and the
engine's own clean-up ran late. Each host now closes what it delegates to before its own close returns.
"""
import asyncio
import contextvars
import types

import pytest

MARK = contextvars.ContextVar("f528_mark", default=None)


def _engine(seen: dict):
    async def run_react_loop(request, server_module, **_kw):
        token = MARK.set("set")
        try:
            yield {"type": "answer_delta", "text": "The weather "}
            yield {"type": "answer_delta", "text": "in Delhi"}
            yield {"type": "final", "response": "The weather in Delhi"}
        finally:
            seen["task"] = asyncio.current_task()
            MARK.reset(token)  # raises in a task that did not set it
            seen["closed"] = True
    return run_react_loop


def _call():
    from core.visitor import VisitorScope
    return types.SimpleNamespace(session_id="voice_chn_1_CA" + "1" * 32, agent_id="agent_1",
                                 visitor=VisitorScope(surface="voice", tools=frozenset()))


@pytest.mark.parametrize("which", ["run_turn", "resume_turn"])
async def test_closing_a_turn_closes_the_engines_stream_now(monkeypatch, which):
    import core.react_engine as re_mod
    from core.messaging.voice.hooks import VoiceHost

    seen: dict = {}
    monkeypatch.setattr(re_mod, "run_react_loop", _engine(seen))
    host = VoiceHost()
    if which == "run_turn":
        stream = host.run_turn(_call(), "weather?", caller_system="", extra_tools=[])
    else:
        stream = host.resume_turn(_call(), {"message": "weather?", "resume_state": {"x": 1}}, caller_system="",
                                  extra_tools=[])
    first = await stream.__anext__()
    assert first["type"] == "answer_delta"
    await stream.aclose()  # what the call does when the caller talks over the answer
    assert seen.get("closed") is True, "the engine's clean-up ran before the close returned"
    assert seen["task"] is asyncio.current_task(), "in the call's own task, not the finaliser's"
