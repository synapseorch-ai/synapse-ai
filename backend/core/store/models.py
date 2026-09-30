"""
SQLAlchemy ORM models for the engine's durable state.

These tables are the engine's **only** source of truth for orchestrations,
agents, custom tools, MCP servers, settings and run state. There is no JSON
mirror and nothing to "sync" — see ``core/store/__init__.py``.

Dialect portability
-------------------
The same models run on SQLite (the default single-tenant install) and on
Postgres (scale deployments and embedders). Two rules keep that true:

* ``JSONType`` renders ``JSONB`` on Postgres and ``JSON`` on everything else.
  Every column here stores and retrieves whole documents; none of them is
  queried with a JSON path operator, so the Postgres-only indexing that JSONB
  buys is not being given up. The one exception is ``knowledge_chunks.meta``,
  which a Knowledge metadata filter reads key by key — only through
  ``core/knowledge/filters_sql.py``, whose constructs render per dialect.
* ``Uuid(as_uuid=True)`` is SQLAlchemy 2.0's dialect-agnostic UUID. It renders
  the native ``UUID`` type on Postgres — byte-identical to the previous
  ``postgresql.UUID`` — and ``CHAR(32)`` elsewhere.

Do not reintroduce a bare ``from sqlalchemy.dialects.postgresql import ...``
here. A Postgres-only type in this module silently makes SQLite installs
unrunnable, and the failure surfaces at ``create_all`` on a user's laptop
rather than in CI.

Tenancy
-------
``tenant_id`` is NOT NULL on every table that holds customer data, and it is
part of the primary key wherever the natural key is a user-chosen name
(``mcp_servers.name``, ``scale_settings.key``). Without that, two tenants
cannot both have a server called ``github`` or a setting called ``model``.

The shipped single-tenant product writes one constant value here and offers no
way to write another — see ``core/tenancy.py``.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index,
    Integer, LargeBinary, PrimaryKeyConstraint, String, Text, UniqueConstraint, Uuid, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


#: JSONB on Postgres, JSON everywhere else. Safe to share across columns —
#: SQLAlchemy type instances are immutable and reusable.
JSONType = JSON().with_variant(JSONB, "postgresql")


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: The tenant every single-tenant install writes. Also the server-side default,
#: so a row inserted by code that predates tenancy lands somewhere valid rather
#: than violating NOT NULL.
DEFAULT_TENANT = "default"


def _tenant_column() -> Column:
    return Column(
        String(255), default=DEFAULT_TENANT, server_default=DEFAULT_TENANT, nullable=False
    )


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

class OrchestrationDB(Base):
    __tablename__ = "orchestrations"

    id = Column(String(255), primary_key=True)
    name = Column(String(500), nullable=False)
    description = Column(Text, default="")
    definition = Column(JSONType, nullable=False)   # full Orchestration model_dump()
    tenant_id = _tenant_column()
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        Index("idx_orchestrations_tenant", "tenant_id"),
        Index("idx_orchestrations_updated", "updated_at"),
    )


class AgentDB(Base):
    __tablename__ = "agents"

    id = Column(String(255), primary_key=True)
    name = Column(String(500), nullable=False)
    definition = Column(JSONType, nullable=False)   # full Agent dict
    tenant_id = _tenant_column()
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_agents_tenant", "tenant_id"),
    )


class ToolDB(Base):
    __tablename__ = "tools"

    id = Column(String(255), primary_key=True)
    name = Column(String(500), nullable=False)
    definition = Column(JSONType, nullable=False)   # full custom tool dict
    tenant_id = _tenant_column()
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_tools_tenant", "tenant_id"),
    )


class MCPServerDB(Base):
    """An MCP server registration.

    ``name`` is chosen by the user, so it is unique *per tenant*, never
    globally. A single-column key here means the second tenant to register
    ``github`` gets a primary-key violation naming a row they are not allowed
    to see — a failure that is both wrong and unexplainable.
    """
    __tablename__ = "mcp_servers"

    tenant_id = _tenant_column()
    name = Column(String(255), nullable=False)
    label = Column(String(500), nullable=False, default="")
    definition = Column(JSONType, nullable=False)   # full config dict (token stripped)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "name", name="mcp_servers_pkey"),
    )


class SettingDB(Base):
    """Key-value settings store, scoped per tenant.

    ``key`` is a bare setting name like ``model``. Keyed globally, one tenant
    setting ``model`` would take the only row that exists.
    """
    __tablename__ = "scale_settings"

    tenant_id = _tenant_column()
    key = Column(String(255), nullable=False)
    value = Column(Text, nullable=False)         # JSON-encoded
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "key", name="scale_settings_pkey"),
    )


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

class OrchestrationRunDB(Base):
    """A run's durable state.

    ``shared_state`` and ``step_history`` are the run itself, not a summary of
    it: a paused run is resumed by reading these columns. They were previously
    written as empty literals while the real state went to a JSON file on the
    worker's local disk, which is why a run could only ever resume on the
    process that started it.
    """
    __tablename__ = "orchestration_runs"

    run_id = Column(String(255), primary_key=True)
    orchestration_id = Column(String(255), nullable=False, index=True)
    session_id = Column(String(255))
    tenant_id = _tenant_column()
    status = Column(String(50), nullable=False, default="running")
    shared_state = Column(JSONType, nullable=False, default=dict)
    step_history = Column(JSONType, nullable=False, default=list)
    current_step_id = Column(String(255))
    # The run's task list (core/task_list.py). Nullable because the column is
    # migration-added on existing installs; readers treat NULL as [].
    tasks = Column(JSONType, default=list)
    # Mid-step progress per step id (the agent transcript checkpointed after
    # every tool turn — up to 200 KB each). Its own column so a per-turn
    # checkpoint updates this and nothing else; it used to live inside
    # shared_state, where every turn rewrote the whole run. Nullable for the
    # same reason as `tasks`; readers treat NULL as {}.
    step_progress = Column(JSONType, default=dict)
    waiting_for_human = Column(Boolean, default=False)
    human_prompt = Column(Text)
    human_fields = Column(JSONType, default=list)
    nested_run_id = Column(String(255))
    nested_orch_id = Column(String(255))

    # Cost & token tracking
    total_tokens_used = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    cache_hit_count = Column(Integer, default=0)
    estimated_savings_usd = Column(Float, default=0.0)

    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=_now)

    # Worker attribution
    worker_id = Column(String(255))
    job_id = Column(String(255))

    # Webhook for async delivery
    webhook_url = Column(Text)
    webhook_secret = Column(Text)

    __table_args__ = (
        Index("idx_runs_status", "status"),
        Index("idx_runs_tenant_status", "tenant_id", "status", "created_at"),
        Index("idx_runs_worker", "worker_id"),
        # Partial index: quickly find all paused runs waiting for human
        Index("idx_runs_waiting_human", "waiting_for_human"),
        # list_runs() orders by recency within a tenant on every call.
        Index("idx_runs_tenant_created", "tenant_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

class ApiKeyDB(Base):
    """An API key, stored as a SHA-256 hash of the key it authenticates.

    A real table rather than a document collection, because this one *is*
    queried: every authenticated request looks a key up by its hash. As a
    document blob that means loading every key the tenant owns and scanning
    them in Python on each request; here it is an indexed point lookup.

    `key_hash` is unique globally, not per tenant. The tenant is a property of
    the key, not part of its identity — a request arrives with nothing but the
    key, so the lookup is what *establishes* the tenant and cannot be scoped by
    one. 256 bits of hash makes a collision across tenants not a practical
    concern; a per-tenant key would allow the same key to authenticate as two
    different tenants, which is worse.
    """
    __tablename__ = "api_keys"

    id = Column(String(255), primary_key=True)
    tenant_id = _tenant_column()
    name = Column(String(500), nullable=False, default="")
    key_hash = Column(String(64), nullable=False, unique=True)
    key_prefix = Column(String(32), nullable=False, default="")   # for display
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    last_used_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_api_keys_hash", "key_hash"),
        Index("idx_api_keys_tenant", "tenant_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Small document collections
# ---------------------------------------------------------------------------

class CollectionDB(Base):
    """A generic per-tenant document collection.

    Repos, database configs, API keys, personal details and messaging channels
    were five JSON files. None of them is ever queried by anything but its own
    key — no filtering, no ordering beyond insertion, no joins — so five tables
    would be five schemas to migrate for no query the engine actually makes.

    Collections that *are* queried get real tables instead: schedules are
    selected by "enabled and overdue" on every scheduler tick, and usage logs
    are aggregated. Those do not belong here.
    """
    __tablename__ = "collections"

    tenant_id = _tenant_column()
    collection = Column(String(100), nullable=False)   # "repos", "api_keys", …
    key = Column(String(255), nullable=False)          # item id, or "" for singletons
    value = Column(JSONType, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "collection", "key", name="collections_pkey"),
        Index("idx_collections_tenant_name", "tenant_id", "collection", "created_at"),
    )


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

class UsageLogDB(Base):
    """One row per LLM call (and per compaction event).

    An append-per-call event log with aggregation over it, which is the other
    thing ``CollectionDB`` is explicitly not for. As a JSON file every call
    read the whole list, appended and wrote it all back — on the per-turn path,
    so the cost of logging a call grew with the number of calls ever made.

    Compaction events share the table because they share every query: the
    usage screen lists both, and ``event_type`` is the discriminator every
    aggregate already filters on. Their extra fields live in ``details``
    rather than as columns no LLM call would ever fill.
    """
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = _tenant_column()
    timestamp = Column(DateTime(timezone=True), nullable=False, default=_now)
    event_type = Column(String(50))          # NULL for an LLM call, "compaction" otherwise
    source = Column(String(100), default="chat")
    model = Column(String(255), default="")
    provider = Column(String(100), default="")
    session_id = Column(String(255), default="unknown")
    agent_id = Column(String(255), default="unknown")
    run_id = Column(String(255))
    tool_name = Column(String(255))
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    context_chars = Column(Integer, default=0)
    estimated_cost = Column(Float, default=0.0)
    latency_seconds = Column(Float, default=0.0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    estimated_savings = Column(Float, default=0.0)
    response_cache_hit = Column(Boolean, default=False)
    details = Column(JSONType)               # compaction-only fields

    __table_args__ = (
        Index("idx_usage_tenant_time", "tenant_id", "timestamp"),
        Index("idx_usage_tenant_session", "tenant_id", "session_id"),
        Index("idx_usage_tenant_run", "tenant_id", "run_id"),
    )


class ModelPricingDB(Base):
    """The per-model rate card.

    Deliberately **not** tenant data, like ``WorkerDB`` above. It is what a
    model costs, which is the same number for everyone — so a tenant column
    would give every org its own copy of one table and mean an operator
    updating a price reached nobody. In cloud this is staff-managed; in a
    self-hosted install it is whatever the user edits on the usage screen.

    Seeded from ``core/model_pricing.py`` at startup, inserting only the models
    that are missing, so neither an edit nor a future automated price feed is
    undone by a restart.
    """
    __tablename__ = "model_pricing"

    model = Column(String(255), primary_key=True)
    entry = Column(JSONType, nullable=False)   # {provider, input_per_1m, output_per_1m, ...}
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------

class EvalScoreDB(Base):
    """One judge verdict on one run, under one version of one rubric.

    A real table rather than a ``CollectionDB`` slot for the reason that class's
    own docstring gives: these rows are *queried*. The dashboard asks for a
    run's scores, and for an orchestration's scores in time order — which is
    both the trend and the baseline the next verdict is measured against.

    **One row per (run, rubric version), never an update in place.** Re-scoring
    a run after the rubric changed appends; it does not overwrite. That is the
    whole mechanism behind "a rubric edit does not silently rewrite history":
    two scores for one run are not a conflict, they are two different questions
    asked of the same work, and the trend line breaks between versions rather
    than pretending they are comparable.

    ``baseline``, ``regression`` and ``rule`` are **stored, not derived on
    read.** The baseline is a function of which runs had been scored at the
    moment of judging, so recomputing it later would quietly change what the
    dashboard said yesterday — the same failure an unversioned rubric causes,
    arriving by a different door. What fired is recorded with the verdict.
    """
    __tablename__ = "eval_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = _tenant_column()
    run_id = Column(String(255), nullable=False)
    orchestration_id = Column(String(255), nullable=False)
    rubric_id = Column(String(255), nullable=False)
    #: The rubric's `version` at judging time. Scores are only comparable
    #: within one version — see the class docstring.
    rubric_version = Column(Integer, nullable=False, default=1)

    score = Column(Float, nullable=False, default=0.0)
    #: {criterion_id: {"score": float, "reasoning": str}}
    per_criterion = Column(JSONType, default=dict)
    #: {step_id: {"score": float, "reasoning": str}} — the same judge call's
    #: step attribution. Empty when the run had no agent steps to attribute to.
    per_step = Column(JSONType, default=dict)
    reasoning = Column(Text, default="")

    judged_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    judge_model = Column(String(255), default="")
    #: Set when this score belongs to a golden-set suite run, so suite results
    #: can be aggregated without walking every run the org ever made.
    suite_run_id = Column(String(255))

    baseline = Column(Float)
    regression = Column(Boolean, nullable=False, default=False)
    #: Which rule fired: "floor" or "drop". NULL when nothing did.
    rule = Column(String(50))

    cost_usd = Column(Float, default=0.0)
    latency_seconds = Column(Float, default=0.0)

    __table_args__ = (
        Index("idx_eval_scores_tenant_run", "tenant_id", "run_id"),
        # The trend query and the baseline query are the same query.
        Index("idx_eval_scores_tenant_orch", "tenant_id", "orchestration_id", "judged_at"),
        Index("idx_eval_scores_tenant_suite", "tenant_id", "suite_run_id"),
    )


class EvalFeedbackDB(Base):
    """A person's thumb on a run or on one chat turn.

    The counterpart to a judge score, and the only ground truth in the product:
    without it a judge's number can be trusted or not, but never *measured*.
    The dashboard's agreement figure is the join of this table and
    ``eval_scores`` on ``run_id``.

    **Exactly one target per row.** A run thumb sets ``run_id``; a chat thumb
    sets ``session_id`` and ``turn_index``. ``turn_index`` is an index into
    ``chat_sessions.messages`` — the same join ``chat_attachments`` uses,
    because the worker owns that list and writes plain ``{role, content}``
    entries with nothing in a message to key on.
    """
    __tablename__ = "eval_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = _tenant_column()
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    #: Who left it, for display and for "change your own mind". Opaque here:
    #: the engine has no user table, and cloud passes its own user id.
    user_ref = Column(String(255), default="")

    run_id = Column(String(255))
    session_id = Column(String(255))
    turn_index = Column(Integer)

    #: +1 or -1. A SMALLINT rather than a boolean so "no opinion" is the
    #: absence of a row and cannot be confused with a thumbs-down.
    verdict = Column(Integer, nullable=False)
    comment = Column(Text)

    __table_args__ = (
        Index("idx_eval_feedback_tenant_run", "tenant_id", "run_id"),
        Index("idx_eval_feedback_tenant_turn", "tenant_id", "session_id", "turn_index"),
    )


class EvalSuiteRunDB(Base):
    """One execution of a golden set — N cases run and scored together.

    A suite run is not a new kind of execution: each case is an ordinary run of
    the orchestration, admitted and metered like any other, and scored by the
    same terminal hook because the orchestration already carries a rubric. This
    row is the thing that makes N runs answerable as one question, and it is
    updated as cases land so a half-finished suite is still readable.
    """
    __tablename__ = "eval_suite_runs"

    id = Column(String(255), primary_key=True)
    tenant_id = _tenant_column()
    suite_id = Column(String(255), nullable=False)
    orchestration_id = Column(String(255), nullable=False)

    status = Column(String(50), nullable=False, default="running")
    case_count = Column(Integer, nullable=False, default=0)
    scored_count = Column(Integer, nullable=False, default=0)
    mean_score = Column(Float)

    rubric_id = Column(String(255), default="")
    rubric_version = Column(Integer, default=1)

    started_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    ended_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_eval_suite_runs_tenant_suite", "tenant_id", "suite_id", "started_at"),
    )


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

class ScheduleDB(Base):
    """A scheduled agent or orchestration run.

    One of the collections that earns a real table rather than a slot in
    ``CollectionDB``: the scheduler asks "enabled and overdue" of the whole
    table every 30 seconds, and each fire writes one row's ``next_run_at``.
    As a JSON document that is a whole-file read plus two whole-file rewrites
    per fire — and it had two independent writers holding two independent
    locks over one file, so the second rewrite could clobber an edit made
    through the API while the run was in flight.

    ``enabled``, ``next_run_at`` and ``last_run_at`` are columns rather than
    keys inside ``definition`` because they are what the tick selects and
    updates. Everything the user authored stays in ``definition``.
    """
    __tablename__ = "schedules"

    id = Column(String(255), primary_key=True)
    tenant_id = _tenant_column()
    name = Column(String(500), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    definition = Column(JSONType, nullable=False)   # the user-authored fields
    next_run_at = Column(DateTime(timezone=True))
    last_run_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_schedules_due", "tenant_id", "enabled", "next_run_at"),
    )


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

class ChatSessionDB(Base):
    """One conversation: a session id, under one agent, for one tenant.

    ``agent_id`` is part of the key, not an attribute of it. The UI keeps a
    single session id per browser and switches agents underneath it, so the
    same session id genuinely names several conversations — the history sidebar
    lists them separately and labels each with its agent. Keyed on
    ``session_id`` alone they would collapse into one row, which is not only a
    lost sidebar entry: the next turn would feed one agent the other agent's
    conversation.

    ``messages`` is ``[{role, content}]``. Entries may also carry ``tools`` and
    ``timestamp``; readers that do not know about those ignore them, which is
    what keeps the v2 chat API's response shape unchanged.
    """
    __tablename__ = "chat_sessions"

    session_id = Column(String(255), nullable=False)
    agent_id = Column(String(255), nullable=False, default="default", server_default="default")
    tenant_id = _tenant_column()
    status = Column(String(50), default="idle")   # idle | running | completed | failed
    messages = Column(JSONType, nullable=False, default=list)
    cli_session_ids = Column(JSONType, default=dict)   # provider key → CLI session id
    last_message_at = Column(DateTime(timezone=True))
    worker_id = Column(String(255))
    job_id = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=_now)

    # Webhook for async delivery
    webhook_url = Column(Text)
    webhook_secret = Column(Text)

    # A tool call this conversation is waiting on a person to allow, together
    # with the turn-boundary snapshot needed to carry on afterwards.
    #
    # Persisted rather than held in memory because the snapshot is the point:
    # without it, resuming means re-running every tool that already succeeded
    # in that turn, side effects included. An orchestration run keeps the same
    # thing on `step_progress`; a chat turn has no run row to keep it on.
    pending_approval = Column(JSONType)

    __table_args__ = (
        # Deliberately unnamed: this is the one table core/store/migrate.py
        # rebuilds, and a named constraint would collide with the live table's
        # while the staging copy exists.
        PrimaryKeyConstraint("tenant_id", "session_id", "agent_id"),
        Index("idx_chat_tenant_time", "tenant_id", "last_message_at"),
    )


# ---------------------------------------------------------------------------
# Workers registry — fleet infrastructure, deliberately not tenant data
# ---------------------------------------------------------------------------

class WorkerDB(Base):
    __tablename__ = "workers"

    worker_id = Column(String(255), primary_key=True)
    hostname = Column(String(500))
    address = Column(String(500))    # http://host:port — health check endpoint
    status = Column(String(50), default="online")  # online | offline | draining
    capabilities = Column(JSONType, default=dict)
    active_jobs = Column(Integer, default=0)
    max_jobs = Column(Integer, default=10)
    last_heartbeat = Column(DateTime(timezone=True), default=_now)
    registered_at = Column(DateTime(timezone=True), default=_now)
    mcp_disabled = Column(JSONType, default=list)   # MCP server names unavailable


# ---------------------------------------------------------------------------
# Dead letter queue
# ---------------------------------------------------------------------------

class DeadLetterQueueDB(Base):
    """Jobs that died outside the engine — see `core.scale.worker._dead_job`.

    A run that fails at a step is not here: the engine records it and the job
    ends normally. Retrying an entry resumes its run (`resume_failed_job`).

    ``job_payload`` is a run's input, so these rows are tenant data even though
    the queue itself is fleet infrastructure. Without ``tenant_id`` an operator
    surface over this table shows every tenant's failed inputs to whoever opens
    it.
    """
    __tablename__ = "dead_letter_queue"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = _tenant_column()
    run_id = Column(String(255))
    orchestration_id = Column(String(255))
    job_function = Column(String(255))
    job_payload = Column(JSONType, nullable=False)
    error_message = Column(Text)
    error_traceback = Column(Text)
    attempt_count = Column(Integer, default=0)
    first_failed_at = Column(DateTime(timezone=True), default=_now)
    last_failed_at = Column(DateTime(timezone=True), default=_now)
    resolved = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_dlq_resolved", "resolved", "last_failed_at"),
        Index("idx_dlq_tenant", "tenant_id", "last_failed_at"),
    )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

class TenantDB(Base):
    __tablename__ = "tenants"

    tenant_id = Column(String(255), primary_key=True)
    name = Column(String(500))
    max_concurrent_runs = Column(Integer, default=100)
    max_queued_runs = Column(Integer, default=1000)
    priority = Column(Integer, default=0)   # higher = dedicated workers
    created_at = Column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------
# Knowledge — document retrieval (D153; MEMORY/16-knowledge.md in the cloud repo)
# ---------------------------------------------------------------------------
#
# FROZEN CONTRACT (KB0, D153): change these through a contract-change request.
#
# The engine owns these five tables in every deployment. Cloud mirrors them
# column for column (plus `org_id` and FORCE RLS, as it does the eval tables)
# and adds two Postgres-only columns of its own to `knowledge_chunks` — an
# untyped `vector` and a generated `tsvector` — which is why neither is here:
# a Postgres-only type in this module would stop SQLite installs at
# `create_all`. The search index (`core.knowledge.seams.KnowledgeIndex`) is the
# only pluggable part; these records never move to an external store.
#
# The CHECK values below are spelled out, not imported: this module must not
# import `core.knowledge`. `test_knowledge_tables.py` holds them equal to the
# tuples there.

#: `BIGINT` on Postgres; on SQLite exactly `INTEGER`, which makes the column the
#: table's rowid. A rowid that is not an INTEGER PRIMARY KEY may be renumbered
#: by VACUUM, and SQLite's FTS5 external-content index maps rows by rowid.
_BigId = BigInteger().with_variant(Integer, "sqlite")


class KnowledgeDocumentDB(Base):
    """One file (or fetched page, or record) inside a knowledge base's Vault folder."""

    __tablename__ = "knowledge_documents"

    id = Column(String(255), primary_key=True)                # kdoc_<32 hex>
    tenant_id = _tenant_column()
    kb_id = Column(String(255), nullable=False)
    path = Column(String(1024), nullable=False)               # Vault-relative, NFC, POSIX; ≤ 1024 UTF-8 bytes
    source = Column(String(20), nullable=False)               # upload | web | record | vault
    source_url = Column(String(2048))
    status = Column(String(20), nullable=False, default="queued")
    status_reason = Column(String(64))
    error = Column(Text)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True))
    sha256 = Column(String(64))
    size_bytes = Column(BigInteger)
    mime = Column(String(255))
    etag = Column(String(255))
    source_modified_at = Column(DateTime(timezone=True))
    version = Column(Integer, nullable=False, default=0)     # bumps each time new content is indexed
    title = Column(String(1000))
    doc_date = Column(String(32))                             # YYYY-MM-DD
    language = Column(String(16))
    meta = Column(JSONType)                                   # the document's own metadata (API: `metadata`)
    report = Column(JSONType)                                 # core.knowledge.status.IngestionReport
    parser = Column(String(100))
    parse_cache_key = Column(String(255))
    snapshot_id = Column(String(255))                         # the snapshot it was last indexed into
    pages = Column(Integer)
    chunks = Column(Integer)
    tokens = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    queued_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    indexed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("source IN ('upload', 'web', 'record', 'vault')",
                        name="ck_knowledge_documents_source"),
        CheckConstraint("status IN ('queued', 'parsing', 'indexing', 'indexed', 'partial', 'failed', "
                        "'skipped', 'paused', 'blocked', 'deleting')",
                        name="ck_knowledge_documents_status"),
        UniqueConstraint("tenant_id", "kb_id", "path", name="uq_knowledge_documents_tenant_kb_path"),
        Index("idx_knowledge_documents_tenant_kb_status", "tenant_id", "kb_id", "status"),
        Index("idx_knowledge_documents_tenant_sha", "tenant_id", "sha256"),
    )


class KnowledgeChunkDB(Base):
    """One chunk of one document version in one snapshot.

    `id` is the row key (the rowid on SQLite); `chunk_id` is the chunk's
    deterministic public id (`core.knowledge.ids.chunk_id`), which the search
    index, traces, citations and the API use. The built-in filter fields
    (path … indexed_at) are copied from the document so a filter runs on this
    table alone, inside the retrieval query.
    """

    __tablename__ = "knowledge_chunks"

    id = Column(_BigId, primary_key=True, autoincrement=True)
    chunk_id = Column(String(64), nullable=False)             # kch_<32 hex>
    tenant_id = _tenant_column()
    kb_id = Column(String(255), nullable=False)
    doc_id = Column(String(255), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False)
    snapshot_id = Column(String(255), nullable=False)
    doc_version = Column(Integer, nullable=False)
    doc_sha256 = Column(String(64))
    ordinal = Column(Integer, nullable=False)
    kind = Column(String(20), nullable=False, default="text")  # text | table | sheet_card
    role = Column(String(10), nullable=False, default="leaf")  # leaf (searched) | parent (expanded into)
    parent_id = Column(String(64))                            # a parent's chunk_id
    content = Column(Text, nullable=False)
    context = Column(Text, nullable=False, default="")        # the enrichment prefix
    heading_path = Column(JSONType)
    page_start = Column(Integer)
    page_end = Column(Integer)
    char_start = Column(Integer, nullable=False)
    char_end = Column(Integer, nullable=False)
    bbox = Column(JSONType)
    tokens = Column(Integer)
    lang = Column(String(32), nullable=False, default="simple")  # a core.knowledge.textsearch language
    block_ids = Column(JSONType)
    path = Column(String(1024))
    source = Column(String(20))
    mime = Column(String(255))
    title = Column(String(1000))
    doc_date = Column(String(32))
    indexed_at = Column(DateTime(timezone=True))
    meta = Column(JSONType)                                   # document metadata ∪ chunk metadata
    embedding = Column(LargeBinary)                           # float32 LE (core.knowledge.vectors); SQL index only
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        CheckConstraint("kind IN ('text', 'table', 'sheet_card')", name="ck_knowledge_chunks_kind"),
        CheckConstraint("role IN ('leaf', 'parent')", name="ck_knowledge_chunks_role"),
        UniqueConstraint("tenant_id", "chunk_id", name="uq_knowledge_chunks_tenant_chunk"),
        Index("idx_knowledge_chunks_tenant_scope", "tenant_id", "kb_id", "snapshot_id"),
        Index("idx_knowledge_chunks_tenant_doc", "tenant_id", "doc_id", "snapshot_id", "ordinal"),
    )


class KnowledgeSnapshotDB(Base):
    """One version of a knowledge base's index: what model, chunker and enrichment made it.

    A settings change that reshapes the index builds a new snapshot beside the
    serving one (blue/green); at most one is `active` per knowledge base.
    """

    __tablename__ = "knowledge_snapshots"

    id = Column(String(255), primary_key=True)                # ksnap_<32 hex>
    tenant_id = _tenant_column()
    kb_id = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="building")
    signature = Column(String(64))                            # core.knowledge.kb_settings.index_signature
    embed_model = Column(String(255))
    embed_dims = Column(Integer)
    embed_profile_version = Column(String(50))
    chunker_version = Column(String(50))
    chunker_settings_hash = Column(String(64))
    enrichment_mode = Column(String(20))
    enrichment_version = Column(String(50))
    index_kind = Column(String(50))
    docs_total = Column(Integer, nullable=False, default=0)
    docs_done = Column(Integer, nullable=False, default=0)
    docs_failed = Column(Integer, nullable=False, default=0)
    chunks = Column(Integer, nullable=False, default=0)
    stats = Column(JSONType)
    created_at = Column(DateTime(timezone=True), default=_now)
    activated_at = Column(DateTime(timezone=True))
    retired_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("status IN ('building', 'active', 'retired', 'failed')",
                        name="ck_knowledge_snapshots_status"),
        Index("ux_knowledge_snapshots_tenant_kb_active", "tenant_id", "kb_id", unique=True,
              sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),
        Index("idx_knowledge_snapshots_tenant_kb", "tenant_id", "kb_id", "created_at"),
    )


class KnowledgeQueryDB(Base):
    """One search and its trace (core.knowledge.trace.Trace). Kept 30 days unless retained by an eval set."""

    __tablename__ = "knowledge_queries"

    id = Column(String(255), primary_key=True)                # kq_<32 hex>
    tenant_id = _tenant_column()
    surface = Column(String(20), nullable=False)              # tool | step | api | mcp | eval | ui
    run_id = Column(String(255))
    session_id = Column(String(255))
    agent_id = Column(String(255))
    step_id = Column(String(255))
    api_key_id = Column(String(255))
    kb_ids = Column(JSONType)
    trace_version = Column(Integer, nullable=False)
    trace = Column(JSONType, nullable=False)
    query_embedding = Column(LargeBinary)                     # for exact replay; never serialized
    hit_count = Column(Integer)
    latency_ms = Column(Integer)
    est_usd = Column(Float)
    degraded = Column(Boolean, nullable=False, default=False)
    verdict = Column(Integer)                                 # a person's rating of the result, when given
    retained_by = Column(String(255))                         # an eval set holding it: exempt from cleanup
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        CheckConstraint("surface IN ('tool', 'step', 'api', 'mcp', 'eval', 'ui')",
                        name="ck_knowledge_queries_surface"),
        Index("idx_knowledge_queries_tenant_created", "tenant_id", "created_at"),
        Index("idx_knowledge_queries_tenant_run", "tenant_id", "run_id"),
        Index("idx_knowledge_queries_tenant_session", "tenant_id", "session_id"),
    )


class KnowledgeCostDB(Base):
    """One priced Knowledge call: parse, OCR, embed, rerank, enrich or rewrite.

    The knowledge base's ledger — budgets, cost previews checked against actuals,
    per-document and per-stage cost. A call no chat path already logged is also
    mirrored into `usage_logs` (`usage_logged_by = 'ledger'`), which is how
    retrieval reaches a run's cost and the Usage screens; see
    `core.knowledge.ledger`.
    """

    __tablename__ = "knowledge_costs"

    id = Column(_BigId, primary_key=True, autoincrement=True)
    tenant_id = _tenant_column()
    at = Column(DateTime(timezone=True), nullable=False, default=_now)
    kb_id = Column(String(255), nullable=False)
    doc_id = Column(String(255))
    query_id = Column(String(255))
    snapshot_id = Column(String(255))
    run_id = Column(String(255))
    session_id = Column(String(255))
    stage = Column(String(20), nullable=False)
    cost_source = Column(String(10), nullable=False)
    model = Column(String(255), nullable=False, default="")
    provider = Column(String(100))
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    units = Column(Float, nullable=False, default=0.0)
    unit = Column(String(20), nullable=False, default="tokens")
    est_usd = Column(Float, nullable=False, default=0.0)
    charged_microusd = Column(BigInteger)                     # what a wallet settled; NULL for BYOK and OSS
    priced = Column(Boolean, nullable=False, default=True)
    usage_logged_by = Column(String(10), nullable=False, default="ledger")  # ledger | chat
    latency_ms = Column(Integer)
    request_id = Column(String(255))
    details = Column(JSONType)

    __table_args__ = (
        CheckConstraint("stage IN ('parse', 'ocr', 'embed', 'rerank', 'enrich', 'rewrite')",
                        name="ck_knowledge_costs_stage"),
        CheckConstraint("cost_source IN ('platform', 'byok', 'local')", name="ck_knowledge_costs_cost_source"),
        CheckConstraint("unit IN ('tokens', 'pages', 'search_units', 'requests')", name="ck_knowledge_costs_unit"),
        CheckConstraint("usage_logged_by IN ('ledger', 'chat')", name="ck_knowledge_costs_usage_logged_by"),
        Index("idx_knowledge_costs_tenant_kb_at", "tenant_id", "kb_id", "at"),
        Index("idx_knowledge_costs_tenant_run", "tenant_id", "run_id"),
        Index("idx_knowledge_costs_tenant_doc", "tenant_id", "doc_id"),
        Index("idx_knowledge_costs_tenant_query", "tenant_id", "query_id"),
    )
