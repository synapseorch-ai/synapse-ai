"""Server spans: one per request, named by route, and none for the noise.

The middleware is added last (outermost) when the app is built and switches on
with `setup()`; off, it is a pass-through that adds nothing to a response.
"""
import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from _fakes.telemetry import telemetry_off, telemetry_on  # noqa: F401  (fixtures)
from core.telemetry.asgi import TelemetryMiddleware

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def _app() -> FastAPI:
    app = FastAPI()
    things = APIRouter(prefix="/api/things")

    @things.get("/{thing_id}")
    async def get_thing(thing_id: str):
        return {"id": thing_id}

    @things.post("/stream")
    async def start_and_stream():
        async def body():
            for i in range(3):
                yield f"data: {i}\n\n"
        return StreamingResponse(body(), media_type="text/event-stream")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/notifications/stream")
    async def tail():
        async def body():
            yield "data: hello\n\n"
        return StreamingResponse(body(), media_type="text/event-stream")

    @app.get("/api/v1/ping")
    async def public_ping():
        return {"pong": True}

    app.include_router(things)
    app.add_middleware(TelemetryMiddleware, untrusted_prefixes=("/api/v1",))
    return app


async def _get(app, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.request(method, path, **kwargs)


async def test_a_request_is_named_by_its_route_not_its_path(telemetry_on):
    response = await _get(_app(), "GET", "/api/things/abc123")
    (span,) = telemetry_on.spans()
    assert span.name == "GET /api/things/{thing_id}"
    assert span.attributes["http.route"] == "/api/things/{thing_id}"
    assert "abc123" not in span.name
    assert response.headers["x-request-id"] == format(span.context.trace_id, "032x")
    assert span.attributes["synapse.request_id"] == response.headers["x-request-id"]


async def test_a_valid_incoming_request_id_is_kept_an_invalid_one_is_not(telemetry_on):
    app = _app()
    kept = await _get(app, "GET", "/api/things/1", headers={"x-request-id": "req-42.a_b"})
    replaced = await _get(app, "GET", "/api/things/2", headers={"x-request-id": "no spaces allowed"})
    assert kept.headers["x-request-id"] == "req-42.a_b"
    assert replaced.headers["x-request-id"] != "no spaces allowed"


async def test_health_checks_and_tailing_streams_are_not_traced(telemetry_on):
    app = _app()
    await _get(app, "GET", "/healthz")
    await _get(app, "GET", "/notifications/stream", headers={"accept": "text/event-stream"})
    assert telemetry_on.spans() == []


async def test_a_post_that_streams_is_traced_without_a_span_per_chunk(telemetry_on):
    await _get(_app(), "POST", "/api/things/stream")
    assert telemetry_on.names() == ["POST /api/things/stream"]


async def test_a_trusted_caller_joins_its_trace_a_public_caller_does_not(telemetry_on):
    app = _app()
    await _get(app, "GET", "/api/things/1", headers={"traceparent": TRACEPARENT})
    await _get(app, "GET", "/api/v1/ping", headers={"traceparent": TRACEPARENT})
    joined, public = telemetry_on.spans()
    assert format(joined.context.trace_id, "032x") == TRACEPARENT.split("-")[1]
    assert format(public.context.trace_id, "032x") != TRACEPARENT.split("-")[1]
    assert public.parent is None


async def test_an_unknown_path_is_named_by_its_method_only(telemetry_on):
    await _get(_app(), "GET", "/no/such/thing-99")
    assert telemetry_on.names() == ["GET"]


async def test_off_the_middleware_changes_nothing(telemetry_off):
    from core import telemetry
    telemetry.setup("synapse-api")
    response = await _get(_app(), "GET", "/api/things/1")
    assert response.status_code == 200
    assert "x-request-id" not in response.headers
