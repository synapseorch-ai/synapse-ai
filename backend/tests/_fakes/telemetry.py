"""A telemetry pipeline a test can read: setup() with an in-memory exporter.

    from _fakes.telemetry import telemetry_on   # a fixture
    def test_x(telemetry_on):
        ...
        spans = telemetry_on.spans()

Every test gets a clean process: no provider, no instrumentation, and none of
the switch variables from the developer's shell — and leaves it that way.
"""
from __future__ import annotations

import pytest

SWITCH_VARS = (
    "SYNAPSE_TELEMETRY", "OTLP_ENDPOINT", "METRICS_TOKEN", "OTEL_SDK_DISABLED",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_SERVICE_NAME",
    "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG", "OTEL_SEMCONV_STABILITY_OPT_IN",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SYNAPSE_RELEASE",
    "SYNAPSE_TELEMETRY_UNTRUSTED_PATHS", "SYNAPSE_TELEMETRY_EXCLUDED_PATHS",
    "SYNAPSE_TELEMETRY_INTERNAL_HOSTS",
)


def clean_env(monkeypatch) -> None:
    for name in SWITCH_VARS:
        # setenv first so monkeypatch records the original and restores it —
        # setup() writes two of these with os.environ.setdefault.
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


def uninstrument() -> None:
    for module, cls in (("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
                        ("opentelemetry.instrumentation.redis", "RedisInstrumentor")):
        try:
            instrumentor = getattr(__import__(module, fromlist=[cls]), cls)()
        except ImportError:
            continue
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def shutdown_providers() -> None:
    """Shut down whatever a test installed — a meter provider's Prometheus
    reader is registered in prometheus_client's global registry until then."""
    from opentelemetry import metrics, trace
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            provider.shutdown()


class Pipeline:
    def __init__(self, exporter):
        self.exporter = exporter

    def spans(self):
        from opentelemetry import trace
        trace.get_tracer_provider().force_flush()
        return list(self.exporter.get_finished_spans())

    def names(self):
        return [s.name for s in self.spans()]


@pytest.fixture
def telemetry_off(monkeypatch):
    from core.telemetry.pipeline import reset_for_tests
    clean_env(monkeypatch)
    reset_for_tests()
    yield
    shutdown_providers()
    uninstrument()
    reset_for_tests()


@pytest.fixture
def telemetry_on(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from core import telemetry
    from core.telemetry.pipeline import reset_for_tests

    clean_env(monkeypatch)
    reset_for_tests()
    exporter = InMemorySpanExporter()
    assert telemetry.setup("synapse-test", span_exporter=exporter)
    yield Pipeline(exporter)
    shutdown_providers()
    uninstrument()
    reset_for_tests()
