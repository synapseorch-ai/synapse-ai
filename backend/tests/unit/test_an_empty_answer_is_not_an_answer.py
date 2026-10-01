"""
An empty or blocked provider answer is a failure, not the reply (F133).

`_extract_anthropic_response` and `_extract_gemini_response` returned their
failures as the answer's text — "Error: Empty Anthropic response.", "Error:
Response blocked by Gemini safety filters.", "Error: … returned only reasoning" —
so an agent said them and an LLM step stored them. They raise `EmptyAnswer`
now, an `LLMError` that carries what the call spent and why it stopped.

Read inside the retried `try`, a raise there would have sent a call that had
already been answered and billed up to four more times; the answer is read
once, outside it. And the final error message counts the attempts that were
made: measured on cloud, a 400 stopped at the first and still read "All 5
attempts failed".
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

import core.llm_providers as llm
from _fakes import seed as S
from test_human_in_containers import _run, driver, worker  # noqa: F401 — fixtures

_REAL_GENERATE = llm.generate_response


@pytest.fixture
def no_backoff(monkeypatch):
    real = asyncio.sleep

    async def instant(_delay, *args, **kwargs):
        await real(0)

    monkeypatch.setattr(llm.asyncio, "sleep", instant)


def _anthropic(blocks, *, stop_reason="end_turn", calls=None, fail=None):
    usage = SimpleNamespace(input_tokens=12, output_tokens=7,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    response = SimpleNamespace(content=blocks, usage=usage, stop_reason=stop_reason)

    class _Messages:
        async def create(self, **_kwargs):
            if calls is not None:
                calls.append(1)
            if fail is not None:
                raise fail
            return response

    class _Client:
        def __init__(self, *_a, **_kw):
            self.messages = _Messages()

    return _Client


def _thinking(text):
    return SimpleNamespace(type="thinking", thinking=text, text=None, name=None, input=None)


class TestAnthropic:
    async def test_an_empty_reply_raises_once_with_what_it_spent(self, monkeypatch, no_backoff):
        import anthropic
        calls = []
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _anthropic([], calls=calls))

        with pytest.raises(llm.EmptyAnswer, match="Empty Anthropic response") as raised:
            await llm.call_anthropic("claude-x", [{"role": "user", "content": "q"}], "sys", "key")
        assert calls == [1], "an answered call is not sent again"
        assert raised.value.usage == (12, 7, 0, 0)

    async def test_a_reply_that_is_only_reasoning_says_where_it_stopped(self, monkeypatch, no_backoff):
        import anthropic
        monkeypatch.setattr(anthropic, "AsyncAnthropic",
                            _anthropic([_thinking("still going")], stop_reason="max_tokens"))

        with pytest.raises(llm.EmptyAnswer, match="only reasoning") as raised:
            await llm.call_anthropic("claude-x", [{"role": "user", "content": "q"}], "sys", "key")
        assert raised.value.reasoning == "still going"
        assert raised.value.stop_reason == "max_tokens"

    async def test_a_refused_request_says_it_was_tried_once(self, monkeypatch, no_backoff):
        import anthropic
        calls = []
        refused = anthropic.BadRequestError(
            "Your credit balance is too low",
            response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
            body=None,
        )
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _anthropic([], calls=calls, fail=refused))

        with pytest.raises(llm.LLMError, match=r"failed after 1 attempt\. Last error: API error 400"):
            await llm.call_anthropic("claude-x", [{"role": "user", "content": "q"}], "sys", "key")
        assert calls == [1]


def _gemini(responses, calls):
    class _Models:
        def generate_content(self, model, contents, config):
            calls.append(getattr(config, "tools", None))
            return responses[min(len(calls), len(responses)) - 1]

    return SimpleNamespace(models=_Models())


def _gemini_response(parts, finish="STOP"):
    candidate = SimpleNamespace(finish_reason=SimpleNamespace(name=finish),
                                content=SimpleNamespace(parts=parts))
    usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=5,
                            cached_content_token_count=0)
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


_TOOLS = [{"type": "function", "function": {
    "name": "lookup", "description": "Look something up",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]


class TestGemini:
    async def test_a_blocked_reply_raises_once(self, monkeypatch, no_backoff):
        calls = []
        client = _gemini([_gemini_response([], "SAFETY")], calls)
        monkeypatch.setattr(llm, "_gemini_client_for", lambda *_a, **_k: client)

        with pytest.raises(llm.EmptyAnswer, match="safety filters") as raised:
            await llm.call_gemini("gemini-2.0", [{"role": "user", "content": "q"}], "sys", "key")
        assert len(calls) == 1, "an answered call is not sent again"
        assert raised.value.usage[:2] == (10, 5)

    async def test_a_malformed_call_is_still_asked_again_without_tools(self, monkeypatch, no_backoff):
        calls = []
        text = SimpleNamespace(function_call=None, text="plain answer", thought=None)
        client = _gemini(
            [_gemini_response([], "MALFORMED_FUNCTION_CALL"), _gemini_response([text])], calls)
        monkeypatch.setattr(llm, "_gemini_client_for", lambda *_a, **_k: client)

        result = await llm.call_gemini("gemini-2.0", [{"role": "user", "content": "q"}], "sys", "key",
                                       tools=_TOOLS)
        assert result.text == "plain answer"
        assert len(calls) == 2 and calls[0] and not calls[1], "the second ask went without tools"


class TestTheDispatch:
    async def test_an_empty_answer_is_still_counted(self, monkeypatch):
        import anthropic
        from core import usage_tracker
        logged = []

        async def log_usage(**kwargs):
            logged.append(kwargs)

        monkeypatch.setattr(usage_tracker, "log_usage", log_usage)
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _anthropic([]))

        with pytest.raises(llm.EmptyAnswer):
            await llm.generate_response_ex("q", "sys", "cloud", "claude-x", {"anthropic_key": "k"})
        assert [(u["input_tokens"], u["output_tokens"]) for u in logged] == [(12, 7)]


class TestAnAgent:
    async def test_a_reply_cut_off_while_thinking_is_continued(self, monkeypatch):
        """The one empty answer that is not a failure: the output cap came before
        the answer did. The loop asks for the rest, as for any truncated reply."""
        import core.react_engine as re_mod
        from core.react_engine import run_agent_step
        replies = []

        async def model(*_a, **_kw):
            replies.append(1)
            if len(replies) == 1:
                cut = llm.EmptyAnswer("Error: Anthropic returned only reasoning", reasoning="thinking…")
                cut.stop_reason, cut.usage = "max_tokens", (5, 50, 0, 0)
                raise cut
            return llm.LLMResult("the answer")

        monkeypatch.setattr(re_mod, "llm_generate_response", model)
        agent = S.make_agent(tools=[], skip_default_tools=True)
        events = [e async for e in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s_cut", server_module=SimpleNamespace(
                agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, tools_override=[], max_turns=4)]

        assert any(e.get("type") == "truncated_output" for e in events)
        assert [e["response"] for e in events if e.get("type") == "final"] == ["the answer"]


class TestAStep:
    async def test_an_llm_step_whose_reply_was_empty_fails(self, driver, monkeypatch):
        """It wrote "Error: Empty Anthropic response." into its output key and
        the run completed."""
        import anthropic
        monkeypatch.setattr(llm, "generate_response", _REAL_GENERATE)
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _anthropic([]))
        orch = S.make_orchestration(id="orch_empty", entry_step_id="write", steps=[
            {"id": "write", "name": "Write", "type": "llm", "model": "claude-x",
             "prompt_template": "write", "output_key": "draft", "next_step_id": "after"},
            {"id": "after", "name": "After", "type": "print", "print_content": "ran",
             "output_key": "o_after", "next_step_id": None},
        ])
        await S.seed_orchestrations([orch])

        assert await driver.start(orch, "run_empty") == "failed"
        state = (await _run("run_empty")).shared_state
        assert "Empty Anthropic response" not in str(state.get("draft"))
        assert "o_after" not in state


class TestTheOtherProvidersSayItToo:
    """The same rule for the three readers F133 did not reach (F137).

    Bedrock (Converse and InvokeModel), the OpenAI-compatible extractor — which
    is openai, grok, deepseek, `oaic.`, `locv1.` and the model gateway — and
    Ollama all returned `""`, and `refusal`/`content_filter`/`SAFETY` normalise
    to "stop", a finished answer. So the agent loop reported "I completed the
    requested actions." for a turn in which the model said nothing at all, and
    an LLM step wrote the empty string into its output key.
    """

    @staticmethod
    def _reply(message, finish_reason="stop"):
        from _fakes.fake_http import FakeResponse, install_httpx
        return FakeResponse({
            "usage": {"prompt_tokens": 9, "completion_tokens": 0},
            "choices": [{"message": message, "finish_reason": finish_reason}],
        }), install_httpx

    async def test_an_empty_openai_reply_raises(self, monkeypatch):
        reply, install = self._reply({"content": ""})
        install(monkeypatch, reply)
        with pytest.raises(llm.EmptyAnswer, match="Empty OpenAI response") as raised:
            await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key")
        # The call was made and billed, so what it spent travels with it.
        assert raised.value.usage == (9, 0, 0, 0)

    async def test_a_filtered_reply_says_it_was_withheld(self, monkeypatch):
        reply, install = self._reply({"content": ""}, finish_reason="content_filter")
        install(monkeypatch, reply)
        with pytest.raises(llm.EmptyAnswer, match="withheld this reply"):
            await llm.call_grok("grok-2", [{"role": "user", "content": "q"}], "sys", "key")

    async def test_a_refusal_carries_the_models_words(self, monkeypatch):
        reply, install = self._reply({"content": "", "refusal": "I can't help with that."})
        install(monkeypatch, reply)
        with pytest.raises(llm.EmptyAnswer, match="I can't help with that."):
            await llm.call_deepseek("deepseek-chat", [{"role": "user", "content": "q"}],
                                    "sys", "key")

    async def test_reasoning_with_no_answer_raises_and_keeps_the_thinking(self, monkeypatch):
        reply, install = self._reply({"content": "", "reasoning_content": "thinking hard"},
                                     finish_reason="length")
        install(monkeypatch, reply)
        with pytest.raises(llm.EmptyAnswer, match="only reasoning") as raised:
            await llm.call_v1_compatible("m", [{"role": "user", "content": "q"}], "sys",
                                         "http://host/v1", "key")
        # `max_tokens` + reasoning is the one the agent loop continues rather
        # than fails, so both have to survive the raise.
        assert raised.value.stop_reason == "max_tokens"
        assert raised.value.reasoning == "thinking hard"

    async def test_a_tool_call_with_no_prose_is_still_an_answer(self, monkeypatch):
        reply, install = self._reply({
            "content": "",
            "tool_calls": [{"function": {"name": "get_x", "arguments": "{}"}}],
        }, finish_reason="tool_calls")
        install(monkeypatch, reply)
        text, *_ = await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key",
                                         tools=[{"type": "function", "function": {"name": "get_x"}}])
        assert "get_x" in text

    async def test_an_empty_bedrock_reply_raises(self, monkeypatch):
        class _FakeBedrock:
            def converse(self, **kwargs):
                return {"output": {"message": {"content": []}},
                        "usage": {"inputTokens": 5, "outputTokens": 0},
                        "stopReason": "end_turn"}

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
        with pytest.raises(llm.EmptyAnswer, match="Empty Bedrock response") as raised:
            await llm.call_bedrock("bedrock.anthropic.claude", [{"role": "user", "content": "q"}],
                                   "sys", "us-east-1", {})
        assert raised.value.usage == (5, 0, 0, 0)

    async def test_a_guardrail_says_it_was_withheld(self, monkeypatch):
        class _FakeBedrock:
            def converse(self, **kwargs):
                return {"output": {"message": {"content": []}}, "usage": {},
                        "stopReason": "guardrail_intervened"}

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
        with pytest.raises(llm.EmptyAnswer, match="withheld this reply"):
            await llm.call_bedrock("bedrock.anthropic.claude", [{"role": "user", "content": "q"}],
                                   "sys", "us-east-1", {})


class TestTheTurnLimitSaysWhatHappened:
    """"I completed the requested actions." was the only thing a turn that ran
    out of turns ever said (F137). It is a claim the run cannot support, and
    the same false line F68 struck off the v1 approval path."""

    async def test_a_turn_that_ran_out_says_so(self, fake_llm):
        from _fakes.fake_llm import tool_call
        from core.react_engine import run_agent_step

        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return "again"

        fake_llm.script([tool_call("my_tool", x=1)] * 6)
        events = [ev async for ev in run_agent_step(
            message="go", agent_id=agent.get("id"), session_id="s_turnlimit",
            server_module=SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, max_turns=2, tool_executor=executor,
            tools_override=[{"type": "function", "function": {
                "name": "my_tool", "description": "t",
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}}],
        )]

        finals = [e for e in events if e.get("type") == "final"]
        assert finals, [e.get('type') for e in events]
        answer = finals[-1].get("response", "")
        assert "ran out of turns" in answer, answer
        assert "I completed the requested actions." not in answer
