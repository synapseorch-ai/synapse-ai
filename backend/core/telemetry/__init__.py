"""OpenTelemetry for Synapse — off unless the operator turns it on.

Nothing is sent to us, ever. An operator can export traces and metrics to a
collector of their own (`config.py` lists the switches); off, this package
installs nothing and the engine's span helpers cost a flag check.

What a span may carry is `attributes.py`: ids, timings, counts, cost, step
types and tool names — never a prompt, an answer, a tool's arguments or
result, or an error message. `guard.py` enforces it on every span.

Importing this package does not import OpenTelemetry.
"""
from __future__ import annotations

from core.telemetry import state


def enabled() -> bool:
    """Spans are being exported from this process."""
    return state.tracing


def setup(service_name: str, **kwargs) -> bool:
    from core.telemetry.pipeline import setup as _setup
    return _setup(service_name, **kwargs)


def fastapi_options() -> dict:
    """`FastAPI(**telemetry.fastapi_options())`: FastAPI's own telemetry, off.

    FastAPI 0.142 added native telemetry that, at startup, adds OTLP exporters
    for traces, metrics and logs whenever an `OTEL_EXPORTER_OTLP_*ENDPOINT` is
    set — on a machine that exports another application's telemetry, it would
    start sending Synapse's, past the content guard (or fail the app's startup
    when its extra is missing) — and that emits its own server spans once a
    provider exists. Synapse's switch and middleware do this job, so FastAPI's
    are turned off. FastAPI before 0.142 has no such argument and gets none.
    """
    import inspect

    from fastapi import FastAPI

    if "telemetry" not in inspect.signature(FastAPI.__init__).parameters:
        return {}
    return {"telemetry": {"auto_configure": False, "tracing": False, "metrics": False,
                          "logs": False}}


def detached_context():
    """A copy of the current context with no current span and no log ids.

    For a task that outlives the request or job that spawns it — a pooled MCP
    connection's supervisor lives for minutes and serves many jobs — so its
    calls do not join the first job's trace or carry its run id.
    """
    import contextvars

    from core.telemetry import logcontext

    context = contextvars.copy_context()

    def clear():
        logcontext.clear()
        if state.tracing:
            from opentelemetry import context as otel_context
            otel_context.attach(otel_context.Context())

    context.run(clear)
    return context


def instrument_engine(engine) -> None:
    """Trace a SQLAlchemy engine's statements once telemetry is on (a no-op off)."""
    if state.configured and not state.tracing:
        return
    from core.telemetry.pipeline import instrument_engine as _instrument
    _instrument(engine)
