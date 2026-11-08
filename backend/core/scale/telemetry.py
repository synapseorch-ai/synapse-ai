"""Kept for callers of the scale layer's old entry point; see `core.telemetry`.

`setup_telemetry()` used to install a tracer the moment `OTLP_ENDPOINT` was
set, after the app was built (so no request was ever traced) and in the API
only. Telemetry now lives in `core.telemetry`, with an explicit switch, a
content guard and the worker set up too.
"""
from __future__ import annotations

from typing import Optional


def setup_telemetry(service_name: str, otlp_endpoint: Optional[str] = None) -> bool:
    from core import telemetry
    return telemetry.setup(service_name, saved_endpoint=otlp_endpoint)


def get_tracer(name: str = "synapse.scale"):
    """A tracer: the real one when telemetry is on, OpenTelemetry's no-op
    otherwise (or a local stand-in if the package is not installed)."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
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
