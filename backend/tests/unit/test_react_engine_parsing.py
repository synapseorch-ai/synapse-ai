"""Unit tests for the pure ReAct parsing helpers (no I/O, no LLM)."""
import pytest


class TestToolCallParsing:
    def test_bare_json_tool_call(self):
        from core.react_engine import parse_tool_call
        call, _ = parse_tool_call('{"tool": "get_weather", "args": {"city": "NYC"}}')
        assert call["tool"] == "get_weather"
        # In the engine's shape since F758: the loop runs a call from its
        # `arguments`, so `args` passed through as written ran with none.
        assert call["arguments"] == {"city": "NYC"}

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

    async def test_iter_with_heartbeat_survives_cancel_scope_across_yield(self):
        """Regression for #356: a source holding an anyio cancel scope across a
        yield must not be advanced by a fresh task per pull, or exiting the
        scope raises "cancel scope in a different task"."""
        import anyio
        from core.react_engine import iter_with_heartbeat

        async def _src():
            with anyio.fail_after(30):
                yield {"type": "step_start"}
                yield {"type": "final"}
            yield {"type": "orchestration_complete"}

        seen = [ev async for ev in iter_with_heartbeat(_src(), interval=100)]
        assert [e["type"] for e in seen] == [
            "step_start", "final", "orchestration_complete",
        ]

    async def test_iter_with_heartbeat_closes_source_on_early_exit(self):
        """Closing the wrapper from another task — what Python's GC finalizer
        does to an abandoned async generator — must still close a
        scope-holding source without a cross-task cancel-scope error."""
        import asyncio
        import anyio
        from core.react_engine import iter_with_heartbeat

        closed, teardown_errors = [], []

        async def _src():
            try:
                with anyio.fail_after(30):
                    yield {"type": "step_start"}
                    yield {"type": "final"}
            except BaseException as exc:
                teardown_errors.append(exc)
                raise
            finally:
                closed.append(True)

        stream = iter_with_heartbeat(_src(), interval=100)
        assert await stream.__anext__() == {"type": "step_start"}

        await asyncio.ensure_future(stream.aclose())  # client disconnected

        assert closed == [True]
        # Closing must unwind cleanly via GeneratorExit. Closing the source from
        # the consumer's task instead made exiting its cancel scope raise
        # RuntimeError here, which the old wrapper then swallowed.
        assert all(isinstance(e, GeneratorExit) for e in teardown_errors), teardown_errors

    async def test_iter_with_heartbeat_propagates_source_exception(self):
        import pytest
        from core.react_engine import iter_with_heartbeat

        async def _src():
            yield {"type": "status"}
            raise ValueError("boom")

        seen = []
        with pytest.raises(ValueError, match="boom"):
            async for ev in iter_with_heartbeat(_src(), interval=100):
                seen.append(ev)
        assert seen == [{"type": "status"}]
