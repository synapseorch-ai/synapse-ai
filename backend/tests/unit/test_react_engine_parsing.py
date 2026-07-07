"""Unit tests for the pure ReAct parsing helpers (no I/O, no LLM)."""
import pytest


class TestToolCallParsing:
    def test_bare_json_tool_call(self):
        from core.react_engine import parse_tool_call
        call, _ = parse_tool_call('{"tool": "get_weather", "args": {"city": "NYC"}}')
        assert call["tool"] == "get_weather"
        assert call["args"] == {"city": "NYC"}

    def test_tool_call_after_reasoning_preamble(self):
        from core.react_engine import parse_tool_call
        out = 'I should look this up.\n{"tool": "search", "args": {"q": "x"}}'
        call, _ = parse_tool_call(out)
        assert call is not None
        assert call["tool"] == "search"

    def test_plain_prose_is_not_a_tool_call(self):
        from core.react_engine import parse_tool_call
        call, _ = parse_tool_call("The answer is 42.")
        assert call is None

    def test_non_tool_json_halts_scan(self):
        from core.react_engine import parse_tool_call
        # A JSON object without a `tool` key (e.g. an echoed result) is not a call.
        call, _ = parse_tool_call('{"result": "done", "value": 3}')
        assert call is None

    def test_xml_wrapped_tool_call(self):
        from core.react_engine import parse_tool_call
        out = '<tool_call>{"tool": "run", "args": {}}</tool_call>'
        call, _ = parse_tool_call(out)
        assert call["tool"] == "run"

    def test_parse_all_returns_every_call_in_order(self):
        from core.react_engine import parse_all_tool_calls
        out = ('{"tool": "a", "args": {}}\n'
               '{"tool": "b", "args": {"x": 1}}')
        calls = parse_all_tool_calls(out)
        assert [c["tool"] for c in calls] == ["a", "b"]

    def test_is_tool_call_predicate(self):
        from core.react_engine import _is_tool_call
        assert _is_tool_call({"tool": "x"}) is True
        assert _is_tool_call({"tool": ""}) is False
        assert _is_tool_call({"notatool": "x"}) is False
        assert _is_tool_call("nope") is False


class TestReasoningBlocks:
    def test_strip_reasoning_removes_block(self):
        from core.react_engine import strip_reasoning
        text = "[REASONING]internal thoughts[/REASONING]\nVisible answer"
        stripped = strip_reasoning(text)
        assert "internal thoughts" not in stripped
        assert "Visible answer" in stripped

    def test_extract_reasoning_returns_blocks(self):
        from core.react_engine import extract_reasoning
        text = "[REASONING]step one[/REASONING] answer [REASONING]step two[/REASONING]"
        blocks = extract_reasoning(text)
        assert any("step one" in b for b in blocks)
        assert any("step two" in b for b in blocks)

    def test_reasoning_keyword_inside_block_is_not_a_tool_call(self):
        from core.react_engine import parse_tool_call
        # A `tool` keyword hidden in reasoning prose must not be parsed as a call.
        text = '[REASONING]I could use a {"tool": "danger"}[/REASONING]\nFinal answer.'
        call, _ = parse_tool_call(text)
        assert call is None


class TestHeartbeat:
    async def test_iter_with_heartbeat_passes_events_through(self):
        from core.react_engine import iter_with_heartbeat

        async def _src():
            yield {"type": "status"}
            yield {"type": "final"}

        seen = [ev async for ev in iter_with_heartbeat(_src(), interval=100)]
        assert seen == [{"type": "status"}, {"type": "final"}]

    async def test_iter_with_heartbeat_emits_ping_on_idle(self):
        import asyncio
        from core.react_engine import iter_with_heartbeat, SSE_HEARTBEAT

        async def _slow():
            await asyncio.sleep(0.05)
            yield {"type": "final"}

        # interval below the source delay -> at least one heartbeat comment.
        seen = [ev async for ev in iter_with_heartbeat(_slow(), interval=0.01)]
        assert SSE_HEARTBEAT in seen
        assert {"type": "final"} in seen
