"""A template that reads a field of a model's or a tool's answer declares that answer an object (F1370, cap-05; eb
11-04).

CAP3's cap set 3, cap-05: an llm step, parse_request, answered a fenced JSON object and then prose, and the next Tool
step set its argument as `{{state.parse_request.order_id}}`. The run failed: "state.parse_request.order_id is not in
the run's state". `render_tool_arguments` steps into a value held as JSON text only when the whole text is JSON, and
compile declared nothing, because it read a Transform's code and a condition for field reads (F1287, F1358), never a
template.

Now a `{{ state.X.field }}` (or the older `{state.X.field}`) in a step's set arguments or its prompt is read as F1358
reads `state.X.field` in code: X is declared an object when an agent, llm or Tool step writes it, a list when the
segment is a number, and the engine parses X's text into it before the next step reads it (whole text, a fenced
block, matching braces), so cap-05's argument renders. A plain `{{state.X}}` is not declared, nor what a Transform
wrote, nor a name only text has (`TEXT_METHODS`).
"""
from __future__ import annotations

import types

from core.native_builder import objects as O
from core.native_builder.compile import compile_plan

#: What parse_request answered in cap-05's run (ff18-c3-cli-claude-claude-haiku-4-5 records).
CAP05_ANSWER = ('```json\n{\n  "order_id": "SO-2077",\n  "amount": "£45.00",\n  "reason": "damaged in transit"\n}\n```'
                '\n\n**Parsing complete.** The refund request has been extracted:\n- **Order ID**: SO-2077\n'
                '- **Amount**: £45.00\n- **Reason**: Damaged in transit\n\nReady to proceed to the next step.')

PARSE = {"id": "parse_request", "type": "llm", "name": "Parse the request", "does": "Reads the refund request",
         "prompt": "Pull the order id, the amount and the reason out of: {{ state.user_input }}. Answer JSON.",
         "next": "lookup_order"}


def _lookup(argument: str = "{{state.parse_request.order_id}}") -> dict:
    return {"id": "lookup_order", "type": "tool", "name": "Look up order status",
            "does": "Retrieves the current status of the order from the Orders API.",
            "tool": "orders_api_getorderstatus", "config": {"tool_arguments": {"order_id": argument}}, "next": "end"}


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


def _compiled(steps, settings=None):
    plan = O.Plan.from_draft({"kind": "create", "title": "Refunds", "summary": "S", "steps": list(steps),
                              **({"settings": settings} if settings else {})})
    return compile_plan(plan, mint=_mint())


def _schema(compiled) -> dict:
    return compiled.orchestration.get("state_schema") or {}


def _say(sid: str, prompt: str) -> dict:
    return {"id": sid, "type": "llm", "name": "Write", "does": "Writes", "prompt": prompt, "next": "end"}


class TestCap05:
    def test_its_tool_arguments_field_read_declares_the_llm_answer_an_object(self):
        compiled = _compiled([PARSE, _lookup()])
        assert _schema(compiled).get("parse_request", {}).get("type") == "dict", _schema(compiled)

    def test_the_engine_hands_the_tool_its_order_id(self):
        from core.models_orchestration import Orchestration, StepConfig
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.steps import render_tool_arguments

        compiled = _compiled([PARSE, _lookup()])
        engine = OrchestrationEngine(Orchestration.model_validate(compiled.orchestration),
                                     types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
        run = types.SimpleNamespace(shared_state={"parse_request": CAP05_ANSWER})
        engine._type_written_state(run, StepConfig(id="parse_request", name="Parse", type="llm",
                                                   output_key="parse_request"), {})
        assert run.shared_state["parse_request"]["order_id"] == "SO-2077", run.shared_state
        step = next(s for s in compiled.orchestration["steps"] if s["id"] == "lookup_order")
        assert render_tool_arguments(step["tool_arguments"], run.shared_state) == {"order_id": "SO-2077"}


class TestEveryTemplate:
    def test_a_prompts_field_read_declares_it(self):
        steps = [{**PARSE, "next": "say"}, _say("say", "Refund {{ state.parse_request.amount }} now.")]
        assert _schema(_compiled(steps)).get("parse_request", {}).get("type") == "dict"

    def test_the_older_single_brace_form_declares_it(self):
        assert _schema(_compiled([PARSE, _lookup("{state.parse_request.order_id}")])).get(
            "parse_request", {}).get("type") == "dict"

    def test_a_numbered_segment_reads_it_as_a_list(self):
        steps = [{**PARSE, "next": "say"}, _say("say", "The first: {{ state.parse_request.0 }}")]
        assert _schema(_compiled(steps)).get("parse_request", {}).get("type") == "list"

    def test_a_tools_answer_read_by_its_field_is_declared_too(self):
        read = {"id": "read_csv", "type": "tool", "name": "Read", "does": "Reads", "tool": "vault_read",
                "next": "say"}
        steps = [read, _say("say", "Here it is: {{ state.read_csv.content }}")]
        assert _schema(_compiled(steps)).get("read_csv", {}).get("type") == "dict"


class TestWhatIsLeftAlone:
    def test_the_whole_answer_in_a_template_is_not_declared(self):
        steps = [{**PARSE, "next": "say"}, _say("say", "It said: {{ state.parse_request }}")]
        assert "parse_request" not in _schema(_compiled(steps))

    def test_a_transforms_answer_read_by_its_field_is_not_declared(self):
        shape = {"id": "shape", "type": "transform", "name": "Shape", "does": "Shapes",
                 "config": {"transform_code": "result = {'order_id': 'SO-1'}"}, "next": "lookup_order"}
        assert "shape" not in _schema(_compiled([shape, _lookup("{{state.shape.order_id}}")]))

    def test_the_plans_own_text_declaration_is_said_naming_the_reader(self):
        compiled = _compiled([PARSE, _lookup()], settings={"state_schema": {"parse_request": {"type": "str"}}})
        assert any("'lookup_order'" in i and "'parse_request'" in i for i in compiled.issues), compiled.issues
