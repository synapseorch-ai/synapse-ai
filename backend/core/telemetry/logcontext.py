"""The ids a log line carries: org, run, session, request.

A contextvar dict, bound by whoever knows them — a request's middleware, a
job's span, a tenant scope — and read by the JSON log formatter. Pure Python:
binding costs a dict copy whether or not telemetry is on, and nothing here
exports anything.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_context: ContextVar[dict] = ContextVar("synapse_log_context", default={})

#: The keys a log line may carry. Ids only — the same rule as spans.
KEYS = ("org_id", "run_id", "root_run_id", "session_id", "request_id", "job_id", "schedule_id")


def bind(**values) -> Token:
    """Add ids to the current context; returns a token for `reset`."""
    current = _context.get()
    merged = dict(current)
    for key, value in values.items():
        if key in KEYS and value not in (None, ""):
            merged[key] = str(value)[:128]
    return _context.set(merged)


def reset(token: Token) -> None:
    try:
        _context.reset(token)
    except ValueError:
        # Reset from another context (an abandoned generator finalised in a
        # new task). The binding dies with the context it was made in.
        pass


def current() -> dict:
    return _context.get()


def clear() -> Token:
    """An empty context, for work that must not inherit its spawner's ids
    (a pooled connection's supervisor outlives the job that opened it)."""
    return _context.set({})
