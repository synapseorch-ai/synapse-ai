"""A step says each note its model calls make once, however many of its calls make it (F1665's follow-up).

An LLM step with answer fields can call its model twice: once, then once more when the answer misses a field. Each call
that asks for the answer in words after a refusal of its schema mode notes it (F1618, F1665), and the step said every
call's notes, so the same "The answer was asked for in words" line showed twice for one step. Now a step keeps a line
once.

Near misses: two different notes on one step both stay, and two steps that each step down each say it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import core.llm_providers as llm

STEPPED = "The answer was asked for in words: the provider refused its schema mode (too complex)."
OTHER = "The answer was asked for in words: the provider refused its schema mode (its answer did not fit)."
FIELDS = [{"name": "kind", "type": "choice", "options": ["question", "complaint"]}]


def _model(monkeypatch, answers: list[str], notes: list[str | None]):
    """The model: each call answers the next of `answers` and notes the next of `notes` (None: no note)."""
    calls = {"n": 0}

    async def generate(prompt_msg, sys_prompt, *a, **kw):
        i = calls["n"]
        calls["n"] += 1
        said = kw.get("answer_notes")
        if said is not None and i < len(notes) and notes[i]:
            said.append(notes[i])
        return answers[min(i, len(answers) - 1)]

    monkeypatch.setattr(llm, "generate_response", generate)
    return calls


async def _run(steps: list[dict], run_id: str) -> list[dict]:
    from _fakes import seed as S
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id=steps[0]["id"], steps=steps)
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 NS(agent_sessions={}, memory_store=None, tool_router={}))
    return [e async for e in engine.run("Do you ship to Madeira?", run_id=run_id)]


def _step(sid: str, nxt: str | None) -> dict:
    return {"id": sid, "name": sid.title(), "type": "llm", "model": "claude-sonnet-4-6", "prompt_template": "Classify.",
            "output_key": sid, "output_fields": FIELDS, "next_step_id": nxt}


def _warned(events: list[dict], step: str | None = None) -> list[str]:
    return [e["message"] for e in events if e.get("type") == "step_warning"
            and (step is None or e.get("orch_step_id") == step) and "asked for in words" in e.get("message", "")]


async def test_a_note_two_calls_make_is_said_once(monkeypatch):
    # The first answer misses its field, so the step asks once more; both calls step down.
    calls = _model(monkeypatch, ["not json", json.dumps({"kind": "question"})], [STEPPED, STEPPED])
    events = await _run([_step("classify", None)], "f1665_once")
    assert calls["n"] == 2, "the case itself: the step asked twice"
    assert _warned(events) == [STEPPED]


async def test_two_different_notes_both_stay(monkeypatch):
    _model(monkeypatch, ["not json", json.dumps({"kind": "question"})], [STEPPED, OTHER])
    events = await _run([_step("classify", None)], "f1665_two_notes")
    assert _warned(events) == [STEPPED, OTHER]


async def test_two_steps_each_say_theirs(monkeypatch):
    _model(monkeypatch, [json.dumps({"kind": "question"})], [STEPPED, STEPPED])
    events = await _run([_step("first", "second"), _step("second", None)], "f1665_two_steps")
    assert _warned(events, "first") == [STEPPED] and _warned(events, "second") == [STEPPED]
