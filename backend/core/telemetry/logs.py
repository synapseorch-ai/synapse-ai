"""Log lines that carry the trace, and arq's lines without their content.

`JsonFormatter` writes one JSON object per line: time, level, logger, message,
and the ids that tie the line to everything else — the trace and span (and the
same trace id in X-Ray's form, which CloudWatch uses to link a trace to its
logs), plus the org, run, session, request and job ids bound in `logcontext`.
Chosen with SYNAPSE_LOG_FORMAT=json (Cloud sets it for every process); plain
text stays the default for a self-hosted install.

`ArqContentFilter` fixes what arq itself writes, whatever the format: arq logs
the first 80 characters of every job's arguments when it starts one — for a
chat job, the first words of a person's message — its result's `repr` when it
ends, and a failed job's exception message. The filter keeps the delay, the job
and the function, and an exception's type and stack frames; it drops the
arguments, the result and the message.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback

from core.telemetry import logcontext, state

_ARQ_START = "%6.2fs → %s(%s)%s"
_ARQ_RESULT = "%6.2fs ← %s ● %s"
_ARQ_FAILED = "%6.2fs ! %s failed, %s: %s"

_providers: list = []


def add_context_provider(provider) -> None:
    """Register a callable returning ids for the current line (an embedder's own
    org contextvar, say). Called per line; must be cheap and never raise."""
    if provider not in _providers:
        _providers.append(provider)


def _trace_ids() -> dict:
    if not state.tracing:
        return {}
    try:
        from opentelemetry import trace
        context = trace.get_current_span().get_span_context()
    except Exception:
        return {}
    if not context.is_valid:
        return {}
    trace_id = format(context.trace_id, "032x")
    return {"trace_id": trace_id, "span_id": format(context.span_id, "016x"),
            "xray_trace_id": f"1-{trace_id[:8]}-{trace_id[8:]}"}


def _tenant() -> str | None:
    """The tenant bound to this line, if one is — never the single-tenant
    placeholder a line outside any tenant would otherwise claim."""
    try:
        from core.store.models import DEFAULT_TENANT
        from core.tenancy import get_tenant
        tenant = get_tenant()
    except Exception:
        return None
    return str(tenant) if tenant and tenant != DEFAULT_TENANT else None


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str | None = None):
        super().__init__()
        self.service = service or os.getenv("OTEL_SERVICE_NAME") or None

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                    + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.service:
            line["service"] = self.service
        line.update(_trace_ids())
        ids = dict(logcontext.current())
        for provider in _providers:
            try:
                ids.update({k: v for k, v in (provider() or {}).items() if v})
            except Exception:
                pass
        if "org_id" not in ids:
            tenant = _tenant()
            if tenant:
                ids["org_id"] = tenant
        line.update(ids)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            line["exception"] = record.exc_text
        return json.dumps(line, default=str, ensure_ascii=False)


def json_handler(service: str | None = None, stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    return handler


def json_wanted() -> bool:
    return (os.getenv("SYNAPSE_LOG_FORMAT") or "").strip().lower() == "json"


class ArqContentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if record.msg == _ARQ_START and len(args) == 4:
            record.args = (args[0], args[1], "…", args[3])
        elif record.msg == _ARQ_RESULT and len(args) == 3:
            record.args = (args[0], args[1], "…")
        elif record.msg == _ARQ_FAILED and len(args) == 4:
            record.args = (args[0], args[1], args[2], "…")
            if record.exc_info and record.exc_info[1] is not None:
                record.exc_text = _frames_and_type(record.exc_info)
                record.exc_info = None
        return True


def _frames_and_type(exc_info) -> str:
    """A traceback's frames (code locations) with its message line cut to the
    exception's type — the message is where a tenant's words would be."""
    exc_type, exc, tb = exc_info
    frames = "".join(traceback.format_tb(tb)) if tb is not None else ""
    return f"Traceback (most recent call last):\n{frames}{exc_type.__name__}"


def install_arq_filter() -> None:
    logger = logging.getLogger("arq.worker")
    if not any(isinstance(f, ArqContentFilter) for f in logger.filters):
        logger.addFilter(ArqContentFilter())
