"""
Settings fields either do something or are gone (F76).

* `max_total_cost_usd` ("Max Cost (USD)" on an orchestration), `n8n_table_id`,
  a repo's `embedding_model_provider` and `report_agent_enabled` were saved and
  read by nothing. The installers wrote the settings ones too.
* `prompt_cache_enabled` is read by the prompt cache, but the `Settings` model
  dropped it, so it could not be changed through the settings route.
* `ollama_base_url` was written by every installer and read by the Ollama call —
  but no caller passed settings, so only the `OLLAMA_BASE_URL` env var worked.
"""
from pathlib import Path

import pytest

import core.llm_providers as _llm_mod
from _fakes.fake_http import FakeResponse

# Captured at collection, before the autouse `fake_llm` fixture replaces it.
_REAL_GENERATE = _llm_mod.generate_response

ROOT = Path(__file__).resolve().parents[3]
DEAD = ("n8n_table_id", "report_agent_enabled")


class TestDeadFieldsAreGone:
    def test_not_settings(self):
        from core.config import default_settings
        from core.models import Settings
        for key in DEAD:
            assert key not in default_settings(), key
            assert key not in Settings.model_fields, key

    def test_not_on_a_repo_or_an_orchestration(self):
        from core.models import Repo
        from core.models_orchestration import Orchestration
        assert "embedding_model_provider" not in Repo.model_fields
        assert "max_total_cost_usd" not in Orchestration.model_fields

    @pytest.mark.parametrize("installer", ["setup.py", "synapse/setup_wizard.py", "bin/synapse.js"])
    def test_no_installer_writes_them(self, installer):
        source = (ROOT / installer).read_text()
        for key in DEAD:
            assert key not in source, (installer, key)

    def test_an_orchestration_that_still_sends_max_cost_is_accepted(self):
        """Cloud's synced form still sends it until batch 3."""
        from core.models_orchestration import Orchestration
        from _fakes import seed as S
        Orchestration.model_validate({**S.make_orchestration(), "max_total_cost_usd": 5.0})


class TestPromptCacheCanBeTurnedOff:
    async def test_it_round_trips(self, client):
        from core.config import load_settings
        resp = await client.post("/api/settings", json={"agent_name": "Synapse", "prompt_cache_enabled": False})

        assert resp.status_code == 200
        assert load_settings()["prompt_cache_enabled"] is False
        assert (await client.get("/api/settings")).json()["prompt_cache_enabled"] is False


class TestTheOllamaUrlSetting:
    async def test_it_round_trips(self, client):
        await client.post("/api/settings", json={"agent_name": "Synapse", "ollama_base_url": "http://gpu-box:11434"})

        assert (await client.get("/api/settings")).json()["ollama_base_url"] == "http://gpu-box:11434"

    async def test_chat_goes_to_it(self, monkeypatch):
        from _fakes.fake_http import install_httpx
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        urls = []

        def handler(url, kwargs):
            urls.append(url)
            return FakeResponse({"response": "hi", "prompt_eval_count": 1, "eval_count": 1})

        install_httpx(monkeypatch, handler)
        await _REAL_GENERATE("hi", "sys", "local", "ollama.llama3", {"ollama_base_url": "http://gpu-box:11434"})

        assert urls and all(u.startswith("http://gpu-box:11434/") for u in urls), urls

    async def test_embeddings_go_to_it(self, monkeypatch):
        from _fakes.fake_http import install_httpx
        from core.llm_providers import embed_batch
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        urls = []

        def handler(url, kwargs):
            urls.append(url)
            return FakeResponse({"embeddings": [[0.1]]})

        install_httpx(monkeypatch, handler)
        await embed_batch(["x"], "ollama.nomic-embed-text", {"ollama_base_url": "http://gpu-box:11434"})

        assert urls and urls[0].startswith("http://gpu-box:11434/"), urls
