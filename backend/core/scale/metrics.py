"""
Prometheus metrics for the scale layer.
No-op when prometheus_client is not installed.

Metrics exposed at GET /metrics (protected by METRICS_TOKEN env var).
"""
import os
from typing import Optional

_metrics_available = False

try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
    _metrics_available = True

    # --- Counters ---
    runs_enqueued = Counter(
        "synapse_runs_enqueued_total",
        "Total orchestration runs enqueued",
        ["tenant_id", "orch_id"],
    )
    runs_completed = Counter(
        "synapse_runs_completed_total",
        "Total orchestration runs completed",
        ["tenant_id", "status"],
    )
    chat_jobs_total = Counter(
        "synapse_chat_jobs_total",
        "Total agent chat jobs processed",
        ["tenant_id", "status"],
    )
    webhook_deliveries = Counter(
        "synapse_webhook_deliveries_total",
        "Total webhook delivery attempts",
        ["status"],  # "success" | "failed"
    )

    # --- Histograms ---
    run_duration = Histogram(
        "synapse_run_duration_seconds",
        "Orchestration run duration in seconds",
        ["tenant_id"],
        buckets=[1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600],
    )
    step_duration = Histogram(
        "synapse_step_duration_seconds",
        "Individual step duration in seconds",
        ["step_type"],
        buckets=[0.1, 0.5, 1, 5, 15, 30, 60, 120],
    )

    # --- Gauges ---
    queue_depth = Gauge(
        "synapse_queue_depth",
        "Current ARQ queue depth",
        ["queue_name"],
    )
    active_workers = Gauge(
        "synapse_active_workers",
        "Number of online workers",
    )
    active_runs = Gauge(
        "synapse_active_runs",
        "Number of currently running orchestrations",
    )

except ImportError:
    pass


def get_metrics_response():
    """Return (content, content_type) for the /metrics endpoint. Returns None if unavailable."""
    if not _metrics_available:
        return None, None
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return generate_latest(), CONTENT_TYPE_LATEST
    except Exception:
        return None, None


def record_run_enqueued(tenant_id: str = "default", orch_id: str = "unknown") -> None:
    if _metrics_available:
        try:
            runs_enqueued.labels(tenant_id=tenant_id, orch_id=orch_id).inc()
        except Exception:
            pass


def record_run_completed(tenant_id: str = "default", status: str = "completed", duration_s: Optional[float] = None) -> None:
    if _metrics_available:
        try:
            runs_completed.labels(tenant_id=tenant_id, status=status).inc()
            if duration_s is not None:
                run_duration.labels(tenant_id=tenant_id).observe(duration_s)
        except Exception:
            pass


def record_step_duration(step_type: str, duration_s: float) -> None:
    if _metrics_available:
        try:
            step_duration.labels(step_type=step_type).observe(duration_s)
        except Exception:
            pass


def record_webhook(success: bool) -> None:
    if _metrics_available:
        try:
            webhook_deliveries.labels(status="success" if success else "failed").inc()
        except Exception:
            pass
