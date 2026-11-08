"""A tool call written with its arguments beside the tool name, not under `arguments`, runs with them (F758).

CLIM1's Phase 1 (2026-10-17): a Team lead on Claude Haiku (cli.claude.haiku) wrote its hand-offs as

    <tool_call>{"tool": "assign", "member": "Status cli_claude_haiku", "task": "Retrieve the status of A-2001 …"}</tool_call>

twice, both refused "Name the member: one of Status cli_claude_haiku, Shipping cli_claude_haiku" — the engine read
`arguments`, found none, and ran `assign` with nothing. The lead gave up. A model that writes its calls as text (every
CLI model, and any model without native tool calls) can write this shape; when a call has no `arguments`, the keys
beside `tool` are its arguments.
"""
import pytest

from core.react_engine import parse_all_tool_calls, parse_tool_call
from core.tool_call_shapes import calls_in

HAIKU = ('<tool_call>\n{"tool": "assign", "member": "Status cli_claude_haiku", '
         '"task": "Retrieve the status of order A-2001 using get_order."}\n</tool_call>')
FLAT = {"tool": "assign", "arguments": {"member": "Status cli_claude_haiku",
                                         "task": "Retrieve the status of order A-2001 using get_order."}}


def test_haikus_hand_off_carries_its_member_and_task():
    assert parse_all_tool_calls(HAIKU) == [FLAT]


def test_as_bare_json_too():
    text = '{"tool": "assign", "member": "Status cli_claude_haiku", "task": "Retrieve the status of order A-2001 using get_order."}'
    call, _ = parse_tool_call(text)
    assert call == FLAT


def test_two_flat_calls_in_one_reply_are_two_calls():
    text = ('<tool_call>{"tool": "get_order", "order_id": "A-1"}</tool_call>\n'
            '<tool_call>{"tool": "get_order", "order_id": "A-2"}</tool_call>')
    assert parse_all_tool_calls(text) == [{"tool": "get_order", "arguments": {"order_id": "A-1"}},
                                          {"tool": "get_order", "arguments": {"order_id": "A-2"}}]


@pytest.mark.parametrize("key", ["parameters", "args", "input"])
def test_arguments_under_another_common_name(key):
    assert calls_in({"tool": "get_order", key: {"order_id": "A-1"}}) == [
        {"tool": "get_order", "arguments": {"order_id": "A-1"}}]


def test_the_engines_own_shape_is_unchanged():
    call = {"tool": "get_order", "arguments": {"order_id": "A-1"}}
    assert calls_in(call) == [call]


def test_a_call_with_arguments_keeps_them_and_nothing_else_is_read_as_one():
    # `arguments` present: it is the arguments, whatever else is written beside it.
    assert calls_in({"tool": "get_order", "arguments": {"order_id": "A-1"}, "id": "c1"}) == [
        {"tool": "get_order", "arguments": {"order_id": "A-1"}, "id": "c1"}]


def test_a_call_with_no_arguments_at_all_is_left_as_written():
    assert calls_in({"tool": "list_orders"}) == [{"tool": "list_orders"}]


def test_a_flat_parameters_value_that_is_not_an_object_is_an_argument():
    # `{"tool": "search", "input": "red shoes"}` — a tool whose own argument is called input.
    assert calls_in({"tool": "search", "input": "red shoes"}) == [
        {"tool": "search", "arguments": {"input": "red shoes"}}]
