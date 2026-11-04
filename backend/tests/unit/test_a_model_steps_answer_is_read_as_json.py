"""A step that needs a list or an object reads a model step's answer as one (F1287, CAP3's pass 2; 79 11-02).

Brief 6's plan wired an agent to an Ingest step, and the Ingest failed: the agent's answer was JSON inside a sentence,
and `records_for` takes a list of objects, not text. The engine already parses a key the workflow declares `list` or
`dict` out of a model's text, the way `extract_json` does (whole text, a fenced block, matching braces), but nothing
declared it. Now compile does, for each step that reads an agent's or an llm step's answer as a structure:

* a Loop over it (a list; over a field of it, an object), an Ingest of its records (a list);
* a Transform that goes through it (a list: iterated, counted, indexed by number) or looks fields up in it (an
  object: indexed by name, `.get`, `.items`);
* an If/Else or Switch condition that reads a field of it (an object).

A key the plan declares itself is kept as it is; one it declares as text, which such a step cannot read, is said,
naming the step. What a Transform or Extract JSON step wrote is already data, and left alone; a Tool step's answer
is text too, and declared as a model's is (F1358, test_a_tools_json_answer_is_read_as_data.py).
"""
from __future__ import annotations

import pytest

from core.native_builder import objects as O
from core.native_builder.compile import compile_plan


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


def _compiled(steps, agents=(), settings=None):
    plan = O.Plan.from_draft({"kind": "create", "title": "T", "summary": "S", "steps": list(steps),
                              "agents": list(agents), **({"settings": settings} if settings else {})})
    return compile_plan(plan, mint=_mint())


def _schema(compiled) -> dict:
    return compiled.orchestration.get("state_schema") or {}


FINDER = {"name": "Lead finder", "purpose": "Finds leads", "tools": ["search_web"]}
FIND = {"id": "find", "type": "agent", "name": "Find", "does": "Finds leads", "agent": "Lead finder",
        "prompt": "Find this week's leads as a JSON list of objects with name and email.", "next": "keep"}
WRITE = {"id": "write", "type": "llm", "name": "Write", "does": "Writes", "prompt": "List three ideas as JSON.",
         "next": "each"}


class TestTheReaderTypesTheAnswer:
    def test_an_ingest_of_an_agents_answer_reads_it_as_a_list(self):
        keep = {"id": "keep", "type": "ingest", "name": "Keep", "does": "Keeps them",
                "config": {"ingest": {"kb_id": "kb_leads", "records_from": "find"}}, "next": "end"}
        compiled = _compiled([FIND, keep], [FINDER])
        assert _schema(compiled)["find"]["type"] == "list", _schema(compiled)

    def test_a_loop_over_an_llm_steps_answer_reads_it_as_a_list(self):
        each = {"id": "each", "type": "loop", "name": "Each", "does": "For each", "loop_over": "write",
                "body": ["say"], "next": "end"}
        say = {"id": "say", "type": "print", "name": "Say", "does": "Says", "prompt": "{{ state.item }}"}
        assert _schema(_compiled([WRITE, each, say]))["write"]["type"] == "list"

    @pytest.mark.parametrize("code, kind", [
        ("result = len(state.write)", "list"),
        ("result = [i['title'] for i in state.write]", "list"),
        ("first = state.write\nresult = first[0]", "list"),
        ("result = state.write['title']", "dict"),
        ("result = state.write.get('title', '')", "dict"),
        ("result = {k: v for k, v in state.write.items()}", "dict"),
    ])
    def test_a_transform_reads_it_as_what_its_code_does_with_it(self, code, kind):
        sum_up = {"id": "sum_up", "type": "transform", "name": "Sum up", "does": "Sums",
                  "config": {"transform_code": code}, "next": "end"}
        assert _schema(_compiled([{**WRITE, "next": "sum_up"}, sum_up]))["write"]["type"] == kind

    def test_a_condition_on_a_field_reads_it_as_an_object(self):
        check = {"id": "check", "type": "if_else", "name": "Check", "does": "Checks",
                 "condition": "state.write.score > 5", "branches": {"true": "end", "false": "end"}}
        assert _schema(_compiled([{**WRITE, "next": "check"}, check]))["write"]["type"] == "dict"


class TestWhatIsLeftAlone:
    def test_what_a_transform_step_wrote_is_data_already(self):
        """A Tool step was here too, until cap-04 showed its answer is the tool's text (F1358)."""
        read = {"id": "read", "type": "transform", "name": "Read", "does": "Reads",
                "config": {"transform_code": "result = [1, 2]"}, "next": "each"}
        each = {"id": "each", "type": "loop", "name": "Each", "does": "For each", "loop_over": "read",
                "body": ["say"], "next": "end"}
        say = {"id": "say", "type": "print", "name": "Say", "does": "Says", "prompt": "{{ state.item }}"}
        assert "read" not in _schema(_compiled([read, each, say]))

    def test_a_plain_answer_read_as_text_is_left_as_text(self):
        say = {"id": "say", "type": "print", "name": "Say", "does": "Says", "prompt": "{{ state.write }}"}
        assert "write" not in _schema(_compiled([{**WRITE, "next": "say"}, say]))

    def test_the_plans_own_declaration_is_kept(self):
        each = {"id": "each", "type": "loop", "name": "Each", "does": "For each", "loop_over": "write",
                "body": ["say"], "next": "end"}
        say = {"id": "say", "type": "print", "name": "Say", "does": "Says", "prompt": "{{ state.item }}"}
        mine = {"write": {"type": "list", "description": "Three ideas"}}
        assert _schema(_compiled([WRITE, each, say], settings={"state_schema": mine}))["write"] == mine["write"]

    def test_declared_as_text_is_said_naming_the_reader(self):
        keep = {"id": "keep", "type": "ingest", "name": "Keep", "does": "Keeps them",
                "config": {"ingest": {"kb_id": "kb_leads", "records_from": "find"}}, "next": "end"}
        compiled = _compiled([FIND, keep], [FINDER], settings={"state_schema": {"find": {"type": "str"}}})
        assert any("'keep'" in i and "'find'" in i and "text" in i for i in compiled.issues), compiled.issues


class TestTheEngineReadsItAsDeclared:
    def test_brief_6s_answer_reaches_the_ingest_as_records(self):
        """The agent's JSON inside a sentence, typed by the declaration compile made, is the records the Ingest
        writes (`_type_written_state`, the engine's own typing of what a step wrote)."""
        import types

        from core.knowledge.api_models import IngestStepConfig
        from core.knowledge.workflow_steps import records_for
        from core.models_orchestration import Orchestration, StepConfig
        from core.orchestration.engine import OrchestrationEngine

        keep = {"id": "keep", "type": "ingest", "name": "Keep", "does": "Keeps them",
                "config": {"ingest": {"kb_id": "kb_leads", "records_from": "find"}}, "next": "end"}
        compiled = _compiled([FIND, keep], [FINDER])
        engine = OrchestrationEngine(Orchestration.model_validate(compiled.orchestration),
                                     types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        answer = ("Here are this week's leads:\n```json\n[{\"name\": \"Ana\", \"email\": \"ana@example.com\"}, "
                  "{\"name\": \"Bo\", \"email\": \"bo@example.com\"}]\n```\nLet me know if you need more.")
        run = types.SimpleNamespace(shared_state={"find": answer})
        engine._type_written_state(run, StepConfig(id="find", name="Find", type="agent", output_key="find"), {})
        records = records_for(IngestStepConfig(kb_id="kb_leads", records_from="find"), run.shared_state["find"],
                              step_name="Keep")
        assert [r.json_ for r in records] == [{"name": "Ana", "email": "ana@example.com"},
                                              {"name": "Bo", "email": "bo@example.com"}]
