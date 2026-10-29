"""A state key's declared type is the type its step writes (F1247; TRI1's triage of Naveen's prod chat, class 7).

Synapse AI's two approval routers are If/Else steps, and an If/Else step writes `bool(condition)` under its output
key; both keys were declared `str`, so every approval said "State key 'approval_decision' is declared str but …" as
a step warning (F14's typing). The walker below holds every shipped definition (the builder's own bundle and the
example packs) to the engine's fixed writes, so the class can't come back: an If/Else writes a bool, a Print its
text, and a Human step with fields the answers as an object. The steps whose writes vary (a tool's result, a
switch's value, a model's answer) are not walked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
#: What the engine writes under a step's output key, where that is fixed by the step's type.
WRITES = {"if_else": "bool", "print": "str"}


def _written(step: dict) -> str | None:
    if step.get("type") == "human" and step.get("human_fields"):
        return "dict"
    return WRITES.get(step.get("type"))


def _workflows(doc):
    if isinstance(doc, dict):
        if isinstance(doc.get("steps"), list) and isinstance(doc.get("state_schema"), dict):
            yield doc
        for value in doc.values():
            yield from _workflows(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _workflows(value)


SHIPPED = [BACKEND / "core" / "native_builder" / "orchestration.json", *sorted((BACKEND / "examples").glob("*.json"))]


def test_there_is_something_to_walk():
    assert len(SHIPPED) >= 5 and all(p.exists() for p in SHIPPED)
    assert sum(1 for p in SHIPPED for _ in _workflows(json.loads(p.read_text()))) >= 5


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_each_key_a_step_writes_is_declared_as_it_is_written(path):
    wrong = []
    for orch in _workflows(json.loads(path.read_text())):
        schema = orch["state_schema"]
        for step in orch["steps"]:
            key, written = step.get("output_key"), _written(step)
            if key in schema and written and (schema[key].get("type") or "str") != written:
                wrong.append(f"{orch.get('name') or orch.get('id')}: '{key}' is declared {schema[key].get('type')}, "
                             f"but step '{step['id']}' ({step['type']}) writes a {written}")
    assert not wrong, wrong


async def test_an_if_else_writes_a_bool():
    """The walker's table, held to the engine: an If/Else step writes `bool(condition)`, whatever the condition gives."""
    from core.models_orchestration import OrchestrationRun, StepConfig
    from core.orchestration.steps import IfElseStepExecutor

    run = OrchestrationRun(run_id="run_f1247", orchestration_id="orch_f1247", shared_state={"said": "approve"})
    step = StepConfig(id="route", name="Route", type="if_else", output_key="decision",
                      if_condition='state.said in ("approve", "ok")')
    [_ async for _ in IfElseStepExecutor().execute(step, run, None)]
    assert run.shared_state["decision"] is True
