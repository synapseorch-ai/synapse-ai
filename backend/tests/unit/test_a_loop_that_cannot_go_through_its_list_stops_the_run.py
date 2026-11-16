"""A Loop that cannot go through its list stops the run there, and nothing after it works on nothing (F1613).

The run this holds: a model step answers `{"keywords": [...]}` (its declared fields, F1553), a Loop goes through
`state.keyword_researcher` (the whole object), and a summary step after it sums up the loop's research. The loop
used to run zero times with a warning, the run said Completed, and the summary step was asked to sum up research
that never ran, and wrote one anyway. Now the Loop step fails with the field to go through named, and no model is
called after it. The same workflow pointed at `.keywords` runs its body once per keyword and the summary reads every
result.
"""
from __future__ import annotations

import types

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _workflow(over: str) -> dict:
    return S.make_orchestration(id="orch_f1613_run", entry_step_id="keyword_researcher", steps=[
        {"id": "keyword_researcher", "name": "Keyword researcher", "type": "llm", "model": "gpt-4o",
         "prompt_template": "Find keywords for {{ state.user_input }}.", "output_key": "keyword_researcher",
         "output_fields": [{"name": "keywords", "type": "list", "description": "The keywords"}],
         "next_step_id": "each"},
        {"id": "each", "name": "Research loop", "type": "loop", "loop_over": over, "loop_step_ids": ["research"],
         "next_step_id": "summary"},
        {"id": "research", "name": "Research", "type": "llm", "model": "gpt-4o",
         "prompt_template": "Research the keyword {{ state.loop_item }}.", "output_key": "research"},
        {"id": "summary", "name": "Summary", "type": "llm", "model": "gpt-4o",
         "prompt_template": "Sum up this research: {{ state.research }}", "output_key": "summary"},
    ])


async def _ran(orch: dict) -> list[dict]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [e async for e in engine.run("garden furniture", run_id=f"run_{orch['id']}")]


def _complete(events) -> dict:
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")


async def test_a_loop_over_the_whole_answer_fails_and_nothing_after_it_is_asked(fake_llm):
    fake_llm.script(['{"keywords": ["teak bench", "rattan chair"]}', "research", "research", "A summary."])
    events = await _ran(_workflow("state.keyword_researcher"))

    assert _complete(events)["status"] == "failed"
    assert len(fake_llm.calls) == 1, "no model may be asked anything after the loop that could not run"
    errors = [e["error"] for e in events if e.get("type") == "step_error" and e.get("orch_step_id") == "each"]
    assert errors and "state.keyword_researcher.keywords" in errors[0], errors
    assert "summary" not in (_complete(events).get("final_state") or {})


async def test_the_same_workflow_over_the_list_field_researches_each_keyword(fake_llm):
    fake_llm.script(['{"keywords": ["teak bench", "rattan chair"]}', "Teak: hardwood.", "Rattan: woven.",
                     "A summary."])
    events = await _ran(_workflow("state.keyword_researcher.keywords"))

    assert _complete(events)["status"] == "completed", [e for e in events if "error" in e]
    assert len(fake_llm.calls) == 4
    last = str(fake_llm.calls[-1])
    assert "Teak: hardwood." in last and "Rattan: woven." in last, last
