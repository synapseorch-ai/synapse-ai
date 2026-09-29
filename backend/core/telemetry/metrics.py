"""The engine's metrics, on the OpenTelemetry metrics API.

Recorded where the engine's spans end (`spans.py`), the queue's envelope
(`queue.py`) and a few seams of their own. Each recorder is a no-op until
`setup()` installed a meter provider — for an OTLP collector, or for
`/api/v2/metrics` when METRICS_TOKEN is set.

Dimensions are low-cardinality only: a step type, a status, a job's function
name, a tool's category, a model bucket (`spans.model_bucket`). Never an org,
a run, a session or an orchestration: on Cloud each distinct value would be a
metric of its own, and the tenant belongs on spans and log lines, not here.
The meter provider's views (`pipeline.py`) drop any other attribute anyway.
"""
from __future__ import annotations

import threading

from core.telemetry import attributes as A
from core.telemetry import state

_lock = threading.Lock()
_instruments: dict | None = None
_queue_depth: dict[str, int] = {}
_active_runs = 0


def _get() -> dict | None:
    global _instruments
    if not state.metering:
        return None
    if _instruments is not None:
        return _instruments
    with _lock:
        if _instruments is None:
            from opentelemetry import metrics
            meter = metrics.get_meter("synapse.engine")
            _instruments = {
                "runs_enqueued": meter.create_counter(
                    "synapse.runs.enqueued", unit="{run}", description="Runs queued for a worker"),
                "runs_completed": meter.create_counter(
                    "synapse.runs.completed", unit="{run}", description="Runs that stopped, by status"),
                "run_duration": meter.create_histogram(
                    "synapse.run.duration", unit="s", description="Time a run worked, per stretch"),
                "step_duration": meter.create_histogram(
                    "synapse.step.duration", unit="s", description="Time a step ran, by type and status"),
                "chat_turns": meter.create_counter(
                    "synapse.chat.turns", unit="{turn}", description="Chat turns, by status"),
                "chat_turn_duration": meter.create_histogram(
                    "synapse.chat.turn.duration", unit="s", description="Time a chat turn took"),
                "model_calls": meter.create_counter(
                    "synapse.model.calls", unit="{call}", description="Model calls, by model and status"),
                "model_duration": meter.create_histogram(
                    "synapse.model.duration", unit="s", description="Model call latency"),
                "model_tokens": meter.create_counter(
                    "synapse.model.tokens", unit="{token}", description="Tokens, by model and direction"),
                "tool_calls": meter.create_counter(
                    "synapse.tool.calls", unit="{call}", description="Tool calls, by outcome"),
                "jobs_completed": meter.create_counter(
                    "synapse.jobs.completed", unit="{job}", description="Queued jobs run, by status"),
                "job_duration": meter.create_histogram(
                    "synapse.job.duration", unit="s", description="Time a queued job ran"),
                "jobs_dead_lettered": meter.create_counter(
                    "synapse.jobs.dead_lettered", unit="{job}",
                    description="Jobs that ran out of retries and were dead-lettered"),
                "webhook_deliveries": meter.create_counter(
                    "synapse.webhook.deliveries", unit="{delivery}", description="Webhook deliveries"),
                "runs_active": meter.create_up_down_counter(
                    "synapse.runs.active", unit="{run}", description="Runs working in this process"),
            }
            meter.create_observable_gauge(
                "synapse.queue.depth", callbacks=[_observe_queue_depth], unit="{job}",
                description="Jobs waiting in the queue, as last read by this process")
    return _instruments


def _observe_queue_depth(_options):
    from opentelemetry.metrics import Observation
    return [Observation(depth, {"queue": queue}) for queue, depth in list(_queue_depth.items())]


def _add(name: str, value, attributes: dict | None = None) -> None:
    instruments = _get()
    if instruments is None:
        return
    try:
        instruments[name].add(value, {k: v for k, v in (attributes or {}).items() if v is not None})
    except Exception:
        pass


def _record(name: str, value, attributes: dict | None = None) -> None:
    instruments = _get()
    if instruments is None:
        return
    try:
        instruments[name].record(value, {k: v for k, v in (attributes or {}).items() if v is not None})
    except Exception:
        pass


# --- recorders ------------------------------------------------------------------


def run_enqueued() -> None:
    _add("runs_enqueued", 1)


def run_stretch(status: str | None, seconds: float) -> None:
    """A run's stretch of work ended (it completed, failed, or paused)."""
    _add("runs_completed", 1, {A.STATUS: status or "unknown"})
    _record("run_duration", seconds, {A.STATUS: status or "unknown"})


def run_started() -> None:
    _active(1)


def run_stopped() -> None:
    _active(-1)


def _active(delta: int) -> None:
    global _active_runs
    _active_runs += delta
    _add("runs_active", delta)


def step(step_type: str | None, status: str | None, seconds: float) -> None:
    _record("step_duration", seconds, {A.STEP_TYPE: step_type or "unknown", A.STATUS: status or "unknown"})


def chat_turn(status: str | None, seconds: float) -> None:
    _add("chat_turns", 1, {A.STATUS: status or "unknown"})
    _record("chat_turn_duration", seconds, {A.STATUS: status or "unknown"})


def model_call(bucket: str | None, status: str, seconds: float) -> None:
    _add("model_calls", 1, {A.MODEL_BUCKET: bucket or "unknown", A.STATUS: status})
    _record("model_duration", seconds, {A.MODEL_BUCKET: bucket or "unknown", A.STATUS: status})


def model_tokens(bucket: str, input_tokens: int, output_tokens: int) -> None:
    if input_tokens:
        _add("model_tokens", int(input_tokens), {A.MODEL_BUCKET: bucket, "direction": "input"})
    if output_tokens:
        _add("model_tokens", int(output_tokens), {A.MODEL_BUCKET: bucket, "direction": "output"})


def tool_call(category: str | None, outcome: str) -> None:
    _add("tool_calls", 1, {A.TOOL_CATEGORY: category or "none", "outcome": outcome})


def job(job_name: str, status: str, seconds: float) -> None:
    _add("jobs_completed", 1, {A.JOB_NAME: job_name, A.STATUS: status})
    _record("job_duration", seconds, {A.JOB_NAME: job_name, A.STATUS: status})


def job_dead_lettered(job_name: str) -> None:
    _add("jobs_dead_lettered", 1, {A.JOB_NAME: job_name})


def webhook(success: bool) -> None:
    _add("webhook_deliveries", 1, {"outcome": "success" if success else "failed"})


def set_queue_depth(queue: str, depth: int) -> None:
    """Remember the queue's depth for the next scrape or export."""
    _queue_depth[queue] = int(depth)
    _get()


def reset_for_tests() -> None:
    global _instruments, _active_runs
    _instruments = None
    _active_runs = 0
    _queue_depth.clear()
