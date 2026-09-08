"""
Engine cost benchmark. Not a load test: one run per phase, LLM instant, and the
numbers are the point. Every optimisation commit re-runs this and quotes the
delta; a commit whose numbers do not move in the expected direction is not done.

Phases, each isolating one cost centre:

  agent_step   one agent, ten LLM turns (tool calls + task-list updates) — the
               per-turn progress checkpoint, prompt stability, resolution reads
  rerun        agent -> evaluator routes back -> agent (execution #2) -> ok
  foreach      five-item for-each over a print body — per-iteration checkpoints
  parallel     three llm branches with a 0.3s fake delay — sequential vs concurrent

Run:  SYNAPSE_FAKE_LLM_DELAY_MAX=0 python -m pytest -m stress -q -s \
          backend/tests/stress/test_bench_engine.py
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from bench_harness import BenchRecorder, write_bench_report

pytestmark = pytest.mark.stress


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, run_id, user_input="benchmark this"):
    """Drive through the production pump so the journal and broker are in the
    measurement, exactly as an HTTP-started run would be."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import stream_engine_events
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    events = []
    async for ev in stream_engine_events(engine.run(user_input, run_id=run_id), run_id):
        events.append(ev)
    return events


def _tasks(*done: int):
    return [{"content": f"step {i}", "status": "completed" if i in done else "pending"}
            for i in range(1, 4)]


AGENT = "bench_agent"


def _agent_step_orch():
    return S.make_orchestration(
        id="orch_bench_agent", entry_step_id="work",
        steps=[
            {"id": "work", "name": "Work", "type": "agent", "agent_id": AGENT,
             "prompt_template": "Do the work on {state.user_input}",
             "output_key": "work", "max_turns": 15, "next_step_id": None},
        ],
    )


def _rerun_orch():
    return S.make_orchestration(
        id="orch_bench_rerun", entry_step_id="work",
        steps=[
            {"id": "work", "name": "Draft", "type": "agent", "agent_id": AGENT,
             "prompt_template": "Draft something about {state.user_input}",
             "output_key": "draft", "max_turns": 3, "max_iterations": 3,
             "next_step_id": "review"},
            {"id": "review", "name": "Review", "type": "evaluator",
             "route_map": {"retry": "work", "ok": None},
             "route_descriptions": {"retry": "needs work", "ok": "good"},
             "evaluator_prompt": "Judge the draft.", "input_keys": ["draft"],
             "output_key": "verdict", "next_step_id": None},
        ],
    )


def _foreach_orch():
    return S.make_orchestration(
        id="orch_bench_foreach", entry_step_id="loop1",
        steps=[
            {"id": "loop1", "name": "Each", "type": "loop", "loop_over": "state.items",
             "loop_step_ids": ["body"], "next_step_id": None},
            {"id": "body", "name": "Body", "type": "print",
             "print_content": "{state.loop_index}:{state.loop_item}",
             "output_key": "seen", "next_step_id": None},
        ],
        state_schema={"items": {"type": "list", "default": ["a", "b", "c", "d", "e"]}},
    )


def _parallel_orch():
    return S.make_orchestration(
        id="orch_bench_parallel", entry_step_id="fan",
        steps=[
            {"id": "fan", "name": "Fan out", "type": "parallel",
             "parallel_branches": [["b1"], ["b2"], ["b3"]], "next_step_id": "join"},
            *[{"id": f"b{i}", "name": f"Branch {i}", "type": "llm",
               "prompt_template": f"Angle {i} on {{state.user_input}}",
               "output_key": f"o{i}", "next_step_id": None} for i in (1, 2, 3)],
            {"id": "join", "name": "Join", "type": "merge", "merge_strategy": "list",
             "input_keys": ["o1", "o2", "o3"], "output_key": "joined",
             "next_step_id": None},
        ],
    )


async def test_engine_benchmark(fake_llm, monkeypatch):
    # The stress conftest defaults to the 5-90s "sometimes slow" profile. This
    # benchmark wants the engine's own overhead, so the LLM is instant except
    # where a phase sets a delay to make concurrency measurable.
    for k, v in (("MIN", "0"), ("MAX", "0"), ("PROB", "0")):
        monkeypatch.setenv(f"SYNAPSE_FAKE_LLM_DELAY_{k}", v)

    await S.seed_agents([S.make_agent(id=AGENT, tools=["all"], max_turns=15)])
    phases: dict[str, dict] = {}

    # -- agent_step ---------------------------------------------------------------
    # Nine tool turns then a final: unknown tools cost a turn and an observation
    # (the engine nudges and continues); update_tasks hits the tasks_updated
    # checkpoint path as well as the per-turn progress one. The second update
    # closes every task — a final given with tasks still open is refused and
    # re-prompted, which would add turns the script does not account for.
    script = []
    for turn in range(1, 10):
        if turn == 3:
            script.append(tool_call("update_tasks", tasks=_tasks(1)))
        elif turn == 7:
            script.append(tool_call("update_tasks", tasks=_tasks(1, 2, 3)))
        else:
            script.append(tool_call("bench_tool", n=turn))
    script.append("Work complete.")
    fake_llm.script(script)

    async with BenchRecorder(monkeypatch, fake_llm) as rec:
        events = await _run(_agent_step_orch(), "bench_agent_step")
    assert any(e.get("type") == "orchestration_complete" for e in events)
    calls = rec.llm_calls()
    # Ten scripted responses must mean ten LLM calls. If the engine makes a call
    # on its own account (compaction, a summary, a projection) it eats a script
    # entry and shifts everything after it — so name every call on failure.
    shape = [(i, c.get("tool_name"), str(c.get("sys_prompt", ""))[:40],
              str(c.get("prompt_msg", ""))[:60], "…", str(c.get("prompt_msg", ""))[-160:])
             for i, c in enumerate(calls)]
    assert len(calls) == 10, "\n".join(map(str, shape))
    phases["agent_step"] = rec.metrics()

    # -- rerun --------------------------------------------------------------------
    fake_llm.script([
        "draft v1",
        tool_call("route_retry", reasoning="thin"),
        "draft v2",
        tool_call("route_ok", reasoning="fine"),
    ])
    async with BenchRecorder(monkeypatch, fake_llm) as rec:
        events = await _run(_rerun_orch(), "bench_rerun")
    assert any(e.get("type") == "orchestration_complete" for e in events)
    started = [e["orch_step_id"] for e in events if e.get("type") == "step_start"]
    assert started.count("work") == 2, started
    phases["rerun"] = rec.metrics()

    # -- foreach ------------------------------------------------------------------
    async with BenchRecorder(monkeypatch, fake_llm) as rec:
        events = await _run(_foreach_orch(), "bench_foreach")
    lc = next(e for e in events if e.get("type") == "loop_complete")
    assert lc["iterations_completed"] == 5
    phases["foreach"] = rec.metrics()

    # -- parallel -----------------------------------------------------------------
    for k, v in (("MIN", "0.3"), ("MAX", "0.3"), ("PROB", "1")):
        monkeypatch.setenv(f"SYNAPSE_FAKE_LLM_DELAY_{k}", v)
    fake_llm.set_default("branch output")
    async with BenchRecorder(monkeypatch, fake_llm) as rec:
        events = await _run(_parallel_orch(), "bench_parallel")
    assert any(e.get("type") == "parallel_complete" for e in events)
    m = rec.metrics()
    m["branch_delay_seconds"] = 0.3
    m["branches"] = 3
    phases["parallel"] = m

    write_bench_report(phases)
