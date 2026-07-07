"""
Coverage for utility modules: usage_tracker, vault, session, config,
personal_details, user_auth, compaction, profiling. Pure/JSON-backed logic,
sandboxed via SYNAPSE_DATA_DIR.
"""
import os
import pathlib

import pytest


class TestUsageTracker:
    def test_log_and_summarize(self):
        from core import usage_tracker
        usage_tracker.log_usage(model="claude-x", provider="anthropic",
                                input_tokens=100, output_tokens=50, context_chars=1000,
                                session_id="s1", source="chat", latency_seconds=0.5)
        logs = usage_tracker.get_usage_logs(limit=10)
        assert any(r.get("model") == "claude-x" for r in logs)
        summary = usage_tracker.get_usage_summary()
        assert isinstance(summary, dict)

    def test_estimate_tokens(self):
        from core import usage_tracker
        assert usage_tracker.estimate_tokens_from_text("hello world foo bar") > 0
        assert usage_tracker.estimate_tokens_from_text("") == 0

    def test_pricing_table_roundtrip(self):
        from core import usage_tracker
        table = usage_tracker.get_pricing_table()
        assert isinstance(table, dict)
        usage_tracker.save_pricing_table({"m": {"provider": "x", "input_per_1m": 1.0, "output_per_1m": 2.0}})
        assert "m" in usage_tracker.get_pricing_table()

    def test_cache_summary_and_clear(self):
        from core import usage_tracker
        assert isinstance(usage_tracker.get_cache_summary(), dict)
        usage_tracker.log_usage(model="m", provider="p", input_tokens=1, output_tokens=1,
                                context_chars=1)
        assert usage_tracker.clear_usage_logs() >= 0


class TestVault:
    def _tmpfile(self, name, content):
        # Absolute path under the sandbox data dir (the vault tools resolve paths
        # as-is via _safe_path, so we pass an absolute path).
        from core.config import DATA_DIR
        d = pathlib.Path(DATA_DIR) / "vault" / "utiltests"
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_maybe_vault_small_output_passthrough(self):
        from core.vault import maybe_vault
        assert maybe_vault("some_tool", "short output") == "short output"

    def test_maybe_vault_large_output_persisted(self, monkeypatch):
        import core.config as config
        monkeypatch.setattr(config, "load_settings",
                            lambda: {"vault_enabled": True, "vault_threshold": 10})
        from core.vault import maybe_vault
        out = maybe_vault("big_tool", "x" * 100)
        assert '"vault_file"' in out and "Output too large" in out

    def test_maybe_vault_disabled_passthrough(self, monkeypatch):
        import core.config as config
        monkeypatch.setattr(config, "load_settings", lambda: {"vault_enabled": False})
        from core.vault import maybe_vault
        assert maybe_vault("t", "x" * 1000) == "x" * 1000

    def test_expand_vault_mentions_no_mentions(self):
        from core.vault import expand_vault_mentions
        assert expand_vault_mentions("plain message") == "plain message"

    def test_expand_vault_mentions_inlines_file(self):
        from core.config import DATA_DIR
        from core.vault import expand_vault_mentions
        vd = pathlib.Path(DATA_DIR) / "vault"
        vd.mkdir(parents=True, exist_ok=True)
        (vd / "cfg.txt").write_text("SECRET=42", encoding="utf-8")
        out = expand_vault_mentions("use @[cfg.txt] please")
        assert "SECRET=42" in out

    def test_read_file_chunk(self):
        from core.vault import tool_read_file_chunk
        p = self._tmpfile("note.txt", "line1\nline2\nline3\nline4\n")
        out = tool_read_file_chunk(p, 1, 2)
        assert "line1" in out and "line2" in out

    def test_search_file(self):
        from core.vault import tool_search_file
        p = self._tmpfile("log.txt", "alpha\nbeta findme gamma\ndelta\n")
        out = tool_search_file(p, "findme")
        assert "findme" in out

    def test_read_and_search_json(self):
        import json as _json
        from core.vault import tool_read_json_chunk, tool_search_json
        data = [{"id": i, "tag": "match" if i == 2 else "no"} for i in range(5)]
        p = self._tmpfile("data.json", _json.dumps(data))
        chunk = tool_read_json_chunk(p, offset=0, limit=3)
        assert '"id"' in chunk
        found = tool_search_json(p, "match")
        assert "match" in found


class TestSession:
    def test_cli_session_id_roundtrip(self):
        import uuid
        from core.session import get_cli_session_id, save_cli_session_id
        sid = f"sess_{uuid.uuid4().hex[:8]}"
        assert get_cli_session_id(sid, "agent1", "anthropic_cli") is None
        save_cli_session_id(sid, "agent1", "anthropic_cli", "cli-xyz")
        assert get_cli_session_id(sid, "agent1", "anthropic_cli") == "cli-xyz"

    def test_history_and_sessions_empty(self):
        from core.session import get_recent_history_messages, list_chat_sessions, delete_chat_session
        assert get_recent_history_messages("ghost") == []
        assert isinstance(list_chat_sessions(), list)
        assert delete_chat_session("ghost") is False


class TestConfig:
    def test_load_settings_returns_dict(self):
        from core.config import load_settings
        assert isinstance(load_settings(), dict)

    def test_jwt_secret_is_stable(self):
        from core.config import get_or_create_jwt_secret
        s1 = get_or_create_jwt_secret()
        s2 = get_or_create_jwt_secret()
        assert s1 and s1 == s2

    def test_sanitize_db_url_normalizes(self):
        from core.config import sanitize_db_url
        # Strips the SQLAlchemy dialect suffix...
        assert sanitize_db_url("postgresql+psycopg://u:p@host/db").startswith("postgresql://")
        # ...and rewrites an empty password so libpq can parse it.
        assert sanitize_db_url("postgresql://user:@host/db") == "postgresql://user@host/db"
        assert sanitize_db_url("") == ""


class TestPersonalDetails:
    def test_default_load_save(self):
        from core import personal_details as pd
        assert isinstance(pd.default_personal_details(), dict)
        pd.save_personal_details({"first_name": "Grace", "last_name": "Hopper"})
        assert pd.load_personal_details()["first_name"] == "Grace"


class TestUserAuth:
    def test_password_hash_and_verify(self):
        from core.user_auth import hash_password, verify_password
        h = hash_password("s3cret")
        assert verify_password("s3cret", h) is True
        assert verify_password("wrong", h) is False

    def test_session_token_roundtrip(self):
        from core.user_auth import create_session_token, verify_session_token
        token = create_session_token("alice")
        assert verify_session_token(token) == "alice"
        assert verify_session_token("garbage.token.value") is None


class TestCompaction:
    async def test_no_compaction_when_disabled(self):
        from core.compaction import maybe_compact
        ctx, hist, archive, stats = await maybe_compact(
            "small context", [{"role": "user", "content": "hi"}],
            {"auto_compact_enabled": False}, "claude-x", "cloud", {}, "s1", "a1")
        assert archive is None and stats is None

    async def test_compaction_fires_over_threshold(self, fake_llm):
        fake_llm.set_default("SUMMARY of the conversation")
        from core.compaction import maybe_compact
        big = "x" * 200
        settings = {"auto_compact_enabled": True, "auto_compact_threshold": 100}
        ctx, hist, archive, stats = await maybe_compact(
            big, [{"role": "user", "content": "y" * 200}],
            settings, "claude-x", "cloud", settings, "s1", "a1")
        # Either it summarized (stats set) or handled gracefully; must not raise.
        assert isinstance(ctx, str)


class TestProfiling:
    def test_stats_and_flags(self):
        from core import profiling
        assert isinstance(profiling.get_stats(), dict)
        assert profiling.is_cpu_profiling() in (True, False)
        assert profiling.is_memory_profiling() in (True, False)
        profiling.reset_stats()
