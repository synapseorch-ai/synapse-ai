"""A step that fails without a key is refused when a branch can reach it with the key unset (F1346; CAP3's cap-05 on
FF17; eb 11-04).

cap-05's build: get_order_id (an If/Else) → extract_webhook (a Transform) or ask_order_id (a Human step), then
normalize_order, a Transform whose code reads `state.extract_webhook`. Only the webhook branch writes it; the run
took the ask branch and failed, "no key 'extract_webhook' here", though validate_orchestration had passed it.

The builder's rule stands (native_builder's guide, U74): a prompt that reads a key no step has written yet reads it
empty, a loop's first pass may read before its producer, and both are said as warnings, never refused. What is
refused is a read that breaks without the key, on a way a run can take with it unset (`branch_keys._first_unwritten`):
a Transform's `state.key` or `state["key"]` (its state has no default for a missing key) and a Loop's `loop_over` list
(it runs no iteration, and the run reads Completed). Not when the code guards it (`state.get`, `"key" in state`), the
schema gives it a default, or it is the run's input. Said once, in `_validate_orchestration`'s issues: the plan check
(compile) and the builder's saves both read them. Each names the step, the key and the branch that misses it.
"""
from __future__ import annotations

import pytest

from core.builder_tools import _validate_orchestration
from core.native_builder import objects as O
from core.native_builder.compile import compile_plan

WEBHOOK = "result = {'order_id': '42'}"
NORMALIZE = "order = state.extract_webhook['order_id']\nresult = {'order_id': order}"


def _cap05(code: str = NORMALIZE, schema: dict | None = None, *, both: bool = False) -> dict:
    """cap-05's shape; with `both`, the ask branch writes extract_webhook too (a join both ways set)."""
    ask_next = "from_answer" if both else "normalize_order"
    steps = [
        {"id": "get_order_id", "name": "Get order id", "type": "if_else",
         "if_condition": "'order' in str(state.user_input)", "if_true_step_id": "extract_webhook",
         "if_false_step_id": "ask_order_id"},
        {"id": "extract_webhook", "name": "Extract webhook", "type": "transform", "transform_code": WEBHOOK,
         "output_key": "extract_webhook", "next_step_id": "normalize_order"},
        {"id": "ask_order_id", "name": "Ask order id", "type": "human", "human_prompt": "Which order is it?",
         "output_key": "ask_order_id", "next_step_id": ask_next},
        {"id": "normalize_order", "name": "Normalize order", "type": "transform", "transform_code": code,
         "output_key": "normalize_order", "next_step_id": "done"},
        {"id": "done", "name": "Done", "type": "end"},
    ]
    if both:
        steps.insert(3, {"id": "from_answer", "name": "From the answer", "type": "transform",
                         "transform_code": "result = {'order_id': str(state.ask_order_id)}",
                         "output_key": "extract_webhook", "next_step_id": "normalize_order"})
    return {"id": "orch_cap05", "name": "Refunds", "entry_step_id": "get_order_id", "steps": steps,
            "state_schema": schema or {}}


def _about(issues: list[str], key: str) -> list[str]:
    return [i for i in issues if f"state.{key}" in i]


class TestATransform:
    def test_cap_05s_join_is_refused_naming_the_step_the_key_and_the_branch(self):
        verdict = _validate_orchestration(_cap05())
        said = _about(verdict["issues"], "extract_webhook")
        assert len(said) == 1 and not verdict["ok"], verdict
        for words in ("normalize_order", "extract_webhook", "get_order_id", "false", "ask_order_id"):
            assert words in said[0], (words, said[0])

    def test_a_join_both_branches_set_passes(self):
        assert _about(_validate_orchestration(_cap05(both=True))["issues"], "extract_webhook") == []

    @pytest.mark.parametrize("code", [
        "hook = state.get('extract_webhook') or {}\nresult = {'order_id': hook.get('order_id')}",
        "if 'extract_webhook' in state:\n    result = state.extract_webhook\nelse:\n    result = {}",
        "result = state.ask_order_id if hasattr(state, 'ask_order_id') else None",
    ], ids=["get", "in-state", "hasattr"])
    def test_a_read_the_code_guards_passes(self, code):
        verdict = _validate_orchestration(_cap05(code))
        assert _about(verdict["issues"], "extract_webhook") == [] and _about(verdict["issues"], "ask_order_id") == []

    def test_a_default_in_the_schema_passes(self):
        schema = {"extract_webhook": {"type": "dict", "default": {}}}
        assert _about(_validate_orchestration(_cap05(schema=schema))["issues"], "extract_webhook") == []

    def test_the_runs_own_input_passes(self):
        """A key the schema declares and no step writes is the run's input, filled when it starts."""
        code = "result = {'order_id': state.order_ref}"
        assert _about(_validate_orchestration(_cap05(code, schema={"order_ref": {"type": "str"}}))["issues"],
                      "order_ref") == []

    def test_a_key_no_step_writes_is_refused(self):
        said = _about(_validate_orchestration(_cap05("result = state['webhook_body']"))["issues"], "webhook_body")
        assert len(said) == 1 and "no step writes" in said[0], said

    def test_a_prompt_reading_it_is_still_a_warning_never_refused(self):
        """The guide's rule (U74): a model's prompt reads an unset key empty."""
        orch = _cap05()
        orch["steps"][3] = {"id": "normalize_order", "name": "Normalize order", "type": "llm",
                            "prompt_template": "Normalize {{ state.extract_webhook }}.",
                            "output_key": "normalize_order", "next_step_id": "done"}
        verdict = _validate_orchestration(orch)
        assert _about(verdict["issues"], "extract_webhook") == []


def _loop(schema: dict | None = None, *, both: bool = False) -> dict:
    """A Loop over a list only one branch writes; with `both`, the other branch writes it too."""
    other = "list_nothing" if both else "each_order"
    steps = [
        {"id": "has_orders", "name": "Has orders", "type": "if_else", "if_condition": "bool(state.user_input)",
         "if_true_step_id": "fetch_orders", "if_false_step_id": other},
        {"id": "fetch_orders", "name": "Fetch orders", "type": "transform", "transform_code": "result = [1, 2]",
         "output_key": "fetch_orders", "next_step_id": "each_order"},
        {"id": "each_order", "name": "Each order", "type": "loop", "loop_over": "state.fetch_orders",
         "loop_step_ids": ["handle"], "next_step_id": "done"},
        {"id": "handle", "name": "Handle", "type": "transform", "transform_code": "result = state.loop_item",
         "output_key": "handle"},
        {"id": "done", "name": "Done", "type": "end"},
    ]
    if both:
        steps.insert(2, {"id": "list_nothing", "name": "List nothing", "type": "transform",
                         "transform_code": "result = []", "output_key": "fetch_orders", "next_step_id": "each_order"})
    return {"id": "orch_loop", "name": "Orders", "entry_step_id": "has_orders", "steps": steps,
            "state_schema": schema or {}}


class TestALoopsList:
    def test_a_loop_over_a_list_one_branch_leaves_unset_is_refused(self):
        said = _about(_validate_orchestration(_loop())["issues"], "fetch_orders")
        assert len(said) == 1, said
        for words in ("each_order", "fetch_orders", "has_orders", "false"):
            assert words in said[0], (words, said[0])

    @pytest.mark.parametrize("orch", [_loop(both=True),
                                      _loop(schema={"fetch_orders": {"type": "list", "default": []}})],
                             ids=["every-way-writes-it", "a-default"])
    def test_written_every_way_or_given_a_default_passes(self, orch):
        assert _about(_validate_orchestration(orch)["issues"], "fetch_orders") == []

    def test_the_loops_own_item_is_its_body_s(self):
        assert _about(_validate_orchestration(_loop(both=True))["issues"], "loop_item") == []


class TestThePlanCheck:
    def test_cap_05s_plan_is_sent_back_by_its_compile(self):
        plan = O.Plan.from_draft({"kind": "create", "title": "Refunds", "summary": "Refunds an order.", "steps": [
            {"id": "get_order_id", "type": "if_else", "name": "Get order id", "does": "Checks for a webhook",
             "condition": "'order' in str(state.user_input)",
             "branches": {"true": "extract_webhook", "false": "ask_order_id"}},
            {"id": "extract_webhook", "type": "transform", "name": "Extract webhook", "does": "Reads the webhook",
             "config": {"transform_code": WEBHOOK}, "next": "normalize_order"},
            {"id": "ask_order_id", "type": "human", "name": "Ask order id", "does": "Asks for the order id",
             "prompt": "Which order is it?", "next": "normalize_order"},
            {"id": "normalize_order", "type": "transform", "name": "Normalize order", "does": "Normalizes the order",
             "config": {"transform_code": NORMALIZE}, "next": "end"},
        ]})
        said = _about(compile_plan(plan).issues, "extract_webhook")
        assert len(said) == 1 and "normalize_order" in said[0] and "ask_order_id" in said[0], said
