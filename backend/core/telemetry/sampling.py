"""Which spans are recorded.

`DropParentlessClient` wraps the standard sampler (OTEL_TRACES_SAMPLER, default
parent-based always-on) with one rule: an outbound call with no parent is not
recorded. A worker's queue polls, heartbeats and connection-pool pings are
CLIENT spans nobody started, and would otherwise be thousands of one-span root
traces an hour. Background work that is wanted gets an explicit root span, and
its calls are then children of it.
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import sampling as _sampling
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import SpanKind


class DropParentlessClient(Sampler):
    def __init__(self, inner: Sampler):
        self._inner = inner

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None,
                      links=None, trace_state=None) -> SamplingResult:
        if kind == SpanKind.CLIENT:
            parent = trace.get_current_span(parent_context).get_span_context()
            if not parent.is_valid:
                return SamplingResult(Decision.DROP)
        return self._inner.should_sample(parent_context, trace_id, name, kind, attributes,
                                         links, trace_state)

    def get_description(self) -> str:
        return f"DropParentlessClient{{{self._inner.get_description()}}}"


def sampler() -> Sampler:
    return DropParentlessClient(_sampling._get_from_env_or_default())
