"""
Every native tool call a provider returns reaches the loop.

The extractors used to keep the first call and drop the rest — silently on
Anthropic and OpenAI, with a warning on Gemini. A model that answered with two
parallel calls ran one. These drive the extractors directly with the response
shapes the SDKs produce; no network.

Each extractor returns `(text, reasoning)`. It used to return one string with
the reasoning spliced into it behind `[REASONING]` markers, which is why these
assertions used to go through `extract_reasoning`; the markers are now the
fallback for models with no reasoning channel, not the carrier.
"""
import json
import types

from core.react_engine import parse_all_tool_calls


def _calls(text: str) -> list[tuple[str, dict]]:
    return [(c["tool"], c.get("arguments", {})) for c in parse_all_tool_calls(text)]


class TestAnthropic:
    @staticmethod
    def _response(*blocks):
        return types.SimpleNamespace(content=list(blocks))

    @staticmethod
    def _text(t):
        return types.SimpleNamespace(type="text", text=t)

    @staticmethod
    def _tool(name, **inp):
        return types.SimpleNamespace(type="tool_use", name=name, input=inp, id=f"toolu_{name}")

    def test_two_parallel_calls_both_survive_in_order(self):
        from core.llm_providers import _extract_anthropic_response
        out, reasoning = _extract_anthropic_response(self._response(
            self._text("I'll fetch both."), self._tool("read_file", path="a"), self._tool("grep", pattern="x"),
        ))
        assert _calls(out) == [("read_file", {"path": "a"}), ("grep", {"pattern": "x"})]
        assert reasoning == "I'll fetch both."

    def test_a_single_call_is_unchanged(self):
        from core.llm_providers import _extract_anthropic_response
        out, reasoning = _extract_anthropic_response(self._response(self._tool("read_file", path="a")))
        assert out == json.dumps({"tool": "read_file", "arguments": {"path": "a"}})
        assert reasoning == ""

    def test_text_only_is_text(self):
        from core.llm_providers import _extract_anthropic_response
        out, reasoning = _extract_anthropic_response(self._response(self._text("Done."), self._text("Really.")))
        assert out == "Done.\nReally."
        assert reasoning == ""
        assert _calls(out) == []


class TestOpenAICompatible:
    @staticmethod
    def _msg(content=None, *calls):
        return {
            "content": content,
            "tool_calls": [{"id": f"call_{i}", "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a)}}
                           for i, (n, a) in enumerate(calls)],
        }

    def test_two_parallel_calls_both_survive_in_order(self):
        from core.llm_providers import _openai_compat_extract
        out, reasoning = _openai_compat_extract(self._msg("checking", ("search", {"q": "1"}), ("search", {"q": "2"})))
        assert _calls(out) == [("search", {"q": "1"}), ("search", {"q": "2"})]
        assert reasoning == "checking"

    def test_a_single_call_is_unchanged(self):
        from core.llm_providers import _openai_compat_extract
        out, _ = _openai_compat_extract(self._msg(None, ("search", {"q": "1"})))
        assert out == json.dumps({"tool": "search", "arguments": {"q": "1"}})

    def test_empty_arguments_string_is_an_empty_object(self):
        from core.llm_providers import _openai_compat_extract
        msg = {"content": None, "tool_calls": [{"function": {"name": "ping", "arguments": ""}}]}
        assert _calls(_openai_compat_extract(msg)[0]) == [("ping", {})]

    def test_reasoning_content_is_read(self):
        """The field every OpenAI-compatible reasoning model answers in.

        DeepSeek-R1, vLLM and gpt-oss through our own gateway all return their
        thinking here, unprompted. It was read nowhere, so the panel sat empty
        on exactly the models that had the most to show it.
        """
        from core.llm_providers import _openai_compat_extract
        text, reasoning = _openai_compat_extract(
            {"content": "42", "reasoning_content": "counting carefully"})
        assert text == "42"
        assert reasoning == "counting carefully"

    def test_the_other_spelling_is_read_too(self):
        from core.llm_providers import _openai_compat_extract
        _, reasoning = _openai_compat_extract({"content": "x", "reasoning": "some endpoints say this"})
        assert reasoning == "some endpoints say this"


class TestGemini:
    @staticmethod
    def _response(*parts):
        return types.SimpleNamespace(candidates=[types.SimpleNamespace(
            finish_reason=types.SimpleNamespace(name="STOP"),
            content=types.SimpleNamespace(parts=list(parts)),
        )])

    @staticmethod
    def _text(t, thought=None):
        return types.SimpleNamespace(text=t, function_call=None, thought=thought)

    @staticmethod
    def _fn(name, **args):
        return types.SimpleNamespace(text=None, thought=None,
                                     function_call=types.SimpleNamespace(name=name, args=args))

    def test_two_function_calls_both_survive_in_order(self):
        from core.llm_providers import _extract_gemini_response
        out, reasoning = _extract_gemini_response(
            self._response(self._text("two lookups"), self._fn("a", x=1), self._fn("b", y=2)))
        assert _calls(out) == [("a", {"x": 1}), ("b", {"y": 2})]
        assert reasoning == "two lookups"

    def test_a_single_call_is_unchanged(self):
        from core.llm_providers import _extract_gemini_response
        out, _ = _extract_gemini_response(self._response(self._fn("a", x=1)))
        assert out == json.dumps({"tool": "a", "arguments": {"x": 1}})

    def test_thought_parts_are_reasoning_and_the_rest_is_the_answer(self):
        """The split that used to be a guess.

        Text beside a call was assumed to be reasoning; text on its own was
        assumed to be the answer. Here the model sends both at once, which the
        positional rule cannot get right — and which `thought=True` settles.
        """
        from core.llm_providers import _extract_gemini_response
        out, reasoning = _extract_gemini_response(self._response(
            self._text("weighing the options", thought=True),
            self._text("The answer is 42."),
        ))
        assert out == "The answer is 42."
        assert reasoning == "weighing the options"

    def test_thoughts_beside_a_call_do_not_swallow_the_call(self):
        from core.llm_providers import _extract_gemini_response
        out, reasoning = _extract_gemini_response(self._response(
            self._text("I need the file first", thought=True),
            self._fn("read_file", path="a"),
        ))
        assert _calls(out) == [("read_file", {"path": "a"})]
        assert reasoning == "I need the file first"


class TestTheLoopExecutesEveryParsedCall:
    async def test_two_calls_in_one_turn_both_run_before_the_next_llm_turn(self, fake_llm):
        from _fakes import seed as S
        from _fakes.fake_llm import tool_call
        from core.react_engine import run_agent_step

        schema = lambda n: {"type": "function", "function": {"name": n, "description": "t",
                                                              "parameters": {"type": "object", "properties": {}}}}
        executed: list[tuple[str, dict]] = []

        async def executor(name, args):
            executed.append((name, args))
            return f"out-{name}"

        # What the fixed extractors now hand the loop: two JSON objects in one response.
        fake_llm.script([tool_call("a", x=1) + "\n" + tool_call("b", y=2), "done"])
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        events = [ev async for ev in run_agent_step(
            message="go", agent_id="a1", session_id="s", agent_override=agent,
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            tools_override=[schema("a"), schema("b")], tool_executor=executor, max_turns=4,
        )]
        assert executed == [("a", {"x": 1}), ("b", {"y": 2})]
        assert fake_llm.call_count == 2
        assert events[-1]["type"] == "final"
