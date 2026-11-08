"""A Plan's `extract_json` step reads the answer of the step before it (F1304, CAP3's cap-06 A2; 79 11-03).

cap-06's A2 plan wrote the three pages' prices with an `llm` step and read them as a list with an `extract_json` step
right after it, as # Structure rule 2 says, for an `ingest` step to keep. Its compiled `parse_json` had no input keys:
`compile._wire` gave them only to the steps a model reads, and the extract step reads only its input keys, so it read
nothing and wrote None on every run ("'parse_json' holds nothing, not a list of objects"). Now an extract step reads
what the steps leading to it wrote, never the person's message (text that looks like JSON there is not the answer);
one whose `config` names its input keys keeps them.
"""
from __future__ import annotations

import json

from core.models_orchestration import OrchestrationRun, StepConfig
from core.native_builder import objects as O
from core.native_builder.compile import compile_plan
from core.orchestration.steps import ExtractJsonStepExecutor

PRICES = [{"competitor": "Brightsmile Supplies", "plan": "Starter", "price": "£29"},
          {"competitor": "Coastline Dental Depot", "plan": "Basic", "price": "£24"}]
# How the model answered on cap-06 A2: the list in a fenced block.
ANSWER = "```json\n" + json.dumps(PRICES, ensure_ascii=False, indent=2) + "\n```"


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


def _steps(parse_config: dict | None = None) -> dict[str, dict]:
    parse = {"id": "parse_json", "type": "extract_json", "name": "Read the prices", "does": "Reads the list",
             "next": "end"}
    plan = O.Plan.from_draft({"kind": "create", "title": "Competitor prices", "summary": "Keeps the prices.",
                              "steps": [
                                  {"id": "extract_pricing", "type": "llm", "name": "Write the prices",
                                   "does": "Lists every price", "prompt": "List every plan's price as JSON.",
                                   "next": "parse_json"},
                                  {**parse, **({"config": parse_config} if parse_config else {})}]})
    return {s["id"]: s for s in compile_plan(plan, mint=_mint()).orchestration["steps"]}


async def _extracted(step: dict, state: dict):
    run = OrchestrationRun(run_id="run_f1304", orchestration_id="orch_f1304", shared_state=dict(state))
    async for _ in ExtractJsonStepExecutor().execute(StepConfig(**step), run, None):
        pass
    return run.shared_state.get(step["output_key"])


def test_the_extract_step_reads_the_answer_before_it():
    steps = _steps()
    assert steps["parse_json"].get("input_keys") == ["extract_pricing"], steps["parse_json"]
    assert steps["extract_pricing"]["input_keys"] == ["user_input"], "a model's step reads as before"


async def test_cap_06s_list_reaches_the_step_after():
    step = _steps()["parse_json"]
    got = await _extracted(step, {"user_input": "Keep our competitors' prices.", "extract_pricing": ANSWER})
    assert got == PRICES


async def test_json_in_the_persons_message_is_not_the_answer():
    step = _steps()["parse_json"]
    got = await _extracted(step, {"user_input": 'Use {"note": "weekly"} as the label.', "extract_pricing": ANSWER})
    assert got == PRICES


def test_input_keys_the_plan_names_are_kept():
    assert _steps({"input_keys": ["extract_pricing", "user_input"]})["parse_json"]["input_keys"] == [
        "extract_pricing", "user_input"]
