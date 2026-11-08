"""The API's own requests are traced once telemetry is on.

Before O1, `setup_telemetry()` ran in the lifespan — after `app = FastAPI(...)`
— and `FastAPIInstrumentor().instrument()` only patches apps built later, so the
API produced no request span at all. Now `TelemetryMiddleware` is installed when
the app is built, outermost, and switches on with `setup()`.
"""
from _fakes.telemetry import telemetry_on  # noqa: F401  (fixture)


async def test_a_request_to_the_api_is_one_span_named_by_its_route(telemetry_on, client):
    response = await client.get("/api/scale/config")
    assert response.status_code == 200
    server = [s for s in telemetry_on.spans() if s.kind.name == "SERVER"]
    assert [s.name for s in server] == ["GET /api/scale/config"]
    assert response.headers["x-request-id"] == format(server[0].context.trace_id, "032x")


async def test_a_request_another_middleware_answers_is_traced_too(telemetry_on, client):
    # Synapse's MCP endpoint answers inside its own middleware (D125), before
    # any route: the telemetry middleware must sit outside it.
    await client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    server = [s for s in telemetry_on.spans() if s.kind.name == "SERVER"]
    assert [s.attributes.get("http.request.method") for s in server] == ["POST"]


async def test_metrics_scrapes_are_not_traced(telemetry_on, client):
    await client.get("/api/v2/metrics")
    assert [s for s in telemetry_on.spans() if s.kind.name == "SERVER"] == []
