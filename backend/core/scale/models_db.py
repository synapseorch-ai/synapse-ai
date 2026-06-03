"""
SQLAlchemy ORM models for the scale layer.
All tables use JSONB for complex nested data (Postgres required).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Definitions (synced from local JSON)
# ---------------------------------------------------------------------------

class OrchestrationDB(Base):
    __tablename__ = "orchestrations"

    id = Column(String(255), primary_key=True)
    name = Column(String(500), nullable=False)
    description = Column(Text, default="")
    definition = Column(JSONB, nullable=False)   # full Orchestration model_dump()
    tenant_id = Column(String(255), default="default", nullable=False)
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
    definition = Column(JSONB, nullable=False)   # full Agent dict
    tenant_id = Column(String(255), default="default", nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_agents_tenant", "tenant_id"),
    )


class ToolDB(Base):
    __tablename__ = "tools"

    id = Column(String(255), primary_key=True)
    name = Column(String(500), nullable=False)
    definition = Column(JSONB, nullable=False)   # full custom tool dict
    tenant_id = Column(String(255), default="default", nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        Index("idx_tools_tenant", "tenant_id"),
    )


class SettingDB(Base):
    """Key-value store for settings synced from settings.json.
    Workers load LLM keys from here instead of the local JSON file."""
    __tablename__ = "scale_settings"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)         # JSON-encoded
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


# ---------------------------------------------------------------------------
# Run state (replaces JSON checkpoint files for V2 runs)
# ---------------------------------------------------------------------------

class OrchestrationRunDB(Base):
    __tablename__ = "orchestration_runs"

    run_id = Column(String(255), primary_key=True)
    orchestration_id = Column(String(255), nullable=False, index=True)
    session_id = Column(String(255))
    tenant_id = Column(String(255), default="default", nullable=False)
    status = Column(String(50), nullable=False, default="running")
    shared_state = Column(JSONB, nullable=False, default=dict)
    step_history = Column(JSONB, nullable=False, default=list)
    current_step_id = Column(String(255))
    waiting_for_human = Column(Boolean, default=False)
    human_prompt = Column(Text)
    human_fields = Column(JSONB, default=list)
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
    )


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

class ChatSessionDB(Base):
    __tablename__ = "chat_sessions"

    session_id = Column(String(255), primary_key=True)
    agent_id = Column(String(255))
    tenant_id = Column(String(255), default="default", nullable=False)
    status = Column(String(50), default="idle")   # idle | running | completed | failed
    messages = Column(JSONB, nullable=False, default=list)
    last_message_at = Column(DateTime(timezone=True))
    worker_id = Column(String(255))
    job_id = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=_now)

    # Webhook for async delivery
    webhook_url = Column(Text)
    webhook_secret = Column(Text)

    __table_args__ = (
        Index("idx_chat_tenant_time", "tenant_id", "last_message_at"),
    )


# ---------------------------------------------------------------------------
# Workers registry
# ---------------------------------------------------------------------------

class WorkerDB(Base):
    __tablename__ = "workers"

    worker_id = Column(String(255), primary_key=True)
    hostname = Column(String(500))
    address = Column(String(500))    # http://host:port — health check endpoint
    status = Column(String(50), default="online")  # online | offline | draining
    capabilities = Column(JSONB, default=dict)
    active_jobs = Column(Integer, default=0)
    max_jobs = Column(Integer, default=10)
    last_heartbeat = Column(DateTime(timezone=True), default=_now)
    registered_at = Column(DateTime(timezone=True), default=_now)
    mcp_disabled = Column(JSONB, default=list)   # MCP server names unavailable


# ---------------------------------------------------------------------------
# Dead letter queue
# ---------------------------------------------------------------------------

class DeadLetterQueueDB(Base):
    __tablename__ = "dead_letter_queue"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(String(255))
    orchestration_id = Column(String(255))
    job_function = Column(String(255))
    job_payload = Column(JSONB, nullable=False)
    error_message = Column(Text)
    error_traceback = Column(Text)
    attempt_count = Column(Integer, default=0)
    first_failed_at = Column(DateTime(timezone=True), default=_now)
    last_failed_at = Column(DateTime(timezone=True), default=_now)
    resolved = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_dlq_resolved", "resolved", "last_failed_at"),
    )


# ---------------------------------------------------------------------------
# Tenants (enterprise multi-tenancy)
# ---------------------------------------------------------------------------

class TenantDB(Base):
    __tablename__ = "tenants"

    tenant_id = Column(String(255), primary_key=True)
    name = Column(String(500))
    max_concurrent_runs = Column(Integer, default=100)
    max_queued_runs = Column(Integer, default=1000)
    priority = Column(Integer, default=0)   # higher = dedicated workers
    created_at = Column(DateTime(timezone=True), default=_now)
