"""A reply that is all reasoning is not an answer, and its tags are never shown (F305, F303).

Measured on cloud (C1a, D146): a managed Nova step agent's second reply was
nothing but `<thinking>…</thinking>`. F32's tag split runs after every provider,
but F137's empty-reply checks run inside each provider, before it — so the
split left an empty answer that nothing raised. The loop took "no tool call"
as the final answer, and the after-loop fallback, which assumed an empty reply
could no longer reach it, called the turn "I ran out of turns (15)" after two
model calls. A reply of `[REASONING]` markers alone went the same way.

On a tool-calling turn the same model's `<thinking>` narration went, tags and
all, into the reasoning channel (the OpenAI-compatible extractor keeps the
narration beside a call as reasoning), so chat showed `<thinking> I need to
scrape the URL … </thinking>` (C1b, F303).

Now: an answer emptied by the reasoning strip is asked for once more, and a
second one fails the turn as `model_no_answer`; "ran out of turns" is said only
when the loop ran out; the tags never reach the reasoning or a thought.
"""
from types import SimpleNamespace

import pytest

import core.llm_providers as llm
from _fakes import seed as S
from _fakes.fake_http import FakeResponse, install_httpx

GATEWAY = {"model_gateway_base_url": "http://gateway.test/v1", "model_gateway_key": "k"}


@pytest.fixture
def no_usage(monkeypatch):
    from core import usage_tracker

    async def nothing(**_kwargs):
        return None

    monkeypatch.setattr(usage_tracker, "log_usage", nothing)


def _gateway_replies(monkeypatch, *messages, finish_reason="stop"):
    """The model gateway answering with each message in turn, as cloud's does:
    Nova's text blocks joined into `content`, its toolUse as `tool_calls`."""
    replies = list(messages)

    def handler(url, kwargs):
        message = replies.pop(0) if len(replies) > 1 else replies[0]
        return FakeResponse({
            "usage": {"prompt_tokens": 20, "completion_tokens": 65},
            "choices": [{"message": message, "finish_reason": finish_reason}],
        })

    install_httpx(monkeypatch, handler)


def _server():
    return SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


# ── the provider layer ───────────────────────────────────────────────────────

class TestTheProviderLayer:
    async def test_a_reply_that_is_only_thinking_tags_raises(self, monkeypatch, no_usage):
        _gateway_replies(monkeypatch, {"content": "<thinking> I need to rely on my knowledge. </thinking>"})
        with pytest.raises(llm.EmptyAnswer, match="only reasoning") as raised:
            await llm.generate_response_ex("q", "sys", "cloud", "synapse.nova-micro", GATEWAY)
        assert raised.value.code == llm.MODEL_NO_ANSWER
        assert raised.value.reasoning == "I need to rely on my knowledge."
        assert raised.value.usage[:2] == (20, 65), "the call was made and billed"

    async def test_thinking_beside_a_tool_call_reaches_the_reasoning_without_its_tags(
            self, monkeypatch, no_usage):
        _gateway_replies(monkeypatch, {
            "content": "<thinking> I need to scrape the URL to answer. </thinking>",
            "tool_calls": [{"function": {"name": "scrape", "arguments": '{"url": "https://x.test"}'}}],
        }, finish_reason="tool_calls")
        result = await llm.generate_response_ex(
            "q", "sys", "cloud", "synapse.nova-pro", GATEWAY,
            tools=[{"type": "function", "function": {"name": "scrape"}}])
        assert '"scrape"' in result.text
        assert "<thinking>" not in result.reasoning and "</thinking>" not in result.reasoning
        assert result.reasoning == "I need to scrape the URL to answer."

    @pytest.mark.parametrize("content,answer,thought", [
        # The opening tag was cut off (a continuation after the output cap).
        ("still weighing it</thinking>\nThe answer is 4.", "The answer is 4.", "still weighing it"),
        # Opened, never closed, and nothing else followed.
        ("The answer is 4.\n<think>should I add units", "The answer is 4.", "should I add units"),
        # A mismatched pair.
        ("<thinking>ok</think>The answer is 4.", "The answer is 4.", "ok"),
    ])
    async def test_a_stray_tag_never_reaches_the_answer(self, monkeypatch, no_usage,
                                                        content, answer, thought):
        _gateway_replies(monkeypatch, {"content": content})
        result = await llm.generate_response_ex("q", "sys", "cloud", "synapse.nova-pro", GATEWAY)
        assert result.text == answer
        assert result.reasoning == thought

    async def test_an_unclosed_tag_before_a_tool_call_keeps_the_call(self, monkeypatch, no_usage):
        _gateway_replies(monkeypatch, {"content": '<thinking>Look it up.\n{"tool": "lookup", "arguments": {}}'})
        result = await llm.generate_response_ex("q", "sys", "cloud", "synapse.nova-pro", GATEWAY)
        assert "<thinking>" not in result.text
        assert '{"tool": "lookup"' in result.text

    async def test_an_answer_with_no_tags_is_untouched(self, monkeypatch, no_usage):
        _gateway_replies(monkeypatch, {"content": "Use <b>bold</b> for emphasis."})
        result = await llm.generate_response_ex("q", "sys", "cloud", "synapse.nova-pro", GATEWAY)
        assert result.text == "Use <b>bold</b> for emphasis."


# ── the agent loop ───────────────────────────────────────────────────────────

class TestTheAgentLoop:
    async def _turn(self, session_id, agent=None, **kwargs):
        from core.react_engine import run_agent_step
        agent = agent or S.make_agent(tools=[], skip_default_tools=True)
        return agent, [e async for e in run_agent_step(
            message="What is 2 + 2?", agent_id=agent["id"], session_id=session_id,
            server_module=_server(), agent_override=agent, tools_override=[],
            **{"max_turns": 15, **kwargs})]

    async def test_a_marker_only_reply_is_asked_for_again(self, fake_llm):
        fake_llm.script(["[REASONING]Two and two.[/REASONING]", "4"])
        _agent, events = await self._turn("s_f305_again")
        assert [e["response"] for e in events if e.get("type") == "final"] == ["4"]
        assert fake_llm.call_count == 2
        assert [e["retry"] for e in events if e.get("type") == "reasoning_only"] == [1]
        # What the model thought is still shown.
        assert any(e.get("type") == "llm_reasoning" and "Two and two" in e["reasoning"] for e in events)

    async def test_two_in_a_row_fail_the_turn_as_no_answer(self, fake_llm):
        from core.chat_turns import FAILED_ANSWER
        from core.session import _get_conversation_history
        fake_llm.script(["[REASONING]hmm[/REASONING]", "[REASONING]still hmm[/REASONING]", "never asked"])
        agent = S.make_agent(tools=[], skip_default_tools=True)
        with pytest.raises(llm.LLMError) as raised:
            await self._turn("s_f305_fail", agent=agent)
        assert raised.value.code == llm.MODEL_NO_ANSWER
        assert "ran out of turns" not in str(raised.value)
        assert fake_llm.call_count == 2
        [turn] = await _get_conversation_history("s_f305_fail", agent["id"])
        assert turn["assistant"] == FAILED_ANSWER, "the conversation keeps the question (F133)"

    async def test_a_provider_reply_of_only_reasoning_is_asked_for_again(self, monkeypatch):
        import core.react_engine as re_mod
        replies = []

        async def model(*_a, **_kw):
            replies.append(1)
            if len(replies) == 1:
                raise llm.EmptyAnswer("Error: The model returned only reasoning, no answer.",
                                      reasoning="I should say four.")
            return llm.LLMResult("4")

        monkeypatch.setattr(re_mod, "llm_generate_response", model)
        _agent, events = await self._turn("s_f305_provider")
        assert [e["response"] for e in events if e.get("type") == "final"] == ["4"]
        assert len(replies) == 2

    async def test_a_truly_empty_reply_still_fails_at_once(self, monkeypatch):
        """No reasoning, no text: F137's failure, unchanged — nothing to nudge."""
        import core.react_engine as re_mod
        replies = []

        async def model(*_a, **_kw):
            replies.append(1)
            raise llm.EmptyAnswer("Error: Empty OpenAI response.")

        monkeypatch.setattr(re_mod, "llm_generate_response", model)
        with pytest.raises(llm.EmptyAnswer):
            await self._turn("s_f305_empty")
        assert len(replies) == 1

    async def test_the_last_turn_gets_no_retry_and_does_not_say_it_ran_out(self, fake_llm):
        fake_llm.script(["[REASONING]hmm[/REASONING]"])
        with pytest.raises(llm.LLMError) as raised:
            await self._turn("s_f305_last", max_turns=1)
        assert raised.value.code == llm.MODEL_NO_ANSWER
        assert fake_llm.call_count == 1
