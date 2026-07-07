"""
V2 backpressure — the REAL rate-limit / queue-depth guards firing (previously
mocked in the contract tests). Uses fakeredis + a fake PG factory and a patched
scale config with tight limits.
"""
import pytest

from _fakes.fake_pg import fake_session_factory

# Capture the real config builder before any test patches the module attribute.
import core.scale.config as _scfg
_ORIG_GET_SCALE_CONFIG = _scfg.get_scale_config


def _cfg(**over):
    cfg = _ORIG_GET_SCALE_CONFIG()
    cfg.enable_tenant_isolation = False
    cfg.default_tenant_id = "default"
    cfg.rate_limit_per_tenant_rps = 1000
    cfg.max_global_queue_depth = 1_000_000
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def v2_real(scale_app, monkeypatch):
    """scale_app with real backpressure helpers + fake PG + no-op metrics."""
    scale_app.state.pg_session_factory = fake_session_factory()
    import core.scale.metrics as metrics
    monkeypatch.setattr(metrics, "record_run_enqueued", lambda **k: None, raising=False)
    return scale_app


class TestRateLimit:
    async def test_second_call_in_window_is_429(self, client, v2_real, api_key, monkeypatch):
        import core.scale.config as config
        monkeypatch.setattr(config, "get_scale_config", lambda: _cfg(rate_limit_per_tenant_rps=1))
        first = await client.post("/api/v2/orchestrations/o1/run",
                                  json={"message": "a"}, headers=api_key["headers"])
        second = await client.post("/api/v2/orchestrations/o1/run",
                                   json={"message": "b"}, headers=api_key["headers"])
        assert first.status_code == 202
        assert second.status_code == 429


class TestQueueDepth:
    async def test_full_queue_is_503(self, client, v2_real, api_key, monkeypatch):
        import core.scale.config as config
        monkeypatch.setattr(config, "get_scale_config", lambda: _cfg(max_global_queue_depth=0))
        resp = await client.post("/api/v2/orchestrations/o1/run",
                                 json={"message": "x"}, headers=api_key["headers"])
        assert resp.status_code == 503
        assert "capacity" in resp.json()["detail"].lower()
