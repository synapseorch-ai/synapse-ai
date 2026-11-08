"""An agent step's execution memory records the tools it called (F81, D104).

`build_execution_trace` looked for `tool_call` events carrying `tool_input`, and
`result`/`output` on the result. The agent loop has always emitted
`tool_execution` with `args`, and `tool_result` with `preview` — so every agent
step stored no tool calls. A re-run was told "No tools were used", and the eval
judge's account of what each step did showed none.
"""
from core.orchestration.context import build_execution_trace
from core.react_engine import run_agent_step  # noqa: F401 — the emitter named below


def test_the_agent_loops_own_events_are_recorded():
    trace = build_execution_trace([
        {"type": "thinking", "message": "…"},
        {"type": "tool_execution", "tool_name": "lookup_order", "args": {"id": "A-17"}},
        {"type": "tool_result", "tool_name": "lookup_order", "preview": '{"status": "shipped"}'},
        {"type": "final", "response": "It shipped."},
    ])
    assert trace["tools_used"] == ["lookup_order"]
    assert trace["tool_calls"] == [{
        "name": "lookup_order", "args": {"id": "A-17"}, "result_preview": '{"status": "shipped"}',
    }]


def test_the_shape_tool_steps_synthesise_still_works():
    trace = build_execution_trace([
        {"type": "tool_call", "tool_name": "t", "tool_input": {"a": 1}},
        {"type": "tool_result", "result": "ok"},
    ])
    assert trace["tool_calls"] == [{"name": "t", "args": {"a": 1}, "result_preview": "ok"}]


def test_the_chat_webhook_summary_reads_the_args_the_loop_sends():
    """Same shape, OSS scale worker: it read `tool_args`, so every tool call in
    a chat webhook payload carried null arguments."""
    import inspect

    from core.scale import worker
    source = inspect.getsource(worker)
    assert '"args": event.get("tool_args")' not in source
