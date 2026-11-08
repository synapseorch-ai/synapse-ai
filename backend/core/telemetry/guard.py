"""The content guard: the last thing every span passes before it leaves.

Instrumentation libraries record what they see — a client span's full URL, a
database statement, an exception's message, a server span named after its raw
path — and any of those can carry a tenant's words: a scraped URL's query, a
customer's SQL, a provider error that quotes the prompt, a search box's text.
Rather than trust each library's settings, every span is rebuilt here from an
allowlist (`attributes.py`) before the exporter sees it:

* spans from an instrumentation scope nobody reviewed are dropped;
* attributes and event attributes not on the list are dropped, strings capped;
* an outbound call's host is kept only for an internal service; any other is
  `external`, and its port goes;
* span names lose any query string, an HTTP client span is named by its
  method, a database span by its operation;
* an exception keeps its type, never its message or stack; the status
  description `use_span` writes ("ValueError: <message>") keeps only the type.

This is defence in depth, not the only line: Synapse's own spans are written to
carry nothing else. `tests/unit/test_telemetry_guard.py` pins every rule.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from core.telemetry.attributes import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_EVENTS,
    ALLOWED_SCOPE_PREFIXES,
    MAX_STRING,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SQL_VERBS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK", "WITH",
    "CREATE", "ALTER", "DROP", "SET", "SHOW", "PRAGMA", "SAVEPOINT", "RELEASE",
    "LISTEN", "NOTIFY", "TRUNCATE", "COPY", "EXPLAIN", "VACUUM", "ANALYZE",
}
_HOST_KEYS = ("server.address", "net.peer.name", "http.host")


def _extra_internal_hosts() -> frozenset[str]:
    raw = os.getenv("SYNAPSE_TELEMETRY_INTERNAL_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def is_internal_host(host: str) -> bool:
    """A service on our own network: a bare name (a compose or Cloud Map
    service), `*.internal` / `*.local`, a loopback or private address, or a host
    listed in SYNAPSE_TELEMETRY_INTERNAL_HOSTS."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h in _extra_internal_hosts() or h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        return ip.is_loopback or ip.is_private
    except ValueError:
        pass
    return "." not in h or h.endswith(".internal") or h.endswith(".local")


def scope_allowed(name: str | None) -> bool:
    return bool(name) and name.startswith(ALLOWED_SCOPE_PREFIXES)


def _clean_value(value):
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        items = [v for v in value if isinstance(v, (str, bool, int, float))][:32]
        return tuple(v[:MAX_STRING] if isinstance(v, str) else v for v in items)
    return None


def clean_attributes(attributes) -> dict:
    out = {}
    for key, value in (attributes or {}).items():
        if key not in ALLOWED_ATTRIBUTES:
            continue
        value = _clean_value(value)
        if value is not None:
            out[key] = value
    return out


def _host_of(attributes) -> str:
    for key in _HOST_KEYS:
        value = (attributes or {}).get(key)
        if isinstance(value, str) and value:
            return value.split(":")[0]
    return ""


def _span_attributes(span: ReadableSpan) -> dict:
    raw = dict(span.attributes or {})
    out = clean_attributes(raw)
    host = _host_of(raw)
    if host:
        if is_internal_host(host):
            out["server.address"] = host[:MAX_STRING]
        else:
            out["server.address"] = "external"
            out.pop("server.port", None)
    return out


def _method(attributes) -> str:
    for key in ("http.request.method", "http.method"):
        value = (attributes or {}).get(key)
        if isinstance(value, str) and value:
            return value.upper()[:16]
    return ""


def _db_operation(name: str, attributes, *, redis: bool) -> str:
    """A database span's name: the operation, never the statement.

    SQL spans keep a known verb; Redis spans keep the command (one alphabetic
    word). Anything else becomes the bare system name.
    """
    candidates = [(attributes or {}).get(k) for k in ("db.operation.name", "db.operation")]
    candidates.append((name or "").strip().split(" ", 1)[0])
    for value in candidates:
        if not isinstance(value, str):
            continue
        word = value.strip().upper()
        if (redis and word.isalpha() and len(word) <= 24) or word in _SQL_VERBS:
            return word
    return "redis" if redis else "db"


def clean_name(span: ReadableSpan) -> str:
    name = (span.name or "").split("?", 1)[0]
    attributes = span.attributes or {}
    if "db.system" in attributes or "db.system.name" in attributes:
        system = attributes.get("db.system") or attributes.get("db.system.name")
        return _db_operation(name, attributes, redis=system == "redis")
    if span.kind == SpanKind.CLIENT and _method(attributes):
        return _method(attributes)
    if "://" in name:
        return _method(attributes) or "HTTP"
    return name[:MAX_STRING]


def clean_status(status: Status) -> Status:
    if status is None or status.status_code != StatusCode.ERROR:
        return status
    description = (status.description or "").split(":", 1)[0].strip()
    return Status(StatusCode.ERROR, description if _IDENT.match(description) else None)


def clean_events(events) -> tuple:
    out = []
    for event in events or ():
        if event.name not in ALLOWED_EVENTS:
            continue
        out.append(Event(event.name, clean_attributes(event.attributes), event.timestamp))
    return tuple(out)


def clean_links(links) -> tuple:
    return tuple(Link(link.context, clean_attributes(link.attributes)) for link in links or ())


def clean_span(span: ReadableSpan) -> ReadableSpan | None:
    scope = span.instrumentation_scope
    if not scope_allowed(scope.name if scope else None):
        return None
    return ReadableSpan(
        name=clean_name(span),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_span_attributes(span),
        events=clean_events(span.events),
        links=clean_links(span.links),
        kind=span.kind,
        status=clean_status(span.status),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=scope,
    )


class ContentGuardExporter(SpanExporter):
    """Wrap an exporter so only cleaned spans reach it."""

    def __init__(self, inner: SpanExporter):
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        cleaned = [c for c in (clean_span(s) for s in spans) if c is not None]
        if not cleaned:
            return SpanExportResult.SUCCESS
        return self._inner.export(cleaned)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)
