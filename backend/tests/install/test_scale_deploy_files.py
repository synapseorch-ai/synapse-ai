"""
The scale deploy files work as shipped (F59, F60).

What they did instead, all read off the files:

* compose's API had no `REDIS_URL`, and scale mode is decided by it at startup —
  so `--profile scale` started a worker the API never enqueued to;
* the worker's `SCALE_POSTGRES_URL` was blank unless set, and its startup built
  an engine from that and crashed;
* Redis ran `allkeys-lru`, which evicts queued jobs under memory pressure;
* the worker's `container_name` and fixed host port made `--scale worker=N`
  impossible;
* a `postgresql://` URL — what every provider hands out, and what the k8s
  secret held — reached the async engine unconverted;
* the KEDA ScaledObject named a list that does not exist (the queue is a sorted
  set, which a list scaler cannot read), the PgBouncer manifest carried a
  literal `${POSTGRES_HOST}`, and compose's enterprise profile ran Prometheus
  and Grafana against a `/metrics` the workers do not serve. The docs had cut
  all three.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def _env(service: dict) -> dict:
    return dict(item.split("=", 1) for item in service.get("environment", []))


class TestTheDatabaseUrl:
    @pytest.mark.parametrize("given", [
        "postgresql://u:p@db:5432/synapse",
        "postgres://u:p@db:5432/synapse",
    ])
    def test_a_plain_postgres_url_is_driven_async(self, monkeypatch, given):
        from core.store.engine import default_url
        monkeypatch.setenv("SYNAPSE_DB_URL", given)

        assert default_url() == "postgresql+asyncpg://u:p@db:5432/synapse"

    @pytest.mark.parametrize("given", [
        "postgresql+asyncpg://u:p@db:5432/synapse",
        "sqlite+aiosqlite:////tmp/x.db",
    ])
    def test_other_urls_are_untouched(self, monkeypatch, given):
        from core.store.engine import default_url
        monkeypatch.setenv("SYNAPSE_DB_URL", given)

        assert default_url() == given

    def test_the_scale_layer_uses_the_engines_postgres_when_not_told_otherwise(self, monkeypatch):
        from core.scale.config import get_scale_config
        monkeypatch.delenv("SCALE_POSTGRES_URL", raising=False)
        monkeypatch.setenv("SYNAPSE_DB_URL", "postgresql://u:p@db:5432/synapse")

        assert get_scale_config().postgres_url == "postgresql+asyncpg://u:p@db:5432/synapse"

    def test_not_a_sqlite_file(self, monkeypatch):
        from core.scale.config import get_scale_config
        monkeypatch.delenv("SCALE_POSTGRES_URL", raising=False)

        assert get_scale_config().postgres_url == ""


class TestComposeScaleProfile:
    def test_the_api_can_be_told_to_scale(self, compose):
        assert "REDIS_URL" in _env(compose["services"]["backend"])

    def test_redis_never_evicts_a_queued_job(self, compose):
        assert "--maxmemory-policy noeviction" in compose["services"]["redis"]["command"]

    def test_workers_can_be_scaled_out(self, compose):
        worker = compose["services"]["worker"]
        assert "container_name" not in worker
        assert "ports" not in worker

    def test_the_worker_is_not_handed_a_blank_database(self, compose):
        assert "SCALE_POSTGRES_URL" not in _env(compose["services"]["worker"])

    def test_no_services_the_docs_cut(self, compose):
        for name in ("pgbouncer", "jaeger", "prometheus", "grafana"):
            assert name not in compose["services"]


class TestKubernetesManifests:
    def test_the_manifests_the_docs_cut_are_gone(self):
        for path in ("infra/k8s/worker-scaledobject.yaml", "infra/k8s/pgbouncer-deployment.yaml",
                     "infra/prometheus.yml"):
            assert not (ROOT / path).exists(), path

    def test_the_worker_service_survives(self):
        docs = list(yaml.safe_load_all((ROOT / "infra/k8s/worker-deployment.yaml").read_text()))
        assert any(d and d.get("kind") == "Service" and d["metadata"]["name"] == "synapse-worker"
                   for d in docs)
