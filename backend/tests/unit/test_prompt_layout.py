"""
The prompt layout every provider's prefix cache needs: a system prompt that
does not change across the turns of a step, an append-only transcript sent as
one block per turn, and everything volatile in a tail at the end of the user
message.

The fake LLM records every kwarg of every call, so these assert on exactly
what a provider would receive — no token counting, no network.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
# Captured at import (collection) time: the autouse `fake_llm` fixture replaces
# `core.llm_providers.generate_response` with the fake before each test runs,
# and the provider-dispatch tests below need the real one.
from core.llm_providers import LLMResult, generate_response as _real_generate_response


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _tool_schema(name="my_tool"):
    return {"type": "function", "function": {
        "name": name, "description": "test tool",
        "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}}


async def _drive(agent, fake_llm, script, **kw):
    from core.react_engine import run_agent_step
    fake_llm.script(script)
    kw.setdefault("tools_override", [_tool_schema()])
    kw.setdefault("tool_executor", _echo)
    return [ev async for ev in run_agent_step(
        message="go", agent_id=agent.get("id"), session_id="s1",
        server_module=_server(), agent_override=agent, max_turns=8, **kw,
    )]


async def _echo(name, args):
    return f"OUT({name},{args})"


def _agent(**over):
    return S.make_agent(tools=["all"], skip_default_tools=True, **over)


def _three_turns():
    return [tool_call("my_tool", x=1), tool_call("my_tool", x=2), "done"]


class TestSystemPromptIsStableWithinAStep:
    async def test_identical_on_every_turn(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns())
        systems = [c["sys_prompt"] for c in fake_llm.calls]
        assert len(systems) == 3
        assert systems[0] == systems[1] == systems[2]

    async def test_no_per_turn_text_in_it(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns())
        system = fake_llm.calls[0]["sys_prompt"]
        for volatile in ("TURN BUDGET", "turn(s) remaining", "CURRENT DATE & TIME", "\n---\n"):
            assert volatile not in system, volatile

    async def test_orchestration_graph_and_banner_are_in_it(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns(),
                     system_prompt_extra="## WORKFLOW GRAPH\n1. step", system_prompt_prefix="ITERATION CONTEXT")
        systems = [c["sys_prompt"] for c in fake_llm.calls]
        assert systems[0].startswith("ITERATION CONTEXT")
        assert "## WORKFLOW GRAPH" in systems[0]
        assert systems[0] == systems[1] == systems[2]


class TestVolatileTailIsAtTheEnd:
    async def test_turn_budget_and_datetime_end_the_user_message(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns())
        for c in fake_llm.calls:
            prompt = c["prompt_msg"]
            assert prompt.endswith(c["volatile_tail"])
            assert "TURN BUDGET" in c["volatile_tail"]
            assert "CURRENT DATE & TIME" in c["volatile_tail"]

    async def test_tail_changes_but_the_transcript_before_it_does_not(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns())
        c1, c2 = fake_llm.calls[1], fake_llm.calls[2]
        assert c1["volatile_tail"] != c2["volatile_tail"]  # turns remaining moved
        transcript1 = "".join(c1["prompt_blocks"])
        transcript2 = "".join(c2["prompt_blocks"])
        assert transcript2.startswith(transcript1)
        assert c1["prompt_msg"] == transcript1 + "\n\n" + c1["volatile_tail"]

    async def test_task_list_rides_the_tail(self, fake_llm):
        agent = S.make_agent(tools=["all"])  # default tools include update_tasks
        await _drive(agent, fake_llm, [
            tool_call("update_tasks", tasks=[{"content": "first", "status": "completed"}]),
            "done",
        ], tools_override=None, tool_executor=None)
        second = fake_llm.calls[1]
        assert "Current task list" in second["volatile_tail"]
        assert "Current task list" not in second["sys_prompt"]


class TestTranscriptBlocks:
    async def test_one_block_per_turn_whatever_the_tool_count(self, fake_llm):
        two_calls = tool_call("my_tool", x=1) + "\n" + tool_call("my_tool", x=2)
        await _drive(_agent(), fake_llm, [two_calls, tool_call("my_tool", x=3), "done"])
        blocks = [c["prompt_blocks"] for c in fake_llm.calls]
        assert [len(b) for b in blocks] == [1, 2, 3]

    async def test_every_turn_extends_the_previous_blocks_byte_for_byte(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns())
        b0, b1, b2 = (c["prompt_blocks"] for c in fake_llm.calls)
        assert b1[: len(b0)] == b0
        assert b2[: len(b1)] == b1

    async def test_first_turn_block_matches_its_successor(self, fake_llm):
        """Turn 0 used to send the bare message while turn 1 sent it framed."""
        await _drive(_agent(), fake_llm, _three_turns())
        b0, b1 = fake_llm.calls[0]["prompt_blocks"], fake_llm.calls[1]["prompt_blocks"]
        assert b0 == [b1[0]]
        assert b0[0].startswith("User Request: go")

    async def test_joined_blocks_plus_tail_is_the_prompt_every_provider_sees(self, fake_llm):
        await _drive(_agent(), fake_llm, _three_turns())
        for c in fake_llm.calls:
            assert "".join(c["prompt_blocks"]) + "\n\n" + c["volatile_tail"] == c["prompt_msg"]

    def test_split_is_lossless(self):
        from core.react_engine import _transcript_blocks
        text = "User Request: go\n\n[Turn 1 — Action]\n{a}\n\n[Turn 1 — Observation] x\n\n[Turn 2 — Action]\n{b}\n"
        blocks = _transcript_blocks(text)
        assert "".join(blocks) == text
        assert len(blocks) == 3
        assert blocks[1].startswith("\n[Turn 1 — Action]")
        assert "[Turn 1 — Observation]" in blocks[1]   # observations stay in their turn


class _FakeMcpSession:
    """Just enough of an MCP session for the dispatch path browser tools take."""

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"<html>page {args}</html>")])


class TestBrowserSnapshotsDoNotRewriteTheTranscript:
    async def test_earlier_blocks_are_untouched_and_the_snapshot_is_in_the_tail(self, fake_llm):
        # Browser handling lives on the MCP dispatch path, not the tool_executor
        # shortcut, so the tools are routed to a fake session.
        from core.react_engine import run_agent_step
        server = types.SimpleNamespace(
            agent_sessions={"browser_srv": _FakeMcpSession()},
            memory_store=None,
            tool_router={"browser_navigate": ("browser_srv", "browser_navigate"),
                         "browser_click": ("browser_srv", "browser_click")},
        )
        fake_llm.script([tool_call("browser_navigate", url="a"), tool_call("browser_click", sel="b"), "done"])
        events = [ev async for ev in run_agent_step(
            message="go", agent_id="a1", session_id="s1", server_module=server,
            agent_override=_agent(), max_turns=8,
            tools_override=[_tool_schema("browser_navigate"), _tool_schema("browser_click")],
        )]
        assert any(e.get("type") == "tool_result" for e in events)
        c1, c2 = fake_llm.calls[1], fake_llm.calls[2]
        assert c2["prompt_blocks"][: len(c1["prompt_blocks"])] == c1["prompt_blocks"]
        assert "<<BROWSER_STATE>>" in c2["volatile_tail"]
        assert "page {'sel': 'b'}" in c2["volatile_tail"]
        assert "<<BROWSER_STATE>>" not in "".join(c2["prompt_blocks"])
        assert "Previous browser actions" in c2["volatile_tail"]


class TestAfterCompaction:
    async def test_the_next_turn_is_prefix_stable_again(self, fake_llm, monkeypatch):
        import core.react_engine as re_mod
        compacted = {"done": False}

        async def fake_compact(ctx, hist, *a, **kw):
            if compacted["done"] or "[Turn 2" not in ctx:
                return ctx, hist, None, None
            compacted["done"] = True
            return "User Request: go\n[compacted summary]\n", [], None, {"stage": "t"}

        monkeypatch.setattr(re_mod, "maybe_compact", fake_compact)
        await _drive(_agent(), fake_llm, [tool_call("my_tool", x=1), tool_call("my_tool", x=2),
                                          tool_call("my_tool", x=3), "done"])
        blocks = [c["prompt_blocks"] for c in fake_llm.calls]
        assert blocks[2][0].startswith("User Request: go\n[compacted summary]")   # the miss
        assert blocks[3][: len(blocks[2])] == blocks[2]                          # stable again


class TestOtherProvidersAreUntouched:
    async def test_non_anthropic_receives_the_joined_string(self, monkeypatch):
        """`prompt_blocks` is a structured view for the one provider that can
        mark a breakpoint inside the conversation; every other path must see
        the same text it saw before, as one message."""
        import core.llm_providers as lp
        seen = {}

        async def fake_openai(model, messages, key, tools=None, images=None, prompt_cache=True,
                              reasoning=None):
            seen["messages"] = messages
            return LLMResult("ok", 1, 1, 0, 0)

        monkeypatch.setattr(lp, "call_openai", fake_openai)
        await _real_generate_response(
            prompt_msg="A\nB\n\ntail", sys_prompt="sys", mode="cloud", current_model="gpt-4o",
            current_settings={"openai_key": "k"}, prompt_blocks=["A\n", "B\n"], volatile_tail="tail",
        )
        assert seen["messages"][-1] == {"role": "user", "content": "A\nB\n\ntail"}

    async def test_anthropic_receives_blocks(self, monkeypatch):
        import core.llm_providers as lp
        seen = {}

        class _Resp:
            content = [types.SimpleNamespace(type="text", text="ok")]
            usage = types.SimpleNamespace(input_tokens=1, output_tokens=1,
                                          cache_read_input_tokens=0, cache_creation_input_tokens=0)
            stop_reason = "end_turn"

        class _Messages:
            async def create(self, **kwargs):
                seen["kwargs"] = kwargs
                return _Resp()

        class _Client:
            def __init__(self, **kw):
                self.messages = _Messages()

        import anthropic
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
        await lp.call_anthropic(
            "claude-test", [{"role": "user", "content": "A\nB\n\ntail"}], "S" * 5000, "key",
            prompt_blocks=["A\n", "B\n"], volatile_tail="tail",
        )
        content = seen["kwargs"]["messages"][-1]["content"]
        assert [b["text"] for b in content] == ["A\n", "B\n", "tail"]
        assert "cache_control" in content[1] and "cache_control" not in content[2]
        assert seen["kwargs"]["system"][0]["cache_control"] == {"type": "ephemeral"}

    async def test_images_are_prepended_to_the_blocks(self, monkeypatch):
        import core.llm_providers as lp
        seen = {}

        class _Resp:
            content = [types.SimpleNamespace(type="text", text="ok")]
            usage = types.SimpleNamespace(input_tokens=1, output_tokens=1,
                                          cache_read_input_tokens=0, cache_creation_input_tokens=0)
            stop_reason = "end_turn"

        class _Messages:
            async def create(self, **kwargs):
                seen["kwargs"] = kwargs
                return _Resp()

        class _Client:
            def __init__(self, **kw):
                self.messages = _Messages()

        import anthropic
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
        png = "data:image/png;base64,aGVsbG8="
        await lp.call_anthropic(
            "claude-test", [{"role": "user", "content": "A"}], "sys", "key",
            images=[png], prompt_blocks=["A"], volatile_tail=None,
        )
        content = seen["kwargs"]["messages"][-1]["content"]
        assert content[0]["type"] == "image"
        assert content[1] == {"type": "text", "text": "A"}


class TestEvaluatorPrompt:
    async def test_datetime_is_not_at_the_top(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        fake_llm.script([tool_call("route_ok", reasoning="fine")])
        orch = S.make_orchestration(
            id="o_eval", entry_step_id="ev",
            steps=[{"id": "ev", "name": "Judge", "type": "evaluator",
                    "route_map": {"ok": None}, "evaluator_prompt": "Judge it.",
                    "input_keys": ["user_input"], "next_step_id": None}],
        )
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [e async for e in engine.run("x" * 600, run_id="run_eval_dt")]
        assert any(e.get("type") == "routing_decision" for e in events)
        prompt = fake_llm.calls[0]["prompt_msg"]
        assert "CURRENT DATE & TIME" not in prompt[:500]
        assert "CURRENT DATE & TIME" in prompt


class TestUsageSummaryCarriesCacheReads:
    async def test_by_run_bucket_sums_cache_tokens(self):
        from core import usage_tracker as ut
        await ut.log_usage(model="claude-x", provider="anthropic", input_tokens=100, output_tokens=10,
                           context_chars=1, source="orchestration", run_id="r1", agent_id="a",
                           session_id="s", cache_read_tokens=80, cache_write_tokens=20)
        await ut.log_usage(model="claude-x", provider="anthropic", input_tokens=100, output_tokens=10,
                           context_chars=1, source="orchestration", run_id="r1", agent_id="a",
                           session_id="s", cache_read_tokens=90, cache_write_tokens=0)
        summary = await ut.get_usage_summary()
        # Orchestration runs are merged into by_session, one entry per run_id.
        run = next(r for r in summary["by_session"] if r.get("run_id") == "r1")
        assert run["cache_read_tokens"] == 170
        assert run["cache_write_tokens"] == 20


class TestProvidersWithoutNativeToolsGetThemInThePrompt:
    """A local HuggingFace model is called with no `tools=` (it has no native
    tool calling), so its tools have to be in the prompt — as for Bedrock and
    the CLIs. `hf` was missing from that list, so an HF agent was never told
    it had any tools (F64)."""

    @pytest.mark.parametrize("model", ["hf.Qwen/Qwen2.5-7B-Instruct", "bedrock.amazon.nova-pro-v1:0"])
    async def test_the_tool_is_described_in_the_system_prompt(self, fake_llm, model):
        await _drive(_agent(model=model), fake_llm, [tool_call("my_tool", x=1), "done"])

        system = fake_llm.calls[0]["sys_prompt"]
        assert "### TOOLS" in system
        assert "my_tool" in system

    async def test_the_tool_call_it_writes_back_runs(self, fake_llm):
        events = await _drive(_agent(model="hf.Qwen/Qwen2.5-7B-Instruct"), fake_llm,
                              [tool_call("my_tool", x=1), "done"])

        assert any(e.get("type") == "tool_result" and "OUT(my_tool" in str(e) for e in events)
