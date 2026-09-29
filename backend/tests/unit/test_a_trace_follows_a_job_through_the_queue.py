"""A trace follows a job through the queue, in the envelope — and old workers
do not notice.

The job's trace context rides as an extra key in arq's pickled job dict: a job
queued by a traced request runs as that trace's child, on the real
`arq.Worker` (fakeredis, as `test_shared_fleet.py` runs it). A deferred or
retried job starts its own trace with a link, and a payload in either format
is read by either side.
"""
import pickle
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from arq import ArqRedis
from arq.jobs import deserialize_job_raw, serialize_job
from arq.worker import Worker

from _fakes.telemetry import telemetry_off, telemetry_on  # noqa: F401  (fixtures)
from core.telemetry import queue as tq

QUEUE = "o1:queue"


async def _no_redis_info(redis, log_func):
    """fakeredis has no INFO command; arq only uses it to log a banner."""


@pytest.fixture
async def redis(monkeypatch):
    import arq.worker
    monkeypatch.setattr(arq.worker, "log_redis_info", _no_redis_info)
    server = fakeredis.FakeServer()
    client = fakeredis.aioredis.FakeRedis(server=server)
    yield client
    await client.aclose()


def _traced(fn):
    async def job(ctx, **kwargs):
        with tq.job_span(ctx, fn.__name__, kwargs):
            return await fn(ctx, **kwargs)
    job.__name__ = fn.__name__
    job.__qualname__ = fn.__name__
    return job


async def _drain(redis, functions):
    pool = ArqRedis(connection_pool=redis.connection_pool, default_queue_name=QUEUE,
                    job_serializer=tq.serialize, job_deserializer=tq.deserialize)
    worker = Worker(functions=functions, redis_pool=pool, queue_name=QUEUE, burst=True,
                    handle_signals=False, poll_delay=0.01, job_serializer=tq.serialize,
                    job_deserializer=tq.deserialize)
    await worker.async_run()
    return pool


async def test_a_job_queued_by_a_request_runs_in_the_requests_trace(telemetry_on, redis):
    from opentelemetry import trace
    ran = []

    async def work(ctx, run_id):
        ran.append(run_id)

    pool = ArqRedis(connection_pool=redis.connection_pool, default_queue_name=QUEUE,
                    job_serializer=tq.serialize, job_deserializer=tq.deserialize)
    with trace.get_tracer("synapse.test").start_as_current_span("POST /run") as request:
        await pool.enqueue_job("work", run_id="run_q1__child", _queue_name=QUEUE)
    await _drain(redis, [_traced(work)])

    assert ran == ["run_q1__child"]
    spans = {s.name: s for s in telemetry_on.spans()}
    job, enqueue = spans["job work"], spans["synapse.enqueue"]
    assert job.context.trace_id == request.get_span_context().trace_id
    assert job.parent.span_id == enqueue.context.span_id
    assert enqueue.attributes["synapse.run_id"] == "run_q1__child"
    assert job.attributes["synapse.root_run_id"] == "run_q1"
    assert job.kind.name == "CONSUMER" and enqueue.kind.name == "PRODUCER"


async def test_a_job_queued_by_a_job_stays_in_the_trace(telemetry_on, redis):
    async def parent(ctx):
        await ctx["redis"].enqueue_job("child", _queue_name=QUEUE)

    async def child(ctx):
        return "done"

    pool = ArqRedis(connection_pool=redis.connection_pool, default_queue_name=QUEUE,
                    job_serializer=tq.serialize, job_deserializer=tq.deserialize)
    await pool.enqueue_job("parent", _queue_name=QUEUE)
    await _drain(redis, [_traced(parent), _traced(child)])
    spans = {s.name: s for s in telemetry_on.spans()}
    assert spans["job child"].context.trace_id == spans["job parent"].context.trace_id


def _ctx(*, job_try=1, queued_ago_ms=0, runs_after_ms=0):
    now = datetime.now(timezone.utc)
    queued = now - timedelta(milliseconds=queued_ago_ms)
    return {"job_id": "j1", "job_try": job_try, "enqueue_time": queued,
            "score": int(queued.timestamp() * 1000) + runs_after_ms}


def _carry_from(span_name):
    from opentelemetry import propagate, trace
    with trace.get_tracer("synapse.test").start_as_current_span(span_name) as span:
        carrier = {}
        propagate.inject(carrier)
    tq._carried.set(carrier)
    return span


@pytest.mark.parametrize("ctx", [
    _ctx(runs_after_ms=3_600_000),          # deferred an hour: a schedule, an expiry
    _ctx(job_try=2),                         # a retry
])
def test_a_deferred_or_retried_job_starts_its_own_trace_with_a_link(telemetry_on, ctx):
    queued_by = _carry_from("POST /schedules")
    with tq.job_span(ctx, "run_schedule_job"):
        pass
    job = next(s for s in telemetry_on.spans() if s.name == "job run_schedule_job")
    assert job.parent is None
    assert job.context.trace_id != queued_by.get_span_context().trace_id
    assert [link.context.span_id for link in job.links] == [queued_by.get_span_context().span_id]


def test_one_job_span_even_when_two_wrappers_start_one(telemetry_on):
    with tq.job_span(_ctx(), "outer"):
        with tq.job_span(_ctx(), "inner") as inner:
            assert inner is None
    assert [s.name for s in telemetry_on.spans()] == ["job outer"]


def test_either_side_reads_either_format(telemetry_on):
    from opentelemetry import trace
    with trace.get_tracer("synapse.test").start_as_current_span("request"):
        new = serialize_job("work", (), {"run_id": "r1"}, 1, 0, serializer=tq.serialize)
    assert tq.KEY in pickle.loads(new)
    # A worker from before this change reads it with arq's own pickle.loads.
    assert deserialize_job_raw(new) == ("work", (), {"run_id": "r1"}, 1, 0)
    # This worker reads a job queued by an API from before it.
    old = serialize_job("work", (), {"run_id": "r1"}, 1, 0)
    assert deserialize_job_raw(old, deserializer=tq.deserialize) == ("work", (), {"run_id": "r1"}, 1, 0)
    assert tq.carried() is None


def test_a_result_is_left_as_it_was(telemetry_on):
    result = {"t": 1, "f": "work", "a": (), "k": {}, "et": 0, "s": True, "r": "ok"}
    assert pickle.loads(tq.serialize(result)) == result


def test_off_the_envelope_is_plain_pickle(telemetry_off):
    data = {"t": 1, "f": "work", "a": (), "k": {}, "et": 0}
    assert tq.serialize(data) == pickle.dumps(data)


def test_the_engines_worker_reads_and_writes_the_envelope():
    from core.scale.worker import WorkerSettings
    from arq.worker import get_kwargs
    kwargs = get_kwargs(WorkerSettings)
    assert kwargs["job_serializer"] is tq.serialize
    assert kwargs["job_deserializer"] is tq.deserialize


async def test_the_engines_job_wrapper_starts_the_job_span(telemetry_on):
    from core.scale.worker import _for_the_jobs_tenant

    @_for_the_jobs_tenant
    async def resume_failed_job(ctx, run_id):
        return run_id

    assert await resume_failed_job(_ctx(), run_id="run_w1") == "run_w1"
    (job,) = [s for s in telemetry_on.spans() if s.name.startswith("job ")]
    assert job.name == "job resume_failed_job"
    assert job.attributes["synapse.run_id"] == "run_w1"


async def test_a_long_lived_task_joins_no_trace_and_carries_no_ids(telemetry_on):
    import asyncio

    from opentelemetry import trace

    from core import telemetry
    from core.telemetry import logcontext

    seen = {}

    async def supervisor():
        seen["span"] = trace.get_current_span().get_span_context().is_valid
        seen["ids"] = dict(logcontext.current())

    with trace.get_tracer("synapse.test").start_as_current_span("job"):
        token = logcontext.bind(run_id="run_first")
        await asyncio.create_task(supervisor(), context=telemetry.detached_context())
        logcontext.reset(token)
    assert seen == {"span": False, "ids": {}}
