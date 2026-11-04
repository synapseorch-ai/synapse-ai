"""A Tool step's JSON answer reaches the step that reads it as data (F1358, cap-04; eb 11-04).

CAP3's FF18 gate, cap-04: a Tool step read a CSV from the vault (`vault_read`), and a Transform looked up its
`content`. The tool answers in text, `{"path": …, "content": "date,region,…"}` as JSON, and a Tool step stores that
text. So the Transform's `isinstance(csv_data, dict)` was false, it fell back to `str()`, and pandas parsed the whole
JSON. The engine already parses a key the workflow declares `list` or `dict` out of text (`_type_written_state`, as
`extract_json` does), and compile declares one for each step that reads a model's answer as a structure (F1287). But
it took a Tool step's answer for data already, which it is not.

Now a Tool step's answer is declared the same way, for the same readers: a Loop over it, an Ingest of its records, a
Transform going through it or looking fields up in it, a condition reading its field. What a Transform or an Extract
JSON step wrote is data already, and still left alone. And as before, the engine parses only what is declared: a
workflow saved without a declaration reads a Tool step's text as text, a Transform that `json.loads` it is not
declared, and an answer that is not JSON is kept as it came, with the engine's warning, and the run goes on.
"""
from __future__ import annotations

import json
import types

import pytest

from core.native_builder import objects as O
from core.native_builder.compile import compile_plan

CSV = "date,region,product,units,amount\n2026-10-12,East,composite,8,96.00\n2026-10-12,West,trays,5,60.00\n"
#: What `vault_read` answered in cap-04's run, and the Tool step stored (its path shortened).
ANSWER = json.dumps({"path": "vault/cap3/sales.csv", "content": CSV}, indent=2)

#: cap-04's Transform, as the builder wrote it (ff18-c1-cli-claude-claude-haiku-4-5 records), up to its read.
CAP04_CODE = ("import pandas as pd\nfrom io import StringIO\n\ncsv_data = state['read_csv']\n"
              "if isinstance(csv_data, dict) and 'content' in csv_data:\n    csv_text = csv_data['content']\n"
              "else:\n    csv_text = str(csv_data)\n\ndf = pd.read_csv(StringIO(csv_text))\n"
              "result = df.to_csv(index=False)")


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


def _compiled(steps, settings=None):
    plan = O.Plan.from_draft({"kind": "create", "title": "Sales by region", "summary": "S", "steps": list(steps),
                              **({"settings": settings} if settings else {})})
    return compile_plan(plan, mint=_mint())


def _schema(compiled) -> dict:
    return compiled.orchestration.get("state_schema") or {}


READ = {"id": "read_csv", "type": "tool", "name": "Read the CSV", "does": "Reads the sales CSV", "tool": "vault_read",
        "next": "parse_sales"}


def _transform(code: str, sid: str = "parse_sales") -> dict:
    return {"id": sid, "type": "transform", "name": "Parse sales", "does": "Sums sales by region",
            "config": {"transform_code": code}, "next": "end"}


def _typed(orchestration: dict, value, key: str = "read_csv"):
    """What the engine leaves in state after the Tool step `key` wrote `value`, and the warnings it said."""
    from core.models_orchestration import Orchestration, StepConfig
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orchestration),
                                 types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    run = types.SimpleNamespace(shared_state={key: value})
    warnings, _ = engine._type_written_state(
        run, StepConfig(id=key, name="Read", type="tool", forced_tool="vault_read", output_key=key), {})
    return run.shared_state[key], warnings, engine.orch


class TestCap04:
    def test_its_transform_reading_content_declares_the_tools_answer_an_object(self):
        compiled = _compiled([READ, _transform(CAP04_CODE)])
        assert _schema(compiled).get("read_csv", {}).get("type") == "dict", _schema(compiled)

    def test_the_engine_hands_its_transform_the_csv(self):
        compiled = _compiled([READ, _transform(CAP04_CODE)])
        value, warnings, _ = _typed(compiled.orchestration, ANSWER)
        assert isinstance(value, dict) and value["content"] == CSV, value
        assert warnings == []


class TestEveryReaderOfAToolsAnswer:
    def test_a_loop_over_it_reads_it_as_a_list(self):
        each = {"id": "each", "type": "loop", "name": "Each", "does": "For each", "loop_over": "read_csv",
                "body": ["say"], "next": "end"}
        say = {"id": "say", "type": "print", "name": "Say", "does": "Says", "prompt": "{{ state.item }}"}
        assert _schema(_compiled([{**READ, "next": "each"}, each, say]))["read_csv"]["type"] == "list"

    def test_a_condition_on_its_field_reads_it_as_an_object(self):
        check = {"id": "check", "type": "if_else", "name": "Check", "does": "Checks",
                 "condition": "state.read_csv.content != ''", "branches": {"true": "end", "false": "end"}}
        assert _schema(_compiled([{**READ, "next": "check"}, check]))["read_csv"]["type"] == "dict"

    @pytest.mark.parametrize("code, kind", [
        ("result = len(state['read_csv'])", "list"),
        ("result = state['read_csv'].get('content', '')", "dict"),
    ])
    def test_a_transform_reads_it_as_what_its_code_does_with_it(self, code, kind):
        assert _schema(_compiled([READ, _transform(code)]))["read_csv"]["type"] == kind


class TestWhatIsLeftAsItWas:
    def test_a_workflow_saved_without_a_declaration_reads_the_tools_text_as_text(self):
        """The engine parses only a declared key: a workflow saved before this, or by a person, is as it was."""
        stored = {"id": "orch_old", "name": "Old", "entry_step_id": "read_csv", "steps": [
            {"id": "read_csv", "name": "Read", "type": "tool", "forced_tool": "vault_read", "output_key": "read_csv",
             "next_step_id": "parse_sales"},
            {"id": "parse_sales", "name": "Parse", "type": "transform", "transform_code": CAP04_CODE,
             "output_key": "parse_sales", "next_step_id": None}]}
        value, warnings, _ = _typed(stored, ANSWER)
        assert value == ANSWER and warnings == []

    def test_a_transform_that_loads_the_json_itself_is_not_declared(self):
        code = "import json\ndata = json.loads(state['read_csv'])\nresult = data['content']"
        assert "read_csv" not in _schema(_compiled([READ, _transform(code)]))

    def test_an_answer_that_is_not_json_is_kept_with_the_engines_warning_and_the_run_goes_on(self):
        compiled = _compiled([READ, _transform(CAP04_CODE)])
        value, warnings, orch = _typed(compiled.orchestration, "No file at vault/cap3/sales.csv")
        assert value == "No file at vault/cap3/sales.csv"
        assert len(warnings) == 1 and "read_csv" in json.dumps(warnings), warnings
        assert orch.strict_state_types is False, "only a workflow that asks for strict types stops on it"

    def test_a_transforms_own_answer_is_data_already(self):
        first = {**_transform("result = {'content': 'x'}", "shape"), "next": "parse_sales"}
        assert "shape" not in _schema(_compiled([first, _transform("result = state['shape']['content']")]))

    def test_the_plans_own_declaration_is_kept(self):
        mine = {"read_csv": {"type": "dict", "description": "The CSV file vault_read answers"}}
        compiled = _compiled([READ, _transform(CAP04_CODE)], settings={"state_schema": mine})
        assert _schema(compiled)["read_csv"] == mine["read_csv"]
