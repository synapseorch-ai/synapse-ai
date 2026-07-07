"""V1 external API auth + validation (all routes require a Bearer API key)."""
import pytest

from _fakes import engine_events as E


class TestV1Auth:
    async def test_missing_bearer_is_rejected(self, client, seed_agent):
        seed_agent()
        resp = await client.post("/api/v1/chat", json={"message": "hi"})
        assert resp.status_code in (401, 403)  # HTTPBearer rejects before handler

    async def test_invalid_key_is_401(self, client, seed_agent):
        seed_agent()
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers={"Authorization": "Bearer sk-syn-not-a-real-key"})
        assert resp.status_code == 401

    async def test_valid_key_passes_auth(self, client, api_key, seed_agent, monkeypatch):
        seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.final("ok")]))
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers=api_key["headers"])
        assert resp.status_code == 200

    async def test_valid_key_bad_body_is_422(self, client, api_key, seed_agent):
        seed_agent()
        resp = await client.post("/api/v1/chat", json={}, headers=api_key["headers"])
        assert resp.status_code == 422
