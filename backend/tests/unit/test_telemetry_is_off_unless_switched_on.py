"""Nothing is exported unless a Synapse switch turns it on (Naveen, 2026-09-29).

The standard OTEL_* variables only tune an export Synapse's own switch started:
a machine that already sends another application's spans to a collector must
not start sending Synapse's there. Off installs nothing at all.
"""
import logging
import sys

from _fakes.telemetry import telemetry_off, telemetry_on  # noqa: F401  (fixtures)
from core import telemetry
from core.telemetry import config


def _proxy_provider() -> bool:
    from opentelemetry import trace
    return isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)


def test_a_machine_with_only_the_standard_endpoint_set_exports_nothing(telemetry_off, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://someone-elses-collector:4317")
    assert telemetry.setup("synapse-api") is False
    assert _proxy_provider()
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    assert not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    assert telemetry.enabled() is False


def test_nothing_set_installs_nothing(telemetry_off):
    assert telemetry.setup("synapse-api") is False
    assert _proxy_provider()


def test_otlp_endpoint_turns_it_on(telemetry_off, monkeypatch):
    monkeypatch.setenv("OTLP_ENDPOINT", "http://collector:4317")
    cfg, warning = config.resolve()
    assert (cfg.endpoint, cfg.protocol, warning) == ("http://collector:4317", "grpc", None)


def test_the_scale_tabs_saved_endpoint_turns_it_on(telemetry_off):
    cfg, _ = config.resolve(saved_endpoint="http://saved:4317")
    assert cfg.endpoint == "http://saved:4317"


def test_the_synapse_switch_uses_the_standard_endpoint(telemetry_off, monkeypatch):
    monkeypatch.setenv("SYNAPSE_TELEMETRY", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    cfg, _ = config.resolve()
    assert (cfg.endpoint, cfg.protocol) == ("https://collector.example:4318", "http/protobuf")


def test_the_switch_with_no_collector_says_so_and_stays_off(telemetry_off, monkeypatch, caplog):
    monkeypatch.setenv("SYNAPSE_TELEMETRY", "otlp")
    with caplog.at_level(logging.WARNING, logger="core.telemetry"):
        assert telemetry.setup("synapse-api") is False
    assert _proxy_provider()
    assert "no collector is" in caplog.text


def test_off_and_sdk_disabled_win_over_an_endpoint(telemetry_off, monkeypatch):
    monkeypatch.setenv("OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("SYNAPSE_TELEMETRY", "off")
    assert config.resolve() == (None, None)
    monkeypatch.delenv("SYNAPSE_TELEMETRY")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert config.resolve() == (None, None)


def test_missing_packages_are_one_line_not_a_crash(telemetry_off, monkeypatch, caplog):
    monkeypatch.setenv("OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", None)
    with caplog.at_level(logging.WARNING, logger="core.telemetry"):
        assert telemetry.setup("synapse-api") is False
    assert "synapse-orch-ai[telemetry]" in caplog.text
    assert _proxy_provider()


def test_on_it_propagates_traceparent_and_never_baggage(telemetry_on):
    from opentelemetry import propagate
    assert set(propagate.get_global_textmap().fields) == {"traceparent", "tracestate"}
    assert telemetry.enabled() is True


def test_an_outbound_call_nobody_started_is_not_recorded(telemetry_on):
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind

    tracer = trace.get_tracer("opentelemetry.instrumentation.redis")
    orphan = tracer.start_span("ZRANGEBYSCORE", kind=SpanKind.CLIENT)
    assert not orphan.is_recording()
    orphan.end()
    with trace.get_tracer("synapse.test").start_as_current_span("job"):
        child = tracer.start_span("ZRANGEBYSCORE", kind=SpanKind.CLIENT)
        assert child.is_recording()
        child.end()
    assert telemetry_on.names() == ["ZRANGEBYSCORE", "job"]
