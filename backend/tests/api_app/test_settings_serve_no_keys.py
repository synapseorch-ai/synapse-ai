"""
The settings API serves no key (F201).

`GET /api/settings` and the answer of `POST /api/settings` carried every
provider, AWS and n8n secret in full — and the POST's answer the bcrypt
`login_password_hash` too, since only the incoming data was stripped. Anything
that can read a session's responses (an extension, a proxy log, an XSS) read
every key. Cloud never serves them; F135 stopped MCP tokens being served.

Now each secret is served as `""` beside `<name>_set` and `<name>_hint` (its
last four characters, for a long one). The settings page posts its whole form
back, so a blank secret means "unchanged"; `clear_secrets` removes one.
"""
import re

import pytest

from core.config import SECRET_SETTINGS, load_settings

KEY = "AIza" + "S" * 31 + "wxyz"  # 39 characters, Gemini's shape


async def _post(client, payload):
    return await client.post("/api/settings", json=payload)


class TestNothingSecretIsServed:
    async def test_get_serves_a_flag_and_a_hint_not_the_key(self, client):
        await _post(client, {"gemini_key": KEY})

        body = (await client.get("/api/settings")).json()

        assert body["gemini_key"] == ""
        assert body["gemini_key_set"] is True
        assert body["gemini_key_hint"] == "wxyz"
        assert KEY not in str(body)
        assert body["openai_key_set"] is False and body["openai_key_hint"] == ""

    async def test_the_posts_answer_serves_none_either(self, client):
        from core.user_auth import hash_password
        from core.routes.settings import save_settings

        # A stored password hash, as a login set up earlier leaves.
        await save_settings({**load_settings(), "login_password_hash": hash_password("pw-123456")})

        resp = await _post(client, {"gemini_key": KEY})

        assert resp.status_code == 200
        assert KEY not in resp.text
        assert "login_password_hash" not in resp.json()
        assert resp.json()["gemini_key_set"] is True

    @pytest.mark.parametrize("name", SECRET_SETTINGS)
    async def test_every_secret_is_masked(self, client, name):
        from core.routes.settings import save_settings

        value = f"secret-value-for-{name}"
        await save_settings({**load_settings(), name: value})

        body = (await client.get("/api/settings")).json()

        assert value not in str(body)
        assert body[name] == "" and body[f"{name}_set"] is True

    def test_every_field_named_like_a_secret_is_on_the_list(self):
        """A secret added to the model later is masked by default, not by memory."""
        from core.models import Settings

        not_secrets = {"aws_access_key_id", "login_password_hash", "huggingface_max_new_tokens", "clear_secrets"}
        named_like_one = {
            f for f in Settings.model_fields
            if re.search(r"(_key|_token|secret|password)", f) and f not in not_secrets
        }
        assert named_like_one <= set(SECRET_SETTINGS), named_like_one - set(SECRET_SETTINGS)


class TestSavingAroundTheMask:
    async def test_a_blank_secret_keeps_the_stored_one(self, client):
        await _post(client, {"gemini_key": KEY})

        # What the settings page does: posts its whole form, the key field blank.
        resp = await _post(client, {"gemini_key": "", "openai_key": "", "vault_enabled": True})

        assert resp.status_code == 200
        assert load_settings()["gemini_key"] == KEY

    async def test_a_new_secret_replaces_it(self, client):
        await _post(client, {"gemini_key": KEY})
        await _post(client, {"gemini_key": "AIza" + "N" * 35})

        assert load_settings()["gemini_key"] == "AIza" + "N" * 35

    async def test_clear_secrets_removes_one(self, client):
        await _post(client, {"gemini_key": KEY, "openai_key": "sk-" + "o" * 40})

        resp = await _post(client, {"clear_secrets": ["gemini_key"]})

        assert resp.status_code == 200
        assert load_settings()["gemini_key"] == ""
        assert load_settings()["openai_key"] == "sk-" + "o" * 40
        assert resp.json()["gemini_key_set"] is False
        assert "clear_secrets" not in load_settings()

    async def test_clear_secrets_takes_only_secrets(self, client):
        resp = await _post(client, {"clear_secrets": ["model"]})

        assert resp.status_code == 422
        assert "model" in resp.text
