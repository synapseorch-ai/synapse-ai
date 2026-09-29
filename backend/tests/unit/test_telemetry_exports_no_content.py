"""The content guard: no span leaves carrying a tenant's words.

Each span below is built the way an instrumentation library builds it — a
client span with the full URL, a database span with the statement, an
exception recorded with its message — and carries the marker SECRET where the
content would be. None of it may reach the exporter.
"""
import pytest

from _fakes.telemetry import telemetry_off, telemetry_on  # noqa: F401  (fixtures)

SECRET = "SECRET-7731"


def _text(span) -> str:
    parts = [span.name, repr(dict(span.attributes or {})), repr(span.status.description)]
    for event in span.events:
        parts += [event.name, repr(dict(event.attributes or {}))]
    for link in span.links:
        parts.append(repr(dict(link.attributes or {})))
    return " ".join(parts)


def _tracer(name):
    from opentelemetry import trace
    return trace.get_tracer(name)


@pytest.fixture
def parent(telemetry_on):
    with _tracer("synapse.test").start_as_current_span("parent") as span:
        yield span


def _by_name(pipeline, name):
    return next(s for s in pipeline.spans() if s.name == name)


def test_an_outbound_calls_url_and_host_do_not_leave(telemetry_on, parent):
    from opentelemetry.trace import SpanKind
    span = _tracer("opentelemetry.instrumentation.httpx").start_span(
        f"GET https://example.com/search?q={SECRET}", kind=SpanKind.CLIENT,
        attributes={"http.request.method": "GET", "url.full": f"https://example.com/search?q={SECRET}",
                    "server.address": f"{SECRET}.example.com", "server.port": 443,
                    "http.response.status_code": 200})
    span.end()
    parent.end()
    exported = _by_name(telemetry_on, "GET")
    assert dict(exported.attributes) == {"http.request.method": "GET", "server.address": "external",
                                         "http.response.status_code": 200}
    assert SECRET not in _text(exported)


def test_an_internal_services_host_is_kept(telemetry_on, parent):
    from opentelemetry.trace import SpanKind
    span = _tracer("opentelemetry.instrumentation.httpx").start_span(
        "POST", kind=SpanKind.CLIENT,
        attributes={"http.request.method": "POST", "server.address": "gateway", "server.port": 8080})
    span.end()
    parent.end()
    exported = _by_name(telemetry_on, "POST")
    assert exported.attributes["server.address"] == "gateway"
    assert exported.attributes["server.port"] == 8080


@pytest.mark.parametrize("scope,name,system,expected", [
    ("opentelemetry.instrumentation.sqlalchemy", f"SELECT {SECRET}", "postgresql", "SELECT"),
    ("opentelemetry.instrumentation.sqlalchemy", f"{SECRET} synapse", "sqlite", "db"),
    ("opentelemetry.instrumentation.redis", "SET", "redis", "SET"),
])
def test_a_database_span_keeps_its_operation_never_its_statement(telemetry_on, parent, scope, name,
                                                                  system, expected):
    from opentelemetry.trace import SpanKind
    span = _tracer(scope).start_span(name, kind=SpanKind.CLIENT, attributes={
        "db.system": system, "db.statement": f"SELECT * FROM t WHERE x = '{SECRET}'",
        "db.query.text": f"SET k {SECRET}"})
    span.end()
    parent.end()
    exported = _by_name(telemetry_on, expected)
    assert dict(exported.attributes) == {"db.system": system}
    assert SECRET not in _text(exported)


def test_an_exception_keeps_its_type_never_its_message(telemetry_on):
    with pytest.raises(ValueError):
        with _tracer("synapse.test").start_as_current_span("boom"):
            raise ValueError(f"the prompt was {SECRET}")
    exported = _by_name(telemetry_on, "boom")
    assert exported.status.description == "ValueError"
    (event,) = exported.events
    assert event.name == "exception"
    assert set(event.attributes) <= {"exception.type", "exception.escaped"}
    assert SECRET not in _text(exported)


def test_an_unreviewed_librarys_spans_are_dropped(telemetry_on, parent):
    _tracer("crawl4ai").start_span(f"crawl {SECRET}").end()
    parent.end()
    assert telemetry_on.names() == ["parent"]


def test_an_undeclared_attribute_is_dropped_and_strings_are_capped(telemetry_on):
    with _tracer("synapse.test").start_as_current_span("turn", attributes={
            "synapse.prompt": SECRET, "synapse.session_id": "s" * 400, "synapse.turn": 3}):
        pass
    exported = _by_name(telemetry_on, "turn")
    assert set(exported.attributes) == {"synapse.session_id", "synapse.turn"}
    assert len(exported.attributes["synapse.session_id"]) == 256


def test_a_span_name_loses_its_query_string(telemetry_on):
    _tracer("opentelemetry.instrumentation.asgi").start_span(f"GET /verify?token={SECRET}").end()
    assert telemetry_on.names() == ["GET /verify"]


def test_an_event_nobody_reviewed_is_dropped(telemetry_on):
    with _tracer("synapse.test").start_as_current_span("step") as span:
        span.add_event("tool.output", {"text": SECRET})
        span.add_event("synapse.retry", {"synapse.retry.attempt": 2, "message": SECRET})
    exported = _by_name(telemetry_on, "step")
    assert [(e.name, dict(e.attributes)) for e in exported.events] == [
        ("synapse.retry", {"synapse.retry.attempt": 2})]


async def _select_secret(engine):
    from opentelemetry import trace
    from sqlalchemy import text
    with trace.get_tracer("synapse.test").start_as_current_span("job"):
        async with engine.connect() as connection:
            await connection.execute(text(f"SELECT '{SECRET}'"))
    await engine.dispose()


def _db_spans(pipeline):
    return [s for s in pipeline.spans() if s.instrumentation_scope.name.endswith("sqlalchemy")]


async def test_a_store_engine_traces_its_operation_never_its_statement(telemetry_on, tmp_path):
    from core.store.engine import build_engine
    await _select_secret(build_engine(f"sqlite+aiosqlite:///{tmp_path / 'after.db'}"))
    (span,) = _db_spans(telemetry_on)
    assert span.name == "SELECT"
    assert SECRET not in _text(span)


async def test_an_engine_built_before_setup_is_traced_once_it_turns_on(telemetry_off, tmp_path):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from _fakes.telemetry import Pipeline
    from core import telemetry
    from core.store.engine import build_engine

    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'before.db'}")
    exporter = InMemorySpanExporter()
    assert telemetry.setup("synapse-test", span_exporter=exporter)
    await _select_secret(engine)
    (span,) = _db_spans(Pipeline(exporter))
    assert span.name == "SELECT"
