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


def instrument_engine(engine) -> None:
    """Trace a SQLAlchemy engine's statements once telemetry is on (a no-op off)."""
    if state.configured and not state.tracing:
        return
    from core.telemetry.pipeline import instrument_engine as _instrument
    _instrument(engine)
