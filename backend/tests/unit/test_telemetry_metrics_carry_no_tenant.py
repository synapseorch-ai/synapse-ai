"""Metrics: low-cardinality dimensions only, and only the reviewed instruments.

On Cloud every distinct value of a metric dimension is a metric of its own
(1,000 orgs × a few metrics is a four-figure monthly bill, D145), and a tenant
belongs on spans and log lines. So a run's metrics carry a step type, a status,
a job name, a tool category, a model bucket — never an org, run, session or
orchestration id, and never a user-typed model name.
"""
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.telemetry import clean_env, shutdown_providers, uninstrument
from core.telemetry import attributes as A

IDS = {"synapse.run_id", "synapse.session_id", "synapse.org_id", "synapse.orchestration_id",
       "synapse.root_run_id", "tenant_id", "orch_id", "synapse.agent_id"}


@pytest.fixture
def metered(monkeypatch):
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from core import telemetry
    from core.telemetry.pipeline import reset_for_tests

    clean_env(monkeypatch)
    reset_for_tests()
    reader = InMemoryMetricReader()
    assert telemetry.setup("synapse-test", span_exporter=InMemorySpanExporter(), metric_reader=reader)
    yield reader
    shutdown_providers()
    uninstrument()
    reset_for_tests()


def _points(reader) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    data = reader.get_metrics_data()
    for resource_metrics in data.resource_metrics if data else []:
        for scope in resource_metrics.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    out.setdefault(metric.name, []).append(dict(point.attributes))
    return out


async def test_a_run_is_measured_by_type_and_status_never_by_id(metered, fake_llm):
    from types import SimpleNamespace

    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import stream_engine_events

    await S.seed_agents([S.make_agent(id="m_agent", tools=["all"], max_turns=4)])
    orch = S.make_orchestration(id="orch_m", entry_step_id="work", steps=[
        {"id": "work", "name": "Work", "type": "agent", "agent_id": "m_agent",
         "prompt_template": "{state.user_input}", "output_key": "out", "next_step_id": None}])
    fake_llm.script([tool_call("m_tool", x=1), "done"])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    [e async for e in stream_engine_events(engine.run("go", run_id="run_m1"), "run_m1")]

    points = _points(metered)
    assert points["synapse.runs.completed"] == [{A.STATUS: "completed"}]
    assert {A.STEP_TYPE: "agent", A.STATUS: "completed"} in points["synapse.step.duration"]
    assert points["synapse.tool.calls"] == [{A.TOOL_CATEGORY: "none", "outcome": "error"}]
    for name, attribute_sets in points.items():
        for attributes in attribute_sets:
            assert not IDS & set(attributes), (name, attributes)
            assert set(attributes) <= A.METRIC_ATTRIBUTES, (name, attributes)


def test_a_user_typed_model_is_measured_by_its_provider(metered):
    from core.telemetry import metrics, spans
    metrics.model_call(spans.model_bucket("my-company-finetune-v7", "OpenAI"), "ok", 0.4)
    metrics.model_call(spans.model_bucket("synapse.nova-pro", "bedrock"), "ok", 0.4)
    buckets = {p[A.MODEL_BUCKET] for p in _points(metered)["synapse.model.calls"]}
    assert buckets == {"own-key:openai", "synapse.nova-pro"}


def test_an_unreviewed_instrument_and_an_undeclared_dimension_are_dropped(metered):
    from opentelemetry import metrics as otel_metrics
    meter = otel_metrics.get_meter("some.library")
    meter.create_histogram("http.client.duration").record(0.2, {"server.address": "customer.example"})
    otel_metrics.get_meter("synapse.engine").create_counter("synapse.o1.probe").add(
        1, {"synapse.run_id": "run_x", A.STATUS: "ok"})
    points = _points(metered)
    assert "http.client.duration" not in points
    assert points["synapse.o1.probe"] == [{A.STATUS: "ok"}]


async def test_the_metrics_route_serves_with_only_a_token_set(monkeypatch, client):
    from core import telemetry
    from core.telemetry import metrics
    from core.telemetry.pipeline import reset_for_tests

    clean_env(monkeypatch)
    reset_for_tests()
    monkeypatch.setenv("METRICS_TOKEN", "t0ken")
    try:
        assert telemetry.setup("synapse-api") is False, "no spans are exported"
        metrics.run_enqueued()
        response = await client.get("/api/v2/metrics", headers={"Authorization": "Bearer t0ken"})
        assert response.status_code == 200
        assert "synapse_runs_enqueued_total" in response.text
        assert "tenant_id" not in response.text
    finally:
        shutdown_providers()
        reset_for_tests()
