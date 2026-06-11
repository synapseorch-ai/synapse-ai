"""
OpenTelemetry setup for the scale layer.
No-op when OTLP_ENDPOINT is not configured (standalone / scale mode without observability).

Instruments:
  - FastAPI (all HTTP requests get traces automatically)
  - asyncpg (all DB queries get child spans)
  - Redis (all Redis ops get child spans)

Usage:
    from core.scale.telemetry import setup_telemetry, get_tracer
    setup_telemetry("synapse-api", otlp_endpoint="http://jaeger:4317")
    tracer = get_tracer()
    with tracer.start_as_current_span("my.operation") as span:
        span.set_attribute("run_id", run_id)
"""
import os
from typing import Optional


_tracer = None


def setup_telemetry(service_name: str, otlp_endpoint: Optional[str] = None) -> None:
    """Initialize OpenTelemetry SDK. Safe to call multiple times — only the first call takes effect."""
    global _tracer

    endpoint = otlp_endpoint or os.getenv("OTLP_ENDPOINT", "")
    if not endpoint:
        return  # Observability disabled

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Auto-instrument HTTP, DB, and Redis
        _instrument_libraries()

        _tracer = trace.get_tracer(service_name)
        print(f"[telemetry] OpenTelemetry enabled → {endpoint}", flush=True)

    except ImportError:
        print("[telemetry] opentelemetry packages not installed — tracing disabled", flush=True)
    except Exception as e:
        print(f"[telemetry] Setup failed: {e}", flush=True)


def _instrument_libraries() -> None:
    """Apply auto-instrumentation to supported libraries."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor().instrument()
    except (ImportError, Exception):
        pass

    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        AsyncPGInstrumentor().instrument()
    except (ImportError, Exception):
        pass

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    except (ImportError, Exception):
        pass


def get_tracer():
    """Return the configured tracer, or a no-op tracer if telemetry is disabled."""
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace
        return trace.get_tracer("synapse.scale")
    except ImportError:
        return _NoOpTracer()


class _NoOpSpan:
    def set_attribute(self, *a, **kw): pass
    def record_exception(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _NoOpTracer:
    def start_as_current_span(self, name, **kw):
        return _NoOpSpan()
    def start_span(self, name, **kw):
        return _NoOpSpan()
