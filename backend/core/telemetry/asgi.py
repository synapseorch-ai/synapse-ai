"""Server spans for an ASGI app, decided per request.

`TelemetryMiddleware` is installed when the app is built and costs one flag
check while telemetry is off. Installing it then matters: Starlette refuses new
middleware once the app has started, and the OSS server can only read a saved
collector endpoint inside its lifespan — after start. So the middleware exists
from the beginning and becomes OpenTelemetry's ASGI middleware the first
request after `setup()` turned tracing on.

What it adds when on:
* a SERVER span per request, named `METHOD /route/{template}` once routing has
  matched (never the raw path, which carries ids), with `http.route`;
* no span for health checks, metrics, or a GET that tails an event stream (a
  browser holds `/notifications/stream` open for its whole session); a POST
  that streams while it starts work is traced, without per-chunk spans;
* a caller's `traceparent` is ignored on `untrusted_prefixes` (a public API a
  stranger can call), so a stranger cannot join or suppress our traces;
* `x-request-id`: a valid incoming one is kept, else the trace id is used; it
  goes on the span, the log context and the response.

Add it LAST (`app.add_middleware` inserts at the front, so the last one added
is outermost) — an inner position would miss requests another middleware
answers itself, and leave the work they do before the app without a parent.
"""
from __future__ import annotations

import os
import re

from core.telemetry import logcontext, state
from core.telemetry.attributes import REQUEST_ID

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ALWAYS_EXCLUDED = ("/healthz", "/readyz", "/health", "/metrics", "/api/v2/metrics")
_TRACE_HEADERS = {b"traceparent", b"tracestate", b"baggage"}


def _paths(env: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.getenv(env, "").split(",") if p.strip())


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return ""
    return ""


def _excluded(scope, extra: tuple[str, ...]) -> bool:
    path = scope.get("path") or ""
    if path in _ALWAYS_EXCLUDED or any(path == p or path.startswith(p.rstrip("/") + "/")
                                       for p in extra):
        return True
    if scope.get("method") == "GET" and (
            "text/event-stream" in _header(scope, b"accept") or path.endswith("/stream")):
        return True
    return False


def route_template(scope) -> str | None:
    """The matched route's template, with any include-time prefix.

    FastAPI 0.141 keeps the prefix a router was *included* with on its
    effective route context (set only while the route runs — which is when
    the response starts); the route object itself holds the bare path. An app
    without that context falls back to the route.
    """
    context = (scope.get("fastapi") or {}).get("effective_route_context")
    path = getattr(context, "path_format", None)
    if not (isinstance(path, str) and path):
        route = scope.get("route")
        path = getattr(route, "path_format", None) or getattr(route, "path", None)
        if not (isinstance(path, str) and path):
            return None
        path = (scope.get("root_path") or "") + path
    return path


class TelemetryMiddleware:
    def __init__(self, app, untrusted_prefixes: tuple[str, ...] | None = None):
        self.app = app
        self._untrusted = tuple(untrusted_prefixes or ()) + _paths("SYNAPSE_TELEMETRY_UNTRUSTED_PATHS")
        self._excluded = _paths("SYNAPSE_TELEMETRY_EXCLUDED_PATHS")
        self._traced = None

    async def __call__(self, scope, receive, send):
        if not state.tracing or scope["type"] != "http" or _excluded(scope, self._excluded):
            return await self.app(scope, receive, send)
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if self._traced is None or self._traced[0] is not provider:
            # Built on the first traced request, against the provider setup()
            # installed (one per process; a test installs its own).
            from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
            self._traced = (provider, OpenTelemetryMiddleware(
                _NameAndTag(self.app),
                default_span_details=lambda s: (s.get("method") or "HTTP", {}),
                exclude_spans=["send", "receive"],
                tracer_provider=provider,
            ))
        path = scope.get("path") or ""
        if self._untrusted and any(path.startswith(p) for p in self._untrusted):
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in scope.get("headers") or ()
                                if k.lower() not in _TRACE_HEADERS]
        return await self._traced[1](scope, receive, send)


class _NameAndTag:
    """Runs inside the server span: names it by route, sets the request id."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from opentelemetry import trace

        span = trace.get_current_span()
        incoming = _header(scope, b"x-request-id")
        if _REQUEST_ID.match(incoming or ""):
            request_id = incoming
        else:
            request_id = format(span.get_span_context().trace_id, "032x")
        span.set_attribute(REQUEST_ID, request_id)
        token = logcontext.bind(request_id=request_id)
        named = False

        def name_span():
            nonlocal named
            if named:
                return
            named = True
            template = route_template(scope)
            method = scope.get("method") or "HTTP"
            if template:
                span.update_name(f"{method} {template}")
                span.set_attribute("http.route", template)

        async def send_tagged(message):
            if message["type"] == "http.response.start":
                name_span()
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_tagged)
        finally:
            name_span()
            logcontext.reset(token)
