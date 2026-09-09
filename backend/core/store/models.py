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
  buys is not being given up.
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
    JSON, Boolean, Column, DateTime, Float, Index,
    Integer, PrimaryKeyConstraint, String, Text, Uuid,
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
    """Jobs that exhausted their retries.

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
