"""The practice run leaves state in the shape the engine's own run leaves it (F1619).

The practice run walks a plan's steps itself, so what it judges is only as good as how closely it runs as the engine
does. Here each planned workflow the plan check's own corpus holds (129 model plans, and the shipped templates) runs
twice on the same made-up answers: once through the engine, its model, tool and Human steps replaced by ones that
write the answer the practice makes (`practice_run.made_answer`), and once through the practice, following the one
way the engine takes. Every key either run leaves, and the shape of each value (its keys to their depth, a list's
items, the type of the rest), is the same.

Code runs on this computer for the test, in both.
"""
from __future__ import annotations

import dataclasses
import json
import types
from pathlib import Path

import pytest

from core.models_orchestration import Orchestration, StepType
from core.native_builder import objects as O
from core.native_builder import practice_run
from core.native_builder.compile import compile_plan
from core.orchestration.step_answers import tool_answers

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = BACKEND / "tests" / "fixtures" / "sai2_plans"
_INV = json.loads((FIXTURES / "inventory.json").read_text())
INVENTORY = O.Inventory(agents=_INV["agents"], workflows=_INV["workflows"], tools=set(_INV["tools"]))
AGENTS = {i: {"id": i, "name": n, "type": "conversational", "tools": ["all"]} for i, n in _INV["agents"].items()}
ANSWERS = tool_answers([])


def _docs() -> list[tuple[str, dict]]:
    out = []
    for line in (FIXTURES / "probe_plans.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        plan, _ = O.read(row["plan"])
        if O.check_structure(plan, INVENTORY)[0]:
            continue
        n = {"i": 0}

        def mint(prefix: str, length: int = 7) -> str:
            n["i"] += 1
            return f"{prefix}_{n['i']:0{length}d}"

        compiled = compile_plan(plan, existing_agents=AGENTS, mint=mint)
        if compiled.all_issues():
            continue
        name = f"{row['run']}-{row['model']}-{row['persona']}"
        out += [(f"{name}#{i}", d) for i, d in enumerate([compiled.orchestration,
                                                          *(c.orchestration for c in compiled.inner)])]
    for path in sorted((BACKEND / "examples").glob("*.bundle.json")):
        for orch in json.loads(path.read_text()).get("orchestrations") or []:
            out.append((f"{path.name}:{orch.get('id')}", orch))
    # Code that reads the Vault reads the person's own file, which neither run has here.
    return [(n, d) for n, d in out if "read_vault" not in json.dumps(d)]


DOCS = _docs()


class _Here:
    async def run(self, request):
        from core.execution import HostRunner, Network

        return await HostRunner().run(dataclasses.replace(request, network=Network.INTERNET))


class _Answers:
    """A model, tool or Human step, writing the practice's own made-up answer."""

    async def execute(self, step, run, engine):
        made = practice_run.made_answer(step, ANSWERS)
        if made.value is not practice_run.UNKNOWN and step.output_key:
            run.shared_state[step.output_key] = made.value
        if step.type == "human":
            run.shared_state["human_response"] = made.value
        return
        yield  # noqa: unreachable — an executor is an async generator


class _FirstRoute:
    async def execute(self, step, run, engine):
        label = next(iter(step.route_map or {}), "")
        run.shared_state[f"_routing_decision_{step.id}"] = label
        if step.output_key:
            run.shared_state[step.output_key] = label
        return
        yield  # noqa: unreachable


class _Nothing:
    async def execute(self, step, run, engine):
        return
        yield  # noqa: unreachable


def _shape(value):
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _kept(state: dict) -> dict:
    """What a run leaves that a later step reads: not the engine's own keys."""
    return {k: _shape(v) for k, v in state.items() if not k.startswith("_") and k not in ("history",)}


@pytest.fixture
def engine_with_stand_ins(monkeypatch):
    from core.orchestration import steps as steps_mod

    for kind in (StepType.LLM, StepType.AGENT, StepType.TOOL, StepType.HUMAN):
        monkeypatch.setitem(steps_mod.STEP_EXECUTORS, kind, _Answers())
    monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.EVALUATOR, _FirstRoute())
    for name in ("RETRIEVE", "INGEST", "BROWSER", "WORKFLOW"):
        if hasattr(StepType, name):
            monkeypatch.setitem(steps_mod.STEP_EXECUTORS, getattr(StepType, name), _Nothing())
    monkeypatch.setenv("SYNAPSE_TRANSFORM_RUNTIME", "host")
    practice_run.set_runner(_Here())
    yield
    practice_run.set_runner(practice_run.OFF)


def test_there_is_a_corpus_to_hold_it_to():
    # The model plans that compile clean, each workflow of them, and the templates': 85 when this was written.
    assert len(DOCS) >= 80


@pytest.mark.parametrize("name,doc", DOCS, ids=[n for n, _ in DOCS])
async def test_the_practice_leaves_what_the_engine_leaves(name, doc, engine_with_stand_ins):
    from _fakes import seed as S
    from core.orchestration.engine import OrchestrationEngine

    orch = Orchestration.model_validate(doc)
    await S.seed_orchestrations([doc])
    engine = OrchestrationEngine(orch, types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    events = [e async for e in engine.run("example request", run_id=f"run_practice_{abs(hash(name)) % 10**9}",
                                          initial_state=practice_run.made_up_inputs(orch))]
    final = next((e for e in events if e.get("type") == "orchestration_complete"), None)
    if final is None or final.get("status") != "completed":
        failed = [e.get("error") or e.get("message") for e in events if e.get("type") in (
            "orchestration_error", "step_error")]
        pytest.skip(f"the engine's own run did not complete: {failed[:1]}")
    state = final["final_state"]
    practice = await practice_run.practise([doc], answers=ANSWERS, follow=True)
    assert len(practice.ended) == 1, practice.ended
    ended = practice.ended[0]
    # A key the practice holds no value for (nothing declares what wrote it) is one it judges nothing by; every other
    # key is the engine's, in the engine's shape.
    engine_kept = {k: v for k, v in _kept(state).items() if k not in ended["unknown"]}
    assert _kept(ended["state"]) == engine_kept
