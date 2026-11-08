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


class TestTheS3SecretIsWriteOnly:
    """F95: the Scale screen read the secret access key back in plaintext.

    GET echoed `s3_secret_access_key`, and the screen posted the whole object
    back on every save — so masking it on the way out would have saved the mask
    over the real value on the way back in. An empty secret therefore means
    "unchanged"; a new one replaces it; and clearing the access key id clears
    it, since the client uses a secret only beside a key id (`SynapseS3`).
    """

    SECRET = "uismoke-s3-secret-not-real"

    async def _save(self, client, **fields):
        body = {"s3_bucket": "bucket", "s3_access_key_id": "AKIAUISMOKE", **fields}
        assert (await client.post("/api/scale/config", json=body)).status_code == 200

    def _stored(self):
        from core.config import load_settings
        return load_settings().get("s3_secret_access_key", "")

    async def test_it_is_never_echoed(self, client, clean_env):
        await self._save(client, s3_secret_access_key=self.SECRET)

        body = (await client.get("/api/scale/config")).json()

        assert self.SECRET not in str(body)
        assert body["s3_secret_access_key_set"] is True

    async def test_saving_without_it_keeps_it(self, client, clean_env):
        await self._save(client, s3_secret_access_key=self.SECRET)
        await self._save(client, s3_secret_access_key="")
        await self._save(client)

        assert self._stored() == self.SECRET

    async def test_a_new_one_replaces_it(self, client, clean_env):
        await self._save(client, s3_secret_access_key=self.SECRET)
        await self._save(client, s3_secret_access_key="a-different-secret")

        assert self._stored() == "a-different-secret"

    async def test_clearing_the_key_id_clears_it(self, client, clean_env):
        await self._save(client, s3_secret_access_key=self.SECRET)
        await self._save(client, s3_access_key_id="", s3_secret_access_key="")

        assert self._stored() == ""
        assert (await client.get("/api/scale/config")).json()["s3_secret_access_key_set"] is False

    async def test_the_connection_test_uses_the_stored_one(self, client, clean_env, monkeypatch):
        import core.s3_storage as s3
        seen: dict = {}

        class Recorder:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def test_connection(self):
                return {"ok": True}

        monkeypatch.setattr(s3, "SynapseS3", Recorder)
        await self._save(client, s3_secret_access_key=self.SECRET)

        resp = await client.post("/api/scale/test-s3", json={
            "s3_bucket": "bucket", "s3_access_key_id": "AKIAUISMOKE", "s3_secret_access_key": ""})

        assert resp.status_code == 200
        assert seen["secret_access_key"] == self.SECRET
