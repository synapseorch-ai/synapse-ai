"""
The Scale screen reports the values nothing but the environment sets (F58).

`POST /api/scale/config` saved sixteen keys, and five of them are read only
from the environment: `WORKER_CONCURRENCY` (ARQ's `max_jobs`), `PGBOUNCER_MODE`,
`MAX_GLOBAL_QUEUE_DEPTH`, `RATE_LIMIT_PER_TENANT_RPS` and `METRICS_TOKEN`
(`/api/v2/metrics`). Saving one changed nothing, and the screen then showed the
saved value as if it were in effect. `NUM_QUEUE_SHARDS` was read by nothing at
all — there has been one queue since the shared fleet.

Now GET reports what is in effect for those keys and names them in `env_only`
(additive: the screen keeps its shape until its own batch), POST no longer
saves them, and the metrics token is reported as set or not, never echoed.
"""
import pytest

ENV_ONLY = {
    "worker_concurrency": "WORKER_CONCURRENCY",
    "pgbouncer_mode": "PGBOUNCER_MODE",
    "max_global_queue_depth": "MAX_GLOBAL_QUEUE_DEPTH",
    "rate_limit_per_tenant_rps": "RATE_LIMIT_PER_TENANT_RPS",
    "metrics_token": "METRICS_TOKEN",
}


@pytest.fixture
def clean_env(monkeypatch):
    for var in ENV_ONLY.values():
        monkeypatch.delenv(var, raising=False)


class TestTheEnvironmentOnlyValues:
    async def test_get_names_them(self, client, clean_env):
        body = (await client.get("/api/scale/config")).json()

        assert body["env_only"] == ENV_ONLY

    async def test_get_reports_the_value_in_effect(self, client, clean_env, monkeypatch):
        monkeypatch.setenv("WORKER_CONCURRENCY", "7")
        monkeypatch.setenv("PGBOUNCER_MODE", "1")

        body = (await client.get("/api/scale/config")).json()

        assert body["worker_concurrency"] == 7
        assert body["pgbouncer_mode"] is True

    async def test_saving_them_changes_nothing(self, client, clean_env):
        from core.config import load_settings

        resp = await client.post("/api/scale/config", json={
            "worker_concurrency": 99, "pgbouncer_mode": True, "max_global_queue_depth": 5,
            "rate_limit_per_tenant_rps": 3, "metrics_token": "secret", "redis_url": "redis://r:6379/0",
        })

        assert resp.status_code == 200
        settings = load_settings()
        assert settings.get("worker_concurrency") != 99
        assert settings.get("metrics_token") != "secret"
        assert settings["redis_url"] == "redis://r:6379/0"
        body = (await client.get("/api/scale/config")).json()
        assert body["worker_concurrency"] == 10
        assert body["max_global_queue_depth"] == 1_000_000

    async def test_the_metrics_token_is_never_echoed(self, client, clean_env, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "hunter2")

        body = (await client.get("/api/scale/config")).json()

        assert "hunter2" not in str(body)
        assert body["metrics_token_set"] is True

    async def test_a_worker_advertises_the_concurrency_arq_runs_with(self, client, clean_env):
        """The workers table showed the saved value while ARQ ran the env's."""
        from core.routes.settings import save_settings
        from core.scale.config import get_scale_config
        await save_settings({"worker_concurrency": 42})

        assert get_scale_config().worker_concurrency == 10


class TestQueueShardsAreGone:
    async def test_not_reported(self, client):
        assert "num_queue_shards" not in (await client.get("/api/scale/config")).json()

    def test_not_a_setting(self):
        from core.config import default_settings
        from core.models import Settings
        assert "num_queue_shards" not in default_settings()
        assert "num_queue_shards" not in Settings.model_fields
