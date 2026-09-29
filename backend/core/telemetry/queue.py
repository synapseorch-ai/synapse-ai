"""A trace follows a job through the queue — in the job's envelope, not its
arguments.

arq pickles each job as a dict `{t, f, a, k, et}` and reads back exactly those
five keys. `serialize` adds one more, `otel`: the W3C trace context of a short
PRODUCER span (plus the request id) — so a worker from before this change
ignores it, and this worker reads a job without it as a new trace. No job
function gains a parameter, and there is no deploy order to keep. (A job
kwarg would fail every job on a worker that did not declare it.)

`deserialize` only remembers the carried context in a contextvar: arq
deserializes a job in the task that then creates the job's own task, so the
value reaches that one job and nothing else. The API also deserializes (a
job's status), where the value is never read.

`job_span` starts the job's CONSUMER span once per job, from whichever
wrapper runs first (Cloud's, around every job; the engine's own around its
four). A job is the carried context's **child** only when it runs promptly —
first try, started within a few seconds of being queued. A deferred, re-armed
or retried job (a schedule, a digest, an expiry, a `Retry`) starts its own
trace with a **link** to the one that queued it; as a child, a job that
re-arms itself would grow one trace for ever.
"""
from __future__ import annotations

import pickle
import time
from contextlib import contextmanager
from contextvars import ContextVar

from core.telemetry import attributes as A
from core.telemetry import logcontext, metrics, state

KEY = "otel"
PROMPT_MS = 5000

_carried: ContextVar[dict | None] = ContextVar("synapse_job_carrier", default=None)
_in_job: ContextVar[bool] = ContextVar("synapse_in_job_span", default=False)


def _is_job(data) -> bool:
    return isinstance(data, dict) and "f" in data and "s" not in data


def serialize(data) -> bytes:
    """arq's `job_serializer`: pickle, plus the trace context when tracing."""
    if state.tracing and _is_job(data):
        carrier = _produce(data)
        if carrier:
            data = {**data, KEY: carrier}
    return pickle.dumps(data)


def deserialize(raw: bytes):
    """arq's `job_deserializer`: pickle; remembers a job's carried context."""
    data = pickle.loads(raw)
    if _is_job(data):
        carried = data.get(KEY)
        _carried.set(carried if isinstance(carried, dict) else None)
    return data


def _ids_from(kwargs) -> dict:
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    run_id = kwargs.get("run_id")
    return {
        A.RUN_ID: run_id if isinstance(run_id, str) else None,
        A.ROOT_RUN_ID: run_id.split("__", 1)[0] if isinstance(run_id, str) else None,
        A.SESSION_ID: kwargs.get("session_id") if isinstance(kwargs.get("session_id"), str) else None,
        A.SCHEDULE_ID: kwargs.get("schedule_id") if isinstance(kwargs.get("schedule_id"), str) else None,
    }


def _produce(data: dict) -> dict | None:
    try:
        from opentelemetry import propagate, trace
        from opentelemetry.trace import SpanKind

        tracer = trace.get_tracer("synapse.queue")
        with tracer.start_as_current_span("synapse.enqueue", kind=SpanKind.PRODUCER) as span:
            span.set_attribute(A.JOB_NAME, str(data.get("f"))[:64])
            span.set_attribute("messaging.system", "arq")
            span.set_attribute("messaging.operation.type", "send")
            for key, value in _ids_from(data.get("k")).items():
                if value:
                    span.set_attribute(key, value)
            carrier: dict = {}
            propagate.inject(carrier)
        request_id = logcontext.current().get("request_id")
        if request_id:
            carrier["x-request-id"] = request_id
        return carrier or None
    except Exception:
        return None


def carried() -> dict | None:
    return _carried.get()


def _prompt(ctx: dict) -> bool:
    if (ctx.get("job_try") or 1) > 1:
        return False
    score, queued = ctx.get("score"), ctx.get("enqueue_time")
    try:
        queued_ms = queued.timestamp() * 1000
    except Exception:
        return True
    if isinstance(score, (int, float)):
        return score - queued_ms < PROMPT_MS
    return time.time() * 1000 - queued_ms < PROMPT_MS


@contextmanager
def job_span(ctx: dict, function_name: str, kwargs: dict | None = None, **ids):
    """The job's CONSUMER span, once per job; yields it (or None off/nested).

    Also the job's metrics (`synapse.jobs.completed`, `synapse.job.duration`)
    when only metrics are on — the span is then a no-op one.
    """
    if not (state.tracing or state.metering) or _in_job.get():
        yield None
        return
    from opentelemetry import context as otel_context
    from opentelemetry import propagate, trace
    from opentelemetry.trace import Link, SpanKind

    from core.telemetry.spans import _fail

    carrier = _carried.get() or {}
    parent = otel_context.Context()
    links = []
    if carrier:
        remote = propagate.extract(carrier)
        remote_span = trace.get_current_span(remote).get_span_context()
        if remote_span.is_valid:
            if _prompt(ctx):
                parent = remote
            else:
                links = [Link(remote_span)]
    attributes = {k: v for k, v in _ids_from(kwargs).items() if v}
    attributes.update({k: v for k, v in ids.items() if isinstance(v, (str, int, float, bool)) and v != ""})
    attributes.update({A.JOB_NAME: function_name[:64], A.JOB_TRY: int(ctx.get("job_try") or 1),
                       "messaging.system": "arq", "messaging.operation.type": "process"})
    if ctx.get("job_id"):
        attributes[A.JOB_ID] = str(ctx["job_id"])[:64]
    request_id = carrier.get("x-request-id")
    if request_id:
        attributes[A.REQUEST_ID] = str(request_id)[:128]
    tracer = trace.get_tracer("synapse.queue")
    started, status = time.monotonic(), "completed"
    with tracer.start_as_current_span(f"job {function_name[:64]}", context=parent, kind=SpanKind.CONSUMER,
                                      links=links, attributes=attributes, record_exception=False,
                                      set_status_on_exception=False) as span:
        in_job = _in_job.set(True)
        log = logcontext.bind(job_id=ctx.get("job_id"), run_id=attributes.get(A.RUN_ID),
                              root_run_id=attributes.get(A.ROOT_RUN_ID),
                              session_id=attributes.get(A.SESSION_ID),
                              schedule_id=attributes.get(A.SCHEDULE_ID), request_id=request_id,
                              org_id=ids.get(A.ORG_ID))
        try:
            yield span
        except BaseException as exc:
            from core.telemetry.spans import _is_cancellation
            status = "cancelled" if _is_cancellation(exc) else "failed"
            _fail(span, exc)
            raise
        finally:
            logcontext.reset(log)
            _in_job.reset(in_job)
            metrics.job(function_name[:64], status, time.monotonic() - started)


# --- queue depth ---------------------------------------------------------------

DEPTH_INTERVAL = 30


def watch_depth(redis, queue_name: str, interval: int = DEPTH_INTERVAL):
    """Read the queue's depth every `interval` seconds for `synapse.queue.depth`.

    Started by a worker's startup; returns the task (None when metrics are off),
    which `stop_watching` cancels. Its Redis read has no parent span, so it is
    never traced.
    """
    if not state.metering:
        return None
    import asyncio

    async def loop():
        while True:
            try:
                metrics.set_queue_depth(queue_name, await redis.zcard(queue_name))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    return asyncio.create_task(loop(), name="synapse-queue-depth")


def stop_watching(task) -> None:
    if task is not None:
        task.cancel()
