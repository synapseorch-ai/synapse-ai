"""Unit tests for pure helpers in core.llm_providers (routing, conversion)."""
import pytest


class TestModeDetection:
    @pytest.mark.parametrize("model,mode", [
        ("gpt-4o", "cloud"),
        ("claude-opus-4-8", "cloud"),
        ("gemini-2.0-flash", "cloud"),
        ("grok-2", "cloud"),
        ("deepseek-chat", "cloud"),
        ("bedrock.anthropic.claude", "bedrock"),
        ("cli.claude.sonnet", "cli"),
        ("hf.Qwen/Qwen2.5", "hf"),
        ("ollama.llama3", "local"),
        ("mistral", "local"),
        ("", "local"),
    ])
    def test_detect_mode_from_model(self, model, mode):
        from core.llm_providers import detect_mode_from_model
        assert detect_mode_from_model(model) == mode


class TestProviderDetection:
    @pytest.mark.parametrize("model,provider", [
        ("gpt-4o", "openai"),
        ("claude-opus-4-8", "anthropic"),
        ("gemini-2.0-flash", "gemini"),
        ("grok-2", "grok"),
        ("deepseek-chat", "deepseek"),
        ("bedrock.x", "bedrock"),
        ("cli.claude.sonnet", "anthropic_cli"),
        ("cli.codex.gpt", "codex_cli"),
        ("oaic.some-model", "openai_compatible"),
        ("locv1.model", "local_compatible"),
        ("ollama.llama3", "ollama"),
        ("hf.model", "huggingface"),
        ("unknown", "ollama"),
    ])
    def test_detect_provider_from_model(self, model, provider):
        from core.llm_providers import detect_provider_from_model
        assert detect_provider_from_model(model) == provider


class TestToolConversion:
    def test_convert_ollama_tools_to_anthropic(self):
        from core.llm_providers import _convert_tools_for_anthropic
        ollama_tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }]
        out = _convert_tools_for_anthropic(ollama_tools)
        assert out[0]["name"] == "get_weather"
        assert out[0]["description"] == "Get weather"
        assert out[0]["input_schema"]["properties"]["city"]["type"] == "string"

    def test_convert_none_returns_none(self):
        from core.llm_providers import _convert_tools_for_anthropic
        assert _convert_tools_for_anthropic(None) is None


class TestImageContent:
    def test_openai_image_content_builds_blocks(self):
        from core.llm_providers import _build_openai_image_content
        out = _build_openai_image_content("hi", ["data:image/png;base64,AAA"])
        # Multimodal content is a list of blocks with a text block + image block.
        assert isinstance(out, list)
        assert any(b.get("type") == "text" for b in out)

    def test_openai_image_content_no_images_returns_text(self):
        from core.llm_providers import _build_openai_image_content
        out = _build_openai_image_content("just text", None)
        assert out == "just text"
