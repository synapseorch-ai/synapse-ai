"""
The provider table holds every provider decision, and holds it the way the
code it replaced did (PF1, D156).

Where each model id goes is pinned by `test_every_model_id_routes_as_before`.
This file pins the rest of what the table declares: that its ids are the ones
usage logs already carry, that it names real adapters and only secret settings
as keys, that its readiness and capabilities say what `model_readiness`,
`images` and the agent loop said before it, and that reading it loads no
provider SDK.
"""
import ast
import inspect
from pathlib import Path

import pytest

from _fakes import provider_routing as pr
from core.providers import registry
from core.providers.registry import PROVIDERS, provider_for

#: Written into every usage log since before the table: never renamed.
IDS = [
    "anthropic_cli", "gemini_cli", "codex_cli", "github_copilot_cli", "cli", "openai", "anthropic", "gemini",
    "bedrock", "grok", "deepseek", "model_gateway", "openai_compatible", "local_compatible", "ollama",
    "huggingface",
]


def test_the_ids_are_the_ones_usage_logs_carry():
    assert [p.id for p in PROVIDERS] == IDS
    assert registry.FALLBACK.id == "ollama"
    assert provider_for("").id == "ollama" and provider_for(None).id == "ollama"


@pytest.mark.parametrize("model,provider", [
    ("cli.claude.sonnet", "anthropic_cli"), ("cli.claudex", "anthropic_cli"), ("cli.x", "cli"),
    ("orca-mini", "ollama"), ("o1", "openai"), ("gpt-oss", "openai"), ("ollama.gpt-oss", "ollama"),
    ("synapse.gpt-oss", "model_gateway"), ("oaic.o3", "openai_compatible"), ("locv1.claude", "local_compatible"),
    ("bedrock.anthropic.claude-3", "bedrock"), ("hf.gpt2", "huggingface"), ("gemma3", "gemini"),
    ("deepseek-r1:7b", "deepseek"), ("mistral", "ollama"),
])
def test_an_id_goes_to_the_first_entry_it_matches(model, provider):
    assert provider_for(model).id == provider


def test_every_key_it_names_is_a_secret():
    from core.config import SECRET_SETTINGS

    named = {key for p in PROVIDERS for key in p.keys}
    assert named <= set(SECRET_SETTINGS), named - set(SECRET_SETTINGS)


def test_every_adapter_it_names_exists():
    for p in PROVIDERS:
        for name in filter(None, (p.chat, p.embed)):
            assert inspect.iscoroutinefunction(registry.resolve(name)), (p.id, name)


def test_the_prefixes_are_lower_case():
    for p in PROVIDERS:
        assert all(prefix == prefix.lower() for prefix in p.prefixes), p.id
        assert p.strip is None or p.strip == p.strip.lower(), p.id


def test_reading_the_table_loads_no_provider_sdk():
    """`images` and `model_readiness` read it; neither should pull in boto3 or genai."""
    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"__future__", "importlib", "re", "dataclasses", "typing"}, imported


def test_readiness_asks_for_what_it_asked_for_before():
    from core import model_readiness

    assert model_readiness._KEYED == {
        "openai": ("openai_key", "OpenAI"),
        "anthropic": ("anthropic_key", "Anthropic"),
        "gemini": ("gemini_key", "Gemini"),
        "grok": ("grok_key", "Grok"),
        "deepseek": ("deepseek_key", "DeepSeek"),
    }
    assert model_readiness._ADDRESSED == {
        "openai_compatible": ("openai_compatible_base_url", "OpenAI-compatible"),
        "local_compatible": ("local_compatible_base_url", "local OpenAI-compatible"),
        "model_gateway": ("model_gateway_base_url", "model gateway"),
    }


@pytest.mark.parametrize("model", pr.CORPUS)
def test_the_blind_providers_are_the_ones_they_were(model):
    """The rule the table replaced: DeepSeek, HuggingFace and the CLIs drop images."""
    from core.images import model_can_see_images

    name = model.strip().lower()
    assert model_can_see_images(model) == (bool(name) and not name.startswith(("deepseek", "hf.", "cli.")))


def test_tools_go_in_the_prompt_for_the_modes_the_agent_loop_prompts():
    """react_engine describes tools in the system prompt for cli, bedrock and hf."""
    for p in PROVIDERS:
        assert (p.caps.tools == "prompt") == (p.mode in ("cli", "bedrock", "hf")), p.id


def test_every_model_with_a_reasoning_channel_belongs_to_a_provider_that_has_one():
    from core import reasoning
    from core.llm_providers import CLI_EFFORT_BASES

    for prefix in (*reasoning._NATIVE, *reasoning._READ_ONLY_NATIVE, *CLI_EFFORT_BASES):
        assert provider_for(prefix).caps.reasoning, prefix


def test_nothing_reranks_yet():
    assert not any(p.caps.rerank for p in PROVIDERS)


async def test_every_provider_the_model_list_names_is_in_the_table(client, monkeypatch):
    import core.routes.data as data
    from _fakes.fake_http import FakeResponse, install_httpx

    monkeypatch.setattr(data, "load_settings", lambda: {})
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse({}, status_code=404))

    providers = (await client.get("/api/models")).json()["providers"]
    assert providers and set(providers) <= set(IDS), set(providers) - set(IDS)
