"""One trace per run, with the engine's shape in it and none of its words.

A run with an agent step that calls a tool and then answers, followed by a
step that runs a whole workflow (a nested run), is one trace: the run, each
step, the agent step, the tool call, and the nested run under the step that
started it. A model call through the real `generate_response_ex` is a span
with its tokens and cost. The marker SECRET is in the input, the tool's
arguments, the model's replies and the prompt; it is in no span.
"""
from types import SimpleNamespace

import pytest

import core.llm_providers as llm
from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.telemetry import telemetry_off, telemetry_on  # noqa: F401  (fixtures)

SECRET = "SECRET-4417"
AGENT, FLOW_AGENT = "o1_agent", "o1_flow_agent"
PARENT, CHILD = "orch_o1_parent", "orch_o1_child"


def _text(span) -> str:
    parts = [span.name, repr(dict(span.attributes or {})), repr(span.status.description),
             repr(dict(span.resource.attributes))]
    for event in span.events:
        parts += [event.name, repr(dict(event.attributes or {}))]
    return " ".join(parts)


def _server():
    return SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch: dict, run_id: str):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import stream_engine_events
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [event async for event in stream_engine_events(
        engine.run(f"Look into {SECRET}", run_id=run_id), run_id)]


async def _seed():
    await S.seed_agents([
        S.make_agent(id=AGENT, tools=["all"], max_turns=5),
        S.make_orchestrator_agent(CHILD, id=FLOW_AGENT),
    ])
    await S.seed_orchestrations([S.make_orchestration(
        id=CHILD, entry_step_id="sum",
        steps=[{"id": "sum", "name": "Summarise", "type": "llm",
                "prompt_template": "Summarise {state.user_input}", "output_key": "summary",
                "next_step_id": None}])])
    return S.make_orchestration(
        id=PARENT, entry_step_id="work",
        steps=[
            {"id": "work", "name": "Work", "type": "agent", "agent_id": AGENT,
             "prompt_template": "Work on {state.user_input}", "output_key": "work",
             "max_turns": 5, "next_step_id": "nest"},
            {"id": "nest", "name": "Nest", "type": "agent", "agent_id": FLOW_AGENT,
             "prompt_template": "Run the flow on {state.work}", "output_key": "nested",
             "next_step_id": None},
        ])


async def test_a_nested_run_is_one_trace_with_no_words_in_it(telemetry_on, fake_llm):
    orch = await _seed()
    fake_llm.script([tool_call("o1_lookup", q=SECRET), f"The answer is {SECRET}",
                     f"Summary of {SECRET}"])
    events = await _run(orch, "run_o1_trace")
    assert any(e.get("type") == "orchestration_complete" for e in events)

    spans = telemetry_on.spans()
    for span in spans:
        assert SECRET not in _text(span), span.name
    assert len({s.context.trace_id for s in spans}) == 1, "one trace"

    by_id = {s.context.span_id: s for s in spans}

    def parent(span):
        return by_id.get(span.parent.span_id) if span.parent else None

    runs = [s for s in spans if s.name == "synapse.run"]
    root = next(s for s in runs if s.attributes["synapse.run_id"] == "run_o1_trace")
    nested = next(s for s in runs if s is not root)
    assert nested.attributes["synapse.root_run_id"] == "run_o1_trace"
    assert root.attributes["synapse.status"] == "completed"

    steps = {s.attributes["synapse.step.id"]: s for s in spans
             if s.name == "synapse.step" and s.attributes.get("synapse.run_id") == "run_o1_trace"}
    assert parent(steps["work"]) is root and parent(steps["nest"]) is root
    assert steps["work"].attributes["synapse.step.type"] == "agent"

    # The nested run hangs under the step that started it.
    ancestor, chain = parent(nested), []
    while ancestor is not None:
        chain.append(ancestor)
        ancestor = parent(ancestor)
    assert steps["nest"] in chain

    (tool,) = [s for s in spans if s.name == "synapse.tool"]
    assert tool.attributes["synapse.tool.name"] == "o1_lookup"
    agent_step = parent(tool)
    assert agent_step.name == "synapse.agent_step" and parent(agent_step) is steps["work"]


def _anthropic(text, *, fail_first=None):
    usage = SimpleNamespace(input_tokens=12, output_tokens=7,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    block = SimpleNamespace(type="text", text=text, name=None, input=None, thinking=None)
    response = SimpleNamespace(content=[block], usage=usage, stop_reason="end_turn")
    state = {"calls": 0}

    class _Messages:
        async def create(self, **_kwargs):
            state["calls"] += 1
            if fail_first is not None and state["calls"] == 1:
                raise fail_first
            return response

    class _Client:
        def __init__(self, *_a, **_kw):
            self.messages = _Messages()

    return _Client


@pytest.fixture
def no_backoff(monkeypatch):
    import asyncio
    real = asyncio.sleep

    async def instant(_delay, *args, **kwargs):
        await real(0)

    monkeypatch.setattr(llm.asyncio, "sleep", instant)


async def test_a_model_call_is_a_span_with_its_usage_and_no_words(telemetry_on, monkeypatch, no_backoff):
    import anthropic
    import httpx
    overloaded = anthropic.InternalServerError(
        f"overloaded {SECRET}", body=None,
        response=httpx.Response(529, request=httpx.Request("POST", "https://api.anthropic.com")))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _anthropic(f"Reply {SECRET}", fail_first=overloaded))

    await llm.generate_response_ex(
        f"Question {SECRET}", f"System {SECRET}", "cloud", "claude-sonnet-4-5",
        {"anthropic_key": "k"}, session_id="sess_o1", agent_id="agent_o1", source="chat")

    (span,) = [s for s in telemetry_on.spans() if s.name == "gen_ai.chat"]
    assert SECRET not in _text(span)
    assert span.attributes["gen_ai.usage.input_tokens"] == 12
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert span.attributes["gen_ai.provider.name"] == "anthropic"
    assert span.attributes["synapse.model.bucket"] == "own-key:anthropic"
    assert span.attributes["synapse.session_id"] == "sess_o1"
    assert "synapse.cost_usd" in span.attributes
    assert [e.attributes["synapse.retry.attempt"] for e in span.events if e.name == "synapse.retry"] == [1]


async def test_off_the_engine_runs_the_same_generators(telemetry_off, fake_llm):
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.steps import STEP_EXECUTORS
    from core.models_orchestration import Orchestration
    from core import telemetry
    telemetry.setup("synapse-test")
    orch = await _seed()
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    assert engine.executors is STEP_EXECUTORS


async def test_a_paused_run_ends_its_spans_and_detaches_cleanly(telemetry_on, fake_llm, caplog):
    import gc
    import logging

    orch = S.make_orchestration(
        id="orch_o1_pause", entry_step_id="ask",
        steps=[{"id": "ask", "name": "Ask", "type": "human",
                "human_prompt": f"Approve {SECRET}?", "output_key": "answer", "next_step_id": None}])
    with caplog.at_level(logging.ERROR, logger="opentelemetry.context"):
        events = await _run(orch, "run_o1_pause")
        gc.collect()
    assert any(e.get("type") == "human_input_required" for e in events)
    assert "Failed to detach context" not in caplog.text

    spans = telemetry_on.spans()
    run = next(s for s in spans if s.name == "synapse.run")
    step = next(s for s in spans if s.name == "synapse.step")
    assert run.attributes["synapse.status"] == "paused"
    assert step.attributes["synapse.status"] == "paused"
    assert step.parent.span_id == run.context.span_id
    for span in spans:
        assert SECRET not in _text(span)
