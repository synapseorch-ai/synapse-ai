"""Every attribute Synapse's telemetry may export, and nothing else.

This module is the allowlist. The exporter guard (`guard.py`) drops any span
or event attribute whose key is not listed here, so a new attribute is a
decision made in this file — with a line saying what it holds — and never a
side effect of an instrumentation library or a call site.

The rule the list keeps: **ids, timings, counts, cost, step types and tool
names — never content.** No prompt, answer, tool argument, tool result, file
name, URL, query string or error message. A self-hoster's collector is often a
third-party service, and on Cloud every tenant's data would flow through it.

`EXPORTED` is also the source of the self-host docs' attribute table: each
entry is (key, what it holds).

Pure Python on purpose: importing this must not import OpenTelemetry.
"""
from __future__ import annotations

# --- Synapse's own attributes -------------------------------------------------

ORG_ID = "synapse.org_id"
RUN_ID = "synapse.run_id"
ROOT_RUN_ID = "synapse.root_run_id"
SESSION_ID = "synapse.session_id"
AGENT_ID = "synapse.agent_id"
ORCHESTRATION_ID = "synapse.orchestration_id"
STEP_ID = "synapse.step.id"
STEP_TYPE = "synapse.step.type"
STATUS = "synapse.status"
TRIGGER = "synapse.trigger"
COST_USD = "synapse.cost_usd"
STEPS = "synapse.steps"
TURN = "synapse.turn"
TOOL_NAME = "synapse.tool.name"
TOOL_CATEGORY = "synapse.tool.category"
TOOL_REFUSED = "synapse.tool.refused"
TOOL_REFUSAL_KIND = "synapse.tool.refusal_kind"
JOB_NAME = "synapse.job.name"
JOB_ID = "synapse.job.id"
JOB_TRY = "synapse.job.try"
REQUEST_ID = "synapse.request_id"
SCHEDULE_ID = "synapse.schedule_id"
SURFACE = "synapse.surface"
MODEL_BUCKET = "synapse.model.bucket"
CACHE_READ_TOKENS = "synapse.tokens.cache_read"
CACHE_WRITE_TOKENS = "synapse.tokens.cache_write"
RETRY_ATTEMPT = "synapse.retry.attempt"
RETRY_REASON = "synapse.retry.reason"
MESSAGES_BEFORE = "synapse.compaction.messages_before"
MESSAGES_AFTER = "synapse.compaction.messages_after"
CANCELLED = "synapse.cancelled"

EXPORTED: tuple[tuple[str, str], ...] = (
    (ORG_ID, "the workspace (tenant) id; Cloud only — a self-hosted install has one"),
    (RUN_ID, "an orchestration run's id"),
    (ROOT_RUN_ID, "the id of the run a nested run was started by"),
    (SESSION_ID, "a chat conversation's id"),
    (AGENT_ID, "the agent's id"),
    (ORCHESTRATION_ID, "the orchestration's id"),
    (STEP_ID, "a step's id within its orchestration"),
    (STEP_TYPE, "a step's type: agent, llm, tool, parallel, loop, human, …"),
    (STATUS, "how a run, step or turn ended: completed, failed, paused, cancelled"),
    (TRIGGER, "what started a run: run, resume, resume_failed"),
    (COST_USD, "the model cost of a call or a run, in US dollars"),
    (STEPS, "how many steps a run executed"),
    (TURN, "the agent loop's turn number"),
    (TOOL_NAME, "the tool's name, as the agent sees it"),
    (TOOL_CATEGORY, "where the tool runs: builtin, mcp, http, python, delegate, subtask, builder"),
    (TOOL_REFUSED, "true when policy, an allowlist or a person refused the call"),
    (TOOL_REFUSAL_KIND, "why, as a code: not_allowed, policy, person, memory, db_allowlist"),
    (JOB_NAME, "the queued job's function name (scale mode)"),
    (JOB_ID, "the queued job's id (scale mode)"),
    (JOB_TRY, "which attempt at the job this is (scale mode)"),
    (REQUEST_ID, "the request's x-request-id, or the trace id when none was sent"),
    (SCHEDULE_ID, "the schedule that started the work"),
    (SURFACE, "where a chat came from: app, api, widget, channel"),
    (MODEL_BUCKET, "the model, or own-key:<provider> for a model name a user typed"),
    (CACHE_READ_TOKENS, "prompt tokens served from the provider's cache"),
    (CACHE_WRITE_TOKENS, "prompt tokens written to the provider's cache"),
    (RETRY_ATTEMPT, "a model call's retry number (on a retry event)"),
    (RETRY_REASON, "why a model call was retried, as a code (on a retry event)"),
    (MESSAGES_BEFORE, "messages in the context before a compaction"),
    (MESSAGES_AFTER, "messages in the context after a compaction"),
    (CANCELLED, "true when the work was stopped rather than finished"),
    # OpenTelemetry's GenAI conventions, for model calls.
    ("gen_ai.provider.name", "the model provider: openai, anthropic, gemini, bedrock, …"),
    ("gen_ai.system", "the model provider (the older name for gen_ai.provider.name)"),
    ("gen_ai.operation.name", "chat"),
    ("gen_ai.request.model", "the model's name as configured (metrics use the model bucket instead)"),
    ("gen_ai.usage.input_tokens", "prompt tokens"),
    ("gen_ai.usage.output_tokens", "completion tokens"),
    ("gen_ai.response.finish_reasons", "why the model stopped: stop, tool_calls, length, …"),
    ("gen_ai.request.max_tokens", "the response cap sent to the model"),
    ("gen_ai.request.temperature", "the sampling temperature sent to the model"),
    ("gen_ai.request.top_p", "the nucleus-sampling value sent to the model"),
    # HTTP, database, queue and AWS conventions from the instrumentation libraries.
    ("http.request.method", "GET, POST, …"),
    ("http.response.status_code", "the HTTP status"),
    ("http.route", "the route's template, e.g. /api/orchestrations/{orch_id} — never the path"),
    ("url.scheme", "http or https"),
    ("network.protocol.version", "the HTTP version"),
    ("server.address", "an internal service's host name, or 'external' for any other host"),
    ("server.port", "an internal service's port"),
    ("error.type", "an error's class name or HTTP status — never its message"),
    ("exception.type", "an exception's class name (on an exception event) — never its message"),
    ("exception.escaped", "whether the exception left the span"),
    ("db.system", "postgresql, sqlite or redis"),
    ("db.system.name", "postgresql, sqlite or redis (the newer name)"),
    ("db.operation", "SELECT, INSERT, … or a Redis command — never the statement"),
    ("db.operation.name", "SELECT, INSERT, … or a Redis command (the newer name)"),
    ("db.redis.database_index", "the Redis database number"),
    ("messaging.system", "arq"),
    ("messaging.operation.type", "send or process"),
    ("messaging.destination.name", "the queue's name"),
    ("rpc.system", "aws-api"),
    ("rpc.service", "the AWS service, e.g. BedrockRuntime"),
    ("rpc.method", "the AWS operation, e.g. Converse"),
    ("cloud.region", "the AWS region"),
    ("aws.region", "the AWS region (the older name)"),
    ("aws.request_id", "the AWS request id"),
    ("retry_attempts", "how many times the AWS SDK retried"),
)

ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(k for k, _ in EXPORTED)

#: Span events that may be exported; their attributes pass the same allowlist.
ALLOWED_EVENTS: frozenset[str] = frozenset({"exception", "synapse.retry"})

#: Instrumentation scopes whose spans may be exported. Everything else — a
#: library that picked up the global tracer provider on its own — is dropped,
#: because its span names and attributes were never reviewed.
ALLOWED_SCOPE_PREFIXES: tuple[str, ...] = (
    "synapse",
    "opentelemetry.instrumentation.asgi",
    "opentelemetry.instrumentation.fastapi",
    "opentelemetry.instrumentation.starlette",
    "opentelemetry.instrumentation.httpx",
    "opentelemetry.instrumentation.redis",
    "opentelemetry.instrumentation.sqlalchemy",
    "opentelemetry.instrumentation.botocore",
)

#: Metric attributes (dimensions). Low cardinality only: never an org, run,
#: session or orchestration id, and never a user-typed model name.
METRIC_ATTRIBUTES: frozenset[str] = frozenset({
    "service", STEP_TYPE, STATUS, JOB_NAME, TOOL_CATEGORY, MODEL_BUCKET, "direction",
    "outcome", "queue", "http.request.method", "http.response.status_code", "http.route",
})

MAX_STRING = 256
