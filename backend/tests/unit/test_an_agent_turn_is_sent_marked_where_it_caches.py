"""An agent turn reaches the endpoints that cache only what is marked with its prompt marked (F1588, CTX2's C1/C2).

Our model gateway caches Claude only when the caller marks the prompt (D191; Bedrock's cache points), and so do
OpenRouter's Claude and its explicit-cache Qwen models (OpenRouter's prompt-caching page, read 2026-11-14) and a
LiteLLM-style proxy in front of Claude. The engine sent them all plain strings, so none of their agent turns was ever
cached: 0 cache reads and 0 writes on every Claude call through the gateway in prod's last 7 days (TRI3, 11-14).

An agent turn now reaches them as OpenAI content parts: the system prompt one part with `cache_control`, the
transcript one part per turn with the last one marked, and the per-turn tail after it unmarked. The text is the same
text, byte for byte. Every call that is not an agent turn, and every other provider, is sent exactly as before; a
person who turned prompt caching off is sent no marker on their own key.
"""
from __future__ import annotations

import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx
from core.react_engine import _transcript_blocks
from core.providers.registry import HOSTED

EPHEMERAL = {"type": "ephemeral"}
#: A system prompt long enough to be worth a marker (prompt_cache.MIN_CACHEABLE_CHARS is 4,000).
SYSTEM = "# Role\nYou plan workflows. Read the pack, then finish with one call.\n" * 80
TRANSCRIPT = (
    "## TASK\nMake a workflow that triages open pull requests each morning.\n"
    "\n[Turn 1 — Action]\n{\"tool\": \"list_custom_tools\", \"arguments\": {}}\n"
    "\n[Turn 1 — Observation] Tool 'list_custom_tools' returned:\n[]\n"
    "\n[Turn 2 — Action]\n{\"tool\": \"read_workspace\", \"arguments\": {\"topic\": \"agents\"}}\n"
    "\n[Turn 2 — Observation] Tool 'read_workspace' returned:\n{\"topic\": \"agents\", \"items\": []}\n"
)
TAIL = "### CURRENT DATE & TIME CONTEXT\n**Current Date:** November 14, 2026\n\n### TURN BUDGET\n58 turn(s) left"
BLOCKS = _transcript_blocks(TRANSCRIPT)
#: What the engine sends as the prompt of that turn (react_engine.py: transcript, "\n\n", tail).
PROMPT = TRANSCRIPT + "\n\n" + TAIL
TOOLS = [{"type": "function", "function": {"name": "submit_plan", "description": "Submit the plan.",
                                           "parameters": {"type": "object", "properties": {}}}}]
SETTINGS = {
    "model_gateway_base_url": "http://gateway.test", "model_gateway_key": "gw-test-token",
    "openai_compatible_base_url": "http://proxy.test", "openai_compatible_key": "proxy-test-token",
    "local_compatible_base_url": "http://local.test",
    **{f"{spec.id}_key": f"{spec.id}-test-token" for spec in HOSTED},
}


@pytest.fixture
def sent(monkeypatch):
    """Every chat body posted, in order; usage logging is a no-op."""
    from core import usage_tracker

    async def nothing(**_kwargs):
        return None

    monkeypatch.setattr(usage_tracker, "log_usage", nothing)
    bodies: list[dict] = []

    def handler(url, kwargs):
        bodies.append(kwargs.get("json") or {})
        return FakeResponse({"usage": {"prompt_tokens": 3, "completion_tokens": 2},
                             "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    install_httpx(monkeypatch, handler)
    return bodies


async def _turn(model, *, blocks=BLOCKS, tail=TAIL, prompt=PROMPT, system=SYSTEM, images=None, **settings):
    """One call as the agent loop makes it (react_engine.py:2861-2870), or, with `blocks=None`, as any other caller
    (an LLM step, an evaluator, compaction, Knowledge) does."""
    await llm.generate_response_ex(prompt, system, llm.detect_mode_from_model(model), model,
                                   {**SETTINGS, **settings}, tools=TOOLS, prompt_blocks=blocks,
                                   volatile_tail=tail if blocks else None, images=images)


def _texts(content) -> str:
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def _marked(body) -> bool:
    return any(isinstance(m.get("content"), list) and any("cache_control" in p for p in m["content"])
               for m in body["messages"])


class TestTheGateway:
    async def test_the_gateway_gets_the_system_prompt_and_each_turn_as_parts_the_last_marked(self, sent):
        await _turn("synapse.claude-sonnet-4-6")
        system, user = sent[-1]["messages"][0], sent[-1]["messages"][-1]
        assert system == {"role": "system",
                          "content": [{"type": "text", "text": SYSTEM.strip(), "cache_control": EPHEMERAL}]}
        assert user["role"] == "user"
        assert user["content"] == [
            *({"type": "text", "text": b} for b in BLOCKS[:-1]),
            {"type": "text", "text": BLOCKS[-1], "cache_control": EPHEMERAL},
            {"type": "text", "text": "\n\n" + TAIL},
        ], "a part per turn, the last one marked, then the tail unmarked"
        assert len(BLOCKS) == 3

    async def test_the_parts_are_the_text_it_sent_before_byte_for_byte(self, sent):
        """The model reads the same text: caching cannot change what it answers."""
        await _turn("synapse.claude-sonnet-4-6", blocks=None)
        before = sent[-1]
        await _turn("synapse.claude-sonnet-4-6")
        after = sent[-1]
        assert not _marked(before) and _marked(after)
        assert [m["role"] for m in after["messages"]] == [m["role"] for m in before["messages"]]
        for old, new in zip(before["messages"], after["messages"]):
            assert _texts(new["content"]) == old["content"]
        assert {k: v for k, v in after.items() if k != "messages"} == \
            {k: v for k, v in before.items() if k != "messages"}, "tools and every other field as before"
        assert "ttl" not in str(after), "the 5-minute default: a 1-hour write costs 2x"

    async def test_a_call_that_is_not_an_agent_turn_is_sent_as_a_string(self, sent):
        """D191: a one-off call (no transcript blocks) is never marked, so it pays 1x."""
        await _turn("synapse.claude-sonnet-4-6", blocks=None)
        assert all(isinstance(m["content"], str) for m in sent[-1]["messages"])

    async def test_a_prefix_under_4000_characters_is_not_marked(self, sent):
        await _turn("synapse.claude-sonnet-4-6", system="You plan.", blocks=["## TASK\nShort."],
                    prompt="## TASK\nShort.\n\n" + TAIL)
        assert not _marked(sent[-1])

    async def test_the_gateway_is_marked_whatever_the_workspace_setting(self, sent):
        """The gateway decides per model (it caches only what its rows price); a workspace cannot turn it off,
        as before (F349)."""
        await _turn("synapse.claude-sonnet-4-6", prompt_cache_enabled=False)
        assert _marked(sent[-1])

    async def test_an_agent_turns_images_follow_its_text(self, sent):
        image = "data:image/png;base64,iVBORw0KGgo="
        await _turn("synapse.claude-sonnet-4-6", images=[image])
        content = sent[-1]["messages"][-1]["content"]
        assert [p["type"] for p in content] == ["text"] * (len(BLOCKS) + 1) + ["image_url"]
        assert content[-1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert _texts(content) == PROMPT, "never the parts' repr"


class TestOpenRouter:
    @pytest.mark.parametrize("model", ["openrouter.anthropic/claude-sonnet-4.6", "openrouter.anthropic/claude-haiku-4.5",
                                       "openrouter.qwen/qwen3-max", "openrouter.qwen/qwen-plus",
                                       "openrouter.qwen/qwen3.6-plus", "openrouter.qwen/qwen3-coder-plus",
                                       "openrouter.qwen/qwen3-coder-flash"])
    async def test_openrouter_claude_and_its_listed_qwen_models_are_marked(self, sent, model):
        await _turn(model)
        assert _marked(sent[-1])
        assert _texts(sent[-1]["messages"][-1]["content"]) == PROMPT

    @pytest.mark.parametrize("model", ["openrouter.openai/gpt-oss-120b", "openrouter.qwen/qwen3.5-plus-02-15",
                                       "openrouter.google/gemini-2.5-flash", "openrouter.deepseek/deepseek-v3.2",
                                       "openrouter.moonshotai/kimi-k3"])
    async def test_openrouter_other_models_and_qwen_snapshots_are_not(self, sent, model):
        """They cache by themselves (or, a snapshot, not this way): sent as before."""
        await _turn(model)
        assert all(isinstance(m["content"], str) for m in sent[-1]["messages"])

    async def test_caching_turned_off_marks_nothing_on_a_persons_own_key(self, sent):
        await _turn("openrouter.anthropic/claude-sonnet-4.6", prompt_cache_enabled=False)
        assert not _marked(sent[-1])


class TestOpenAICompatible:
    async def test_an_openai_compatible_server_is_marked_only_when_its_setting_says(self, sent):
        await _turn("oaic.claude-sonnet-4-6")
        assert not _marked(sent[-1]), "off by default: a server may not take content parts"
        await _turn("oaic.claude-sonnet-4-6", openai_compatible_cache_markers=True)
        assert _marked(sent[-1])

    async def test_a_local_server_is_never_marked(self, sent):
        await _turn("locv1.qwen3", openai_compatible_cache_markers=True)
        assert not _marked(sent[-1])

    def test_the_setting_is_off_by_default_and_saved_like_any_other(self):
        from core.config import default_settings
        from core.models import Settings

        assert default_settings()["openai_compatible_cache_markers"] is False
        assert Settings(openai_compatible_cache_markers=True).openai_compatible_cache_markers is True


#: Every hosted OpenAI-style provider but OpenRouter (whose marked models are above), and the two v1 servers with
#: the opt-in off: each is sent its agent turn exactly as before.
UNMARKED = [f"{spec.id}.some-model" for spec in HOSTED if spec.id != "openrouter"] + ["oaic.some-model",
                                                                                        "locv1.some-model"]


@pytest.mark.parametrize("model", UNMARKED)
async def test_every_other_provider_sends_what_it_sent(sent, model):
    await _turn(model, blocks=None)
    before = sent[-1]
    await _turn(model)
    assert sent[-1] == before
