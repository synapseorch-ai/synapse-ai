"""The scale layer's metric helpers, kept for their callers; see `core.telemetry`.

They used to be `prometheus_client` metrics of their own, labelled with the
tenant and the orchestration — a series per tenant per orchestration — and only
one of them was ever recorded. The engine's metrics now live on the
OpenTelemetry API (`core/telemetry/metrics.py`), with low-cardinality
dimensions only; these names forward to it. `/api/v2/metrics` serves them
through OpenTelemetry's Prometheus reader when METRICS_TOKEN is set.
"""
from __future__ import annotations

from typing import Optional

from core.telemetry import metrics as _metrics


def get_metrics_response():
    """(content, content_type) for `/api/v2/metrics`, or (None, None) when the
    Prometheus exporter is not installed."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return None, None
    try:
        return generate_latest(), CONTENT_TYPE_LATEST
    except Exception:
        return None, None


def record_run_enqueued(tenant_id: str = "default", orch_id: str = "unknown") -> None:
    # The tenant and orchestration are no longer dimensions (see module doc).
    _metrics.run_enqueued()


def record_run_completed(tenant_id: str = "default", status: str = "completed",
                         duration_s: Optional[float] = None) -> None:
    _metrics.run_stretch(status, duration_s or 0.0)


def record_step_duration(step_type: str, duration_s: float) -> None:
    _metrics.step(step_type, None, duration_s)


def record_webhook(success: bool) -> None:
    _metrics.webhook(success)
