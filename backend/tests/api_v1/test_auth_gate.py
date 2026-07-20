"""Login-gate auth: /api/auth/status reports whether the request's session is valid.

The Next.js proxy forwards the `synapse_session` cookie as the `X-Synapse-Session`
header and gates protected routes on the `authenticated` field. The backend is the
sole owner of SYNAPSE_JWT_SECRET, so this endpoint is the authoritative check — it
works even when the proxy's local JWT fast-path can't (empty/mismatched secret in
the frontend process), which is exactly the redirect-loop bug this guards against.
"""

_SETTINGS = {
    "login_enabled": True,
    "login_username": "admin",
    "login_password_hash": "x",  # presence is all `login_configured` checks
}


def _patch_settings(monkeypatch, settings):
    import core.routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "load_settings", lambda: settings)


class TestAuthStatusGate:
    async def test_valid_token_is_authenticated(self, client, monkeypatch):
        # setenv BEFORE minting so signing + verification share the secret
        # (both read SYNAPSE_JWT_SECRET at call time).
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "test-secret-" + "a" * 40)
        _patch_settings(monkeypatch, _SETTINGS)
        from core.user_auth import create_session_token
        token = create_session_token("admin")

        resp = await client.get("/api/auth/status", headers={"X-Synapse-Session": token})
        assert resp.status_code == 200
        assert resp.json() == {
            "login_enabled": True,
            "login_configured": True,
            "authenticated": True,
        }

    async def test_missing_token_is_not_authenticated(self, client, monkeypatch):
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "test-secret-" + "a" * 40)
        _patch_settings(monkeypatch, _SETTINGS)
        resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["login_enabled"] is True
        assert body["login_configured"] is True
        assert body["authenticated"] is False

    async def test_garbage_token_is_not_authenticated(self, client, monkeypatch):
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "test-secret-" + "a" * 40)
        _patch_settings(monkeypatch, _SETTINGS)
        resp = await client.get(
            "/api/auth/status", headers={"X-Synapse-Session": "garbage.token.value"}
        )
        assert resp.json()["authenticated"] is False

    async def test_token_signed_with_other_secret_is_rejected(self, client, monkeypatch):
        # Mirrors the real bug: a token signed with a DIFFERENT secret must NOT
        # validate — the gate stays closed rather than looping.
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "backend-secret-" + "a" * 40)
        from core.user_auth import create_session_token
        token = create_session_token("admin")
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "different-secret-" + "b" * 40)
        _patch_settings(monkeypatch, _SETTINGS)

        resp = await client.get("/api/auth/status", headers={"X-Synapse-Session": token})
        assert resp.json()["authenticated"] is False

    async def test_login_disabled_reports_not_configured(self, client, monkeypatch):
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "test-secret-" + "a" * 40)
        _patch_settings(monkeypatch, {"login_enabled": False})
        resp = await client.get("/api/auth/status")
        body = resp.json()
        assert body["login_enabled"] is False
        assert body["login_configured"] is False
        assert body["authenticated"] is False
