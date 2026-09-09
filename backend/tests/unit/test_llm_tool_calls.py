"""
Every native tool call a provider returns reaches the loop.

The extractors used to keep the first call and drop the rest — silently on
Anthropic and OpenAI, with a warning on Gemini. A model that answered with two
parallel calls ran one. These drive the extractors directly with the response
shapes the SDKs produce; no network.
"""
import json
import types

from core.react_engine import extract_reasoning, parse_all_tool_calls


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
        out = _extract_anthropic_response(self._response(
            self._text("I'll fetch both."), self._tool("read_file", path="a"), self._tool("grep", pattern="x"),
        ))
        assert _calls(out) == [("read_file", {"path": "a"}), ("grep", {"pattern": "x"})]
        assert extract_reasoning(out) == ["I'll fetch both."]

    def test_a_single_call_is_unchanged(self):
        from core.llm_providers import _extract_anthropic_response
        out = _extract_anthropic_response(self._response(self._tool("read_file", path="a")))
        assert out == json.dumps({"tool": "read_file", "arguments": {"path": "a"}})

    def test_text_only_is_text(self):
        from core.llm_providers import _extract_anthropic_response
        out = _extract_anthropic_response(self._response(self._text("Done."), self._text("Really.")))
        assert out == "Done.\nReally."
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
        out = _openai_compat_extract(self._msg("checking", ("search", {"q": "1"}), ("search", {"q": "2"})))
        assert _calls(out) == [("search", {"q": "1"}), ("search", {"q": "2"})]
        assert extract_reasoning(out) == ["checking"]

    def test_a_single_call_is_unchanged(self):
        from core.llm_providers import _openai_compat_extract
        out = _openai_compat_extract(self._msg(None, ("search", {"q": "1"})))
        assert out == json.dumps({"tool": "search", "arguments": {"q": "1"}})

    def test_empty_arguments_string_is_an_empty_object(self):
        from core.llm_providers import _openai_compat_extract
        msg = {"content": None, "tool_calls": [{"function": {"name": "ping", "arguments": ""}}]}
        assert _calls(_openai_compat_extract(msg)) == [("ping", {})]


class TestGemini:
    @staticmethod
    def _response(*parts):
        return types.SimpleNamespace(candidates=[types.SimpleNamespace(
            finish_reason=types.SimpleNamespace(name="STOP"),
            content=types.SimpleNamespace(parts=list(parts)),
        )])

    @staticmethod
    def _text(t):
        return types.SimpleNamespace(text=t, function_call=None)

    @staticmethod
    def _fn(name, **args):
        return types.SimpleNamespace(text=None, function_call=types.SimpleNamespace(name=name, args=args))

    def test_two_function_calls_both_survive_in_order(self):
        from core.llm_providers import _extract_gemini_response
        out = _extract_gemini_response(self._response(self._text("two lookups"), self._fn("a", x=1), self._fn("b", y=2)))
        assert _calls(out) == [("a", {"x": 1}), ("b", {"y": 2})]
        assert extract_reasoning(out) == ["two lookups"]

    def test_a_single_call_is_unchanged(self):
        from core.llm_providers import _extract_gemini_response
        out = _extract_gemini_response(self._response(self._fn("a", x=1)))
        assert out == json.dumps({"tool": "a", "arguments": {"x": 1}})


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
