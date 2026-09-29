"""A log line carries its trace and its run — and none of a job's words.

JSON lines (SYNAPSE_LOG_FORMAT=json) carry the trace and span ids, X-Ray's
form of the trace id, and the org, run, session and request ids bound in
`logcontext`. arq's own lines lose what it used to print: the first 80
characters of a job's arguments (for a chat job, a person's message), its
result's repr, and a failed job's exception message.
"""
import json
import logging

from _fakes.telemetry import telemetry_on  # noqa: F401  (fixture)
from core.telemetry import logcontext
from core.telemetry.logs import ArqContentFilter, JsonFormatter

SECRET = "SECRET-9020"


def _record(msg, args, exc_info=None, name="arq.worker"):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)


def test_a_json_line_carries_the_trace_and_the_runs_ids(telemetry_on):
    from opentelemetry import trace
    with trace.get_tracer("synapse.test").start_as_current_span("job") as span:
        token = logcontext.bind(org_id="org_1", run_id="run_1", session_id="s_1", request_id="req-1")
        line = json.loads(JsonFormatter("synapse-worker").format(
            _record("step %s done", ("work",), name="core.engine")))
        logcontext.reset(token)
    trace_id = format(span.get_span_context().trace_id, "032x")
    assert line["message"] == "step work done"
    assert line["trace_id"] == trace_id
    assert line["xray_trace_id"] == f"1-{trace_id[:8]}-{trace_id[8:]}"
    assert line["span_id"] == format(span.get_span_context().span_id, "016x")
    assert (line["org_id"], line["run_id"], line["session_id"], line["request_id"]) == \
        ("org_1", "run_1", "s_1", "req-1")
    assert line["service"] == "synapse-worker"


def test_only_ids_can_be_bound_to_a_line():
    token = logcontext.bind(run_id="run_1", prompt=SECRET)
    assert logcontext.current() == {"run_id": "run_1"}
    logcontext.reset(token)


def test_arq_start_and_result_lines_lose_the_arguments_and_the_result():
    start = _record("%6.2fs → %s(%s)%s", (0.01, "7f:run_agent_chat_job",
                                          f"message='{SECRET} please'", " try=1"))
    result = _record("%6.2fs ← %s ● %s", (1.2, "7f:run_agent_chat_job", f"{{'answer': '{SECRET}'}}"))
    for record in (start, result):
        assert ArqContentFilter().filter(record)
        assert SECRET not in record.getMessage()
    assert start.getMessage() == "  0.01s → 7f:run_agent_chat_job(…) try=1"


def test_a_failed_jobs_line_keeps_its_type_and_frames_not_its_message():
    try:
        raise ValueError(f"the model said {SECRET}")
    except ValueError as exc:
        import sys
        record = _record("%6.2fs ! %s failed, %s: %s", (2.0, "7f:run_orchestration_job", "ValueError", exc),
                         exc_info=sys.exc_info())
    ArqContentFilter().filter(record)
    rendered = logging.Formatter().format(record)
    assert SECRET not in rendered
    assert "ValueError" in rendered and "test_a_failed_jobs_line_keeps" in rendered


def test_a_line_outside_any_tenant_claims_none():
    line = json.loads(JsonFormatter().format(_record("booted", (), name="core.server")))
    assert "org_id" not in line
