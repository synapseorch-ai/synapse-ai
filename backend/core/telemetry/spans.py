"""The engine's spans: a run, a step, a chat turn, an agent step, a model call,
a tool call, a compaction — and the metrics recorded where each ends.

Off (no tracing, no metrics), every helper here is a flag check that hands back
the function's own result: the engine runs exactly the code it ran before.

The engine's loops are async generators driven by one task (see
`orchestration/runner.py`, issue #356), and a generator that pauses is
*abandoned* — Python closes it later, in a new task, from the garbage
collector. A span context held current across a `yield` would then be reset in
the wrong task, and leak into whatever the driving task did next. So nothing
here keeps a span current across a yield:

* `traced_generator` attaches the span's context only while the inner
  generator runs (each `__anext__`), and detaches it before passing the event
  up; the span ends when the generator does — or, for the events after which
  the engine stops pulling (`human_input_required`, `orchestration_end`),
  before that event goes up.
* A tool call is a span opened by the agent step's wrapper on the
  `tool_execution` event and closed on the matching `tool_result`: the tool's
  own work — an MCP call, an HTTP request, a delegated agent — runs inside the
  `__anext__` calls between the two, so it is the tool span's child. No line of
  the ReAct loop changes for it.

What a span carries is `attributes.py`'s list: ids, types, counts, cost and
tool names. Never a prompt, an answer, arguments or a result.
"""
from __future__ import annotations

import functools
import re
import time
from contextvars import ContextVar

from core.telemetry import attributes as A
from core.telemetry import metrics, state

_TERMINAL = {"human_input_required", "orchestration_end"}
_SAFE = re.compile(r"[^a-z0-9_ ()./-]+")

#: The model call `log_usage` and `_retry_notice` annotate.
_model_call: ContextVar = ContextVar("synapse_model_call", default=None)


def active() -> bool:
    return state.tracing or state.metering


def _tracer():
    from opentelemetry import trace
    return trace.get_tracer("synapse.engine")


def _is_cancellation(exc: BaseException) -> bool:
    import asyncio
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return True
    try:
        import anyio
        return isinstance(exc, anyio.get_cancelled_exc_class())
    except Exception:
        return False


def _fail(span, exc: BaseException) -> None:
    """Mark a span failed with the exception's type — never its message."""
    from opentelemetry.trace import Status, StatusCode
    if _is_cancellation(exc):
        span.set_attribute(A.CANCELLED, True)
        return
    name = type(exc).__name__
    span.set_status(Status(StatusCode.ERROR, name))
    span.set_attribute("error.type", name)
    span.add_event("exception", {"exception.type": name})


def safe_code(value, limit: int = 64) -> str | None:
    """A short machine-made phrase (a retry reason, a policy category), reduced
    to plain characters. Callers pass code-built strings; this is the belt."""
    if not isinstance(value, str) or not value:
        return None
    return _SAFE.sub("", value.lower())[:limit] or None


def model_bucket(model: str | None, provider: str | None) -> str:
    """The model as a metric dimension: a managed `synapse.*` model by name,
    anything else — a name a user typed — as its provider."""
    if isinstance(model, str) and model.startswith("synapse."):
        return model[:64]
    return f"own-key:{(provider or 'unknown').lower()[:32]}"


class Rec:
    """A span and what the engine learned about it, readable without the span
    (a non-recording span keeps nothing, and metrics need the status)."""

    __slots__ = ("span", "status", "failed", "cancelled", "started", "extra")

    def __init__(self, span):
        self.span = span
        self.status = None
        self.failed = False
        self.cancelled = False
        self.started = time.monotonic()
        self.extra: dict = {}

    def set(self, key: str, value) -> None:
        if value is None or value == "":
            return
        if key == A.STATUS:
            self.status = value
        self.span.set_attribute(key, value)

    def update(self, attributes: dict) -> None:
        for key, value in (attributes or {}).items():
            self.set(key, value)

    def fail(self, code: str) -> None:
        from opentelemetry.trace import Status, StatusCode
        self.failed = True
        self.span.set_status(Status(StatusCode.ERROR, code))

    def raised(self, exc: BaseException) -> None:
        if _is_cancellation(exc):
            self.cancelled = True
        else:
            self.failed = True
        _fail(self.span, exc)

    def seconds(self) -> float:
        return time.monotonic() - self.started

    def outcome(self, default: str = "completed") -> str:
        if self.status:
            return self.status
        return "failed" if self.failed else "cancelled" if self.cancelled else default


# --- generators ------------------------------------------------------------


def traced_generator(name: str, attributes_of, on_event=None, on_end=None):
    """Decorate an async generator function (or method) with a span.

    `attributes_of(*args, **kwargs)` gives the span's starting attributes;
    `on_event(rec, event, tools)` may annotate it per event; `on_end(rec, args,
    kwargs)` once, at the end. Off, the decorated function returns the original
    generator untouched.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            gen = fn(*args, **kwargs)
            if not active():
                return gen
            return _traced(gen, name, attributes_of, on_event, on_end, args, kwargs)
        return wrapper
    return decorate


async def _traced(gen, name, attributes_of, on_event, on_end, args, kwargs):
    from opentelemetry import context, trace

    try:
        start_attributes = attributes_of(*args, **kwargs) or {}
    except Exception:
        start_attributes = {}
    rec = Rec(_tracer().start_span(name))
    rec.update(start_attributes)
    span_context = trace.set_span_in_context(rec.span)
    tools = _ToolCalls(span_context)
    ended = False

    def end():
        nonlocal ended
        if ended:
            return
        ended = True
        tools.close_all()
        if on_end is not None:
            try:
                on_end(rec, args, kwargs)
            except Exception:
                pass
        rec.span.end()

    try:
        while True:
            token = context.attach(tools.context() or span_context)
            try:
                event = await gen.__anext__()
            except StopAsyncIteration:
                break
            except BaseException as exc:
                rec.raised(exc)
                raise
            finally:
                context.detach(token)
            if isinstance(event, dict):
                if on_event is not None:
                    try:
                        on_event(rec, event, tools)
                    except Exception:
                        pass
                if event.get("type") in _TERMINAL:
                    end()
            yield event
    finally:
        end()


class _ToolCalls:
    """The tool call an agent step is in the middle of, opened on its
    `tool_execution` event and closed on its `tool_result`."""

    def __init__(self, parent_context):
        self._parent = parent_context
        self._rec: Rec | None = None
        self._context = None

    def context(self):
        return self._context

    def open(self, tool_name) -> None:
        from opentelemetry import trace
        self.close_all()
        self._rec = Rec(_tracer().start_span("synapse.tool", context=self._parent))
        self._rec.set(A.TOOL_NAME, tool_name[:128] if isinstance(tool_name, str) else None)
        self._context = trace.set_span_in_context(self._rec.span)

    def close(self, event: dict) -> None:
        rec = self._rec
        if rec is None:
            return
        refused = bool(event.get("refused"))
        category = safe_code(event.get("category"), 32)
        rec.set(A.TOOL_REFUSED, refused)
        rec.set(A.TOOL_CATEGORY, category)
        if refused:
            rec.set(A.TOOL_REFUSAL_KIND, refusal_kind(event.get("reason")))
            outcome = "refused"
        else:
            preview = event.get("preview")
            outcome = "error" if isinstance(preview, str) and preview.startswith("Error") else "ok"
        metrics.tool_call(category, outcome)
        self._end()

    def pause(self) -> None:
        if self._rec is not None:
            self._rec.set(A.STATUS, "awaiting_approval")
            metrics.tool_call(None, "awaiting_approval")
            self._end()

    def close_all(self) -> None:
        self._end()

    def _end(self) -> None:
        if self._rec is not None:
            self._rec.span.end()
        self._rec = None
        self._context = None


def refusal_kind(reason) -> str:
    """Why a call was refused, as a code. The reason sentence itself — which can
    quote what a person typed — is never exported."""
    text = reason.lower() if isinstance(reason, str) else ""
    if text.startswith("the person"):
        return "person"
    if "policy" in text:
        return "policy"
    if "not one of this agent" in text:
        return "not_allowed"
    if "memory" in text:
        return "memory"
    if "database" in text or "db" in text.split():
        return "db_allowlist"
    return "other"


# --- the orchestration engine ------------------------------------------------


def traced_executors(executors: dict) -> dict:
    """The engine's step executors, each run inside a `synapse.step` span.

    Every step reaches its executor through `engine.executors` — the engine's
    own loop and the Parallel and Loop executors' sub-steps alike — so wrapping
    the table traces all of them. Off, the table itself comes back.
    """
    if not active():
        return executors
    return {step_type: _TracedExecutor(executor) for step_type, executor in executors.items()}


class _TracedExecutor:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, step, run, engine):
        return _traced(self._inner.execute(step, run, engine), "synapse.step",
                       _step_attributes, _step_events, _step_end, (step, run, engine), {})


def _step_attributes(step, run, engine):
    step_type = getattr(step, "type", None)
    return {
        A.STEP_ID: getattr(step, "id", None),
        A.STEP_TYPE: getattr(step_type, "value", step_type),
        A.RUN_ID: getattr(run, "run_id", None),
        A.AGENT_ID: getattr(step, "agent_id", None),
    }


def _step_events(rec: Rec, event: dict, tools) -> None:
    kind = event.get("type")
    if kind == "human_input_required":
        rec.set(A.STATUS, "paused")
    elif kind == "step_error":
        rec.set(A.STATUS, "failed")
        rec.fail("step_error")


def _step_end(rec: Rec, args, kwargs) -> None:
    step = args[0]
    step_type = getattr(getattr(step, "type", None), "value", None)
    status = rec.outcome()
    rec.set(A.STATUS, status)
    metrics.step(step_type, status, rec.seconds())


def root_run_id(run_id) -> str | None:
    return run_id.split("__", 1)[0] if isinstance(run_id, str) else None


def run_attributes(engine, run, state_, started=None):
    metrics.run_started()
    return {
        A.RUN_ID: run.run_id,
        A.ROOT_RUN_ID: root_run_id(run.run_id),
        A.ORCHESTRATION_ID: getattr(getattr(engine, "orch", None), "id", None),
        A.TRIGGER: "resume" if started is not None or getattr(run, "step_history", None) else "run",
    }


def run_end(rec: Rec, args, kwargs) -> None:
    metrics.run_stopped()
    run = args[1] if len(args) > 1 else kwargs.get("run")
    status = getattr(run, "status", None)
    if isinstance(status, str):
        rec.set(A.STATUS, status)
        if status == "failed":
            rec.fail("run_failed")
    history = getattr(run, "step_history", None)
    if isinstance(history, list):
        rec.set(A.STEPS, len(history))
        cost = 0.0
        for entry in history:
            value = entry.get("cost") if isinstance(entry, dict) else None
            if isinstance(value, (int, float)):
                cost += value
        if cost:
            rec.set(A.COST_USD, round(cost, 6))
    metrics.run_stretch(rec.outcome(), rec.seconds())


# --- the agent loop ------------------------------------------------------------


def agent_step_attributes(message=None, agent_id=None, session_id=None, *args, **kwargs):
    return {
        A.AGENT_ID: agent_id if isinstance(agent_id, str) else None,
        A.SESSION_ID: session_id if isinstance(session_id, str) else None,
        A.RUN_ID: kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None,
    }


def agent_step_events(rec: Rec, event: dict, tools: _ToolCalls) -> None:
    """Tool spans and the turn count, from the agent step's own events."""
    if "delegate_from" in event:
        return  # a delegated agent's events: its own agent step traces them
    kind = event.get("type")
    if kind == "tool_execution":
        tools.open(event.get("tool_name"))
    elif kind == "tool_result":
        tools.close(event)
    elif kind == "human_input_required":
        tools.pause()
        rec.set(A.STATUS, "paused")
    elif kind == "final":
        rec.set(A.STATUS, "completed")
    turn = event.get("turn")
    if isinstance(turn, int):
        rec.set(A.TURN, turn)


def chat_turn_attributes(request=None, *args, **kwargs):
    return {
        A.SESSION_ID: getattr(request, "session_id", None),
        A.AGENT_ID: getattr(request, "agent_id", None),
    }


def chat_turn_events(rec: Rec, event: dict, tools) -> None:
    kind = event.get("type")
    if kind == "human_input_required":
        rec.set(A.STATUS, "paused")
    elif kind == "final" and "delegate_from" not in event:
        rec.set(A.STATUS, "completed")
    elif kind == "error":
        rec.set(A.STATUS, "failed")
        rec.fail("turn_error")


def chat_turn_end(rec: Rec, args, kwargs) -> None:
    status = rec.outcome()
    rec.set(A.STATUS, status)
    metrics.chat_turn(status, rec.seconds())


# --- coroutines ----------------------------------------------------------------


def traced_coroutine(name: str, attributes_of=None, *, model_call: bool = False):
    """Decorate an async function with a span; off, the function's own
    coroutine comes back."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not active():
                return fn(*args, **kwargs)
            return _traced_call(fn, name, attributes_of, model_call, args, kwargs)
        return wrapper
    return decorate


async def _traced_call(fn, name, attributes_of, model_call, args, kwargs):
    from opentelemetry import trace
    try:
        start_attributes = (attributes_of(*args, **kwargs) if attributes_of else {}) or {}
    except Exception:
        start_attributes = {}
    span = _tracer().start_span(name)
    rec = Rec(span)
    rec.update(start_attributes)
    token = _model_call.set(rec) if model_call else None
    try:
        with trace.use_span(span, end_on_exit=True, record_exception=False,
                            set_status_on_exception=False):
            try:
                return await fn(*args, **kwargs)
            except BaseException as exc:
                rec.raised(exc)
                raise
            finally:
                if model_call:
                    status = "failed" if rec.failed else "cancelled" if rec.cancelled else "ok"
                    metrics.model_call(rec.extra.get("bucket"), status, rec.seconds())
    finally:
        if token is not None:
            _model_call.reset(token)


def model_call_attributes(prompt_msg=None, sys_prompt=None, mode=None, current_model=None, *args, **kwargs):
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": current_model[:128] if isinstance(current_model, str) else None,
        A.SESSION_ID: kwargs.get("session_id") if isinstance(kwargs.get("session_id"), str) else None,
        A.AGENT_ID: kwargs.get("agent_id") if isinstance(kwargs.get("agent_id"), str) else None,
        A.RUN_ID: kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None,
    }


def model_usage(*, model, provider, input_tokens, output_tokens, cache_read_tokens=0,
                cache_write_tokens=0, cost=None, response_cache_hit=False) -> None:
    """What a model call used — called by `usage_tracker.log_usage`."""
    rec = _model_call.get()
    if rec is None or not active():
        return
    bucket = model_bucket(model, provider)
    rec.extra["bucket"] = bucket
    rec.update({
        "gen_ai.provider.name": (provider or "").lower()[:32] or None,
        "gen_ai.request.model": model[:128] if isinstance(model, str) else None,
        "gen_ai.usage.input_tokens": int(input_tokens or 0),
        "gen_ai.usage.output_tokens": int(output_tokens or 0),
        A.CACHE_READ_TOKENS: int(cache_read_tokens or 0) or None,
        A.CACHE_WRITE_TOKENS: int(cache_write_tokens or 0) or None,
        A.MODEL_BUCKET: bucket,
    })
    if isinstance(cost, (int, float)):
        rec.set(A.COST_USD, round(float(cost), 8))
    if response_cache_hit:
        rec.set(A.STATUS, "cache_hit")
    metrics.model_tokens(bucket, int(input_tokens or 0), int(output_tokens or 0))


def model_retry(attempt: int, reason) -> None:
    """A retry, as an event on the model call's span — called by `_retry_notice`."""
    rec = _model_call.get()
    if rec is None or not state.tracing:
        return
    attributes = {A.RETRY_ATTEMPT: int(attempt)}
    code = safe_code(reason)
    if code:
        attributes[A.RETRY_REASON] = code
    rec.span.add_event("synapse.retry", attributes)


def compaction_attributes(current_context_text=None, recent_history_messages=None, *args, **kwargs):
    count = len(recent_history_messages) if isinstance(recent_history_messages, list) else None
    session_id = kwargs.get("session_id")
    return {A.MESSAGES_BEFORE: count, A.SESSION_ID: session_id if isinstance(session_id, str) else None}
