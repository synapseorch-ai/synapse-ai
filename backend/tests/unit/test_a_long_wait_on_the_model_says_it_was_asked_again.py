"""A wait on the model says it was asked again for as long as it waits, and a builder turn that fails on the model says
what to do next in plain words (F1682).

A model call that did not answer in time was sent again and said so once (`model_wait.report`, a `status` event). The
panel draws `status` and the waiting heartbeat into one live line, so the next heartbeat ("waiting on the model, Ns")
replaced the notice 15 seconds later: a build that waited sixteen minutes through its tries showed only a counter. Each
heartbeat now carries the newest notice. And when the turn's model call failed after its tries, the panel said the
call's own text ("Claude LLM Error: failed after 5 attempts. Last error: Request timed out (180s)"). It now says what
happened and what to do next (try again, a smaller ask, another model), quoting no raw error; the step's failure
event carries its model failure, the sentence a person is told (F1502), for the panel to say.

Near misses: a wait with no retry reads as before; a step that fails for a reason other than its model keeps its words.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace as NS

import core.llm_providers as llm
import core.model_wait as mw
from _fakes import seed as S

TIMED_OUT = "Claude LLM Error: failed after 5 attempts. Last error: Request timed out (180s)"


async def _beats(awaitable) -> list[str]:
    wait = mw.ModelWait(awaitable, heartbeat=0.05)
    return [e["message"] for e in [e async for e in wait.events()] if e["type"] == "thinking"]


# ── the wait says it was asked again ──────────────────────────────────────────────────────────────────────────────

async def test_each_heartbeat_after_a_retry_carries_it():
    async def call():
        mw.report("The model did not answer within 180s — retrying (2 of 5) in 5s.")
        await asyncio.sleep(0.3)
        return "ok"

    beats = await _beats(call())
    assert len(beats) >= 2, beats
    assert all("waiting on the model" in b and "retrying (2 of 5)" in b for b in beats), beats
    assert not any("in 5s" in b for b in beats), "the backoff it named is over by the next heartbeat"


async def test_the_newest_retry_is_the_one_said():
    async def call():
        mw.report("The model did not answer within 180s — retrying (2 of 5) in 5s.")
        await asyncio.sleep(0.15)
        mw.report("The model did not answer within 180s — retrying (3 of 5) in 10s.")
        await asyncio.sleep(0.2)
        return "ok"

    beats = await _beats(call())
    assert "retrying (3 of 5)" in beats[-1] and "(2 of 5)" not in beats[-1], beats


async def test_a_wait_with_no_retry_reads_as_before():
    async def call():
        await asyncio.sleep(0.2)
        return "ok"

    beats = await _beats(call())
    assert beats and all(re.fullmatch(r"waiting on the model, \d+s", b) for b in beats), beats


# ── a turn that fails on its model says what to do next ───────────────────────────────────────────────────────────

async def _failed_step_event(monkeypatch, error: Exception) -> dict:
    """The step_error a model step's failure sends, from a real run of one LLM step."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    async def fails(*a, **kw):
        raise error

    monkeypatch.setattr(llm, "generate_response", fails)
    orch = S.make_orchestration(id="orch_f1682", entry_step_id="plan", steps=[
        {"id": "plan", "name": "Plan It", "type": "llm", "model": "mistral", "prompt_template": "Plan it.",
         "output_key": "plan", "next_step_id": None}])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), NS(agent_sessions={}, memory_store=None,
                                                                        tool_router={}))
    events = [e async for e in engine.run("go", run_id="run_f1682")]
    return next(e for e in events if e.get("type") == "step_error")


async def _panel(event: dict) -> dict:
    from core.routes.builder import SENTINEL, _queue_events

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(event)
    await queue.put(SENTINEL)
    return [e async for e in _queue_events(queue)][0]


async def test_the_failure_event_carries_its_model_failure(monkeypatch):
    event = await _failed_step_event(monkeypatch, llm.LLMError(TIMED_OUT))
    assert (event.get("model_failure") or {}).get("said"), event


async def test_the_panel_says_the_model_didnt_answer_and_what_to_do(monkeypatch):
    shown = await _panel(await _failed_step_event(monkeypatch, llm.LLMError(TIMED_OUT)))
    for words in (shown["error"], shown["reason"]):
        assert "didn't answer in time" in words, shown
        assert "Try again" in words and "smaller part" in words and "another model for Synapse AI" in words, shown
        assert not re.search(r"LLM ?Error|Request timed out|attempts|180s", words), shown


async def test_another_model_failure_says_its_own_sentence_and_another_model(monkeypatch):
    shown = await _panel(await _failed_step_event(monkeypatch, llm.LLMError("HTTP 429: slow down", status=429)))
    assert "limiting requests" in shown["reason"] and "another model for Synapse AI" in shown["reason"], shown
    assert "429" not in shown["reason"] and "slow down" not in shown["reason"], shown


async def test_a_failure_that_isnt_the_models_keeps_its_words():
    event = {"type": "step_error", "orch_step_id": "plan", "error": "Step 'Plan It' failed",
             "reason": "KeyError: 'plan_context'"}
    assert await _panel(dict(event)) == event
