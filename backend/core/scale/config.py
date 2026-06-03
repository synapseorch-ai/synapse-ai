"""
Scale mode configuration — reads from settings and environment variables.
Returns ScaleConfig(scale_mode=False) when Redis URL is not set (standalone mode).
"""
from dataclasses import dataclass, field
import os


@dataclass
class ScaleConfig:
    # Core connection URLs
    redis_url: str = ""
    postgres_url: str = ""           # asyncpg URL: postgresql+asyncpg://...

    # Feature flags
    scale_mode: bool = False         # True only when redis_url is set
    pgbouncer_mode: bool = False     # Use NullPool (required for PgBouncer transaction mode)
    redis_cluster_mode: bool = False # Use RedisCluster client

    # Queue settings
    worker_concurrency: int = 10
    num_queue_shards: int = 1        # Number of ARQ queue shards for Redis Cluster
    default_tenant_id: str = "default"
    enable_tenant_isolation: bool = False

    # Retention
    runs_retention_days: int = 90
    pubsub_event_ttl: int = 3600     # seconds to keep Redis Stream entries

    # Enterprise backpressure
    max_global_queue_depth: int = 1_000_000
    rate_limit_per_tenant_rps: int = 1000

    # Observability
    otlp_endpoint: str = ""          # e.g. http://jaeger:4317
    metrics_token: str = ""          # bearer token for /metrics endpoint

    # Kubernetes
    k8s_mode: bool = False


def get_scale_config() -> ScaleConfig:
    """Build ScaleConfig from settings + environment variable overrides."""
    try:
        from core.config import load_settings
        settings = load_settings()
    except Exception:
        settings = {}

    redis_url = (
        os.getenv("REDIS_URL")
        or settings.get("redis_url", "")
    )
    postgres_url = (
        os.getenv("SCALE_POSTGRES_URL")
        or settings.get("scale_postgres_url", "")
    )

    # Normalise the Postgres URL to asyncpg dialect
    if postgres_url and "postgresql+asyncpg" not in postgres_url:
        postgres_url = postgres_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        postgres_url = postgres_url.replace("postgres://", "postgresql+asyncpg://", 1)

    # Redis Cluster is auto-detected from URL scheme
    redis_cluster_mode = redis_url.startswith("redis+cluster://")

    scale_mode = bool(redis_url)

    return ScaleConfig(
        redis_url=redis_url,
        postgres_url=postgres_url,
        scale_mode=scale_mode,
        pgbouncer_mode=bool(int(os.getenv("PGBOUNCER_MODE", "0"))),
        redis_cluster_mode=redis_cluster_mode,
        worker_concurrency=int(os.getenv("WORKER_CONCURRENCY", str(settings.get("worker_concurrency", 10)))),
        num_queue_shards=int(os.getenv("NUM_QUEUE_SHARDS", "1")),
        default_tenant_id=os.getenv("DEFAULT_TENANT_ID", "default"),
        enable_tenant_isolation=bool(int(os.getenv("ENABLE_TENANT_ISOLATION", "0"))),
        runs_retention_days=int(os.getenv("RUNS_RETENTION_DAYS", "90")),
        pubsub_event_ttl=int(os.getenv("PUBSUB_EVENT_TTL", "3600")),
        max_global_queue_depth=int(os.getenv("MAX_GLOBAL_QUEUE_DEPTH", "1000000")),
        rate_limit_per_tenant_rps=int(os.getenv("RATE_LIMIT_PER_TENANT_RPS", "1000")),
        otlp_endpoint=os.getenv("OTLP_ENDPOINT", settings.get("otlp_endpoint", "")),
        metrics_token=os.getenv("METRICS_TOKEN", settings.get("metrics_token", "")),
        k8s_mode=bool(int(os.getenv("K8S_MODE", "0"))),
    )
