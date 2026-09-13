"""
Deep coverage of core.react_engine.run_agent_step: the tool-execution loop,
reasoning projection, blocked/unavailable tools, the max-turns guard, LLM error
handling, and delegate-agent context injection — all driven by the fake LLM.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _tool_schema(name="my_tool"):
    return {"type": "function", "function": {
        "name": name, "description": "test tool",
        "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}}


async def _drive(agent, fake_llm, script, *, tools_override=None, tool_executor=None,
                 max_turns=5, message="go"):
    from core.react_engine import run_agent_step
    fake_llm.script(script)
    events = []
    async for ev in run_agent_step(
        message=message, agent_id=agent.get("id"), session_id="s1",
        server_module=_server(), agent_override=agent,
        tools_override=tools_override, tool_executor=tool_executor, max_turns=max_turns,
    ):
        events.append(ev)
    return events


def _types(events):
    return [e.get("type") for e in events]


class TestToolLoop:
    async def test_tool_executed_then_final(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        calls = []

        async def executor(name, args):
            calls.append((name, args))
            return "TOOL-OUTPUT-42"

        events = await _drive(
            agent, fake_llm,
            [tool_call("my_tool", x=1), "The final answer."],
            tools_override=[_tool_schema()], tool_executor=executor,
        )
        assert calls == [("my_tool", {"x": 1})]
        results = [e for e in events if e.get("type") == "tool_result"]
        assert results and "TOOL-OUTPUT-42" in results[-1]["preview"]
        finals = [e for e in events if e.get("type") == "final"]
        assert finals and finals[-1]["response"] == "The final answer."

    async def test_unavailable_tool_is_blocked(self, fake_llm):
        # Agent allows no tools -> a tool call is blocked, then it wraps up.
        agent = S.make_agent(tools=[], skip_default_tools=True)
        events = await _drive(
            agent, fake_llm,
            [tool_call("my_tool", x=1), "wrapped up"],
            tools_override=[_tool_schema()], tool_executor=None,
        )
        previews = [e.get("preview", "") for e in events if e.get("type") == "tool_result"]
        assert any("not available" in p.lower() or "blocked" in p.lower() for p in previews)


class TestReasoningAndThought:
    async def test_reasoning_block_projected(self, fake_llm):
        agent = S.make_agent(tools=[], skip_default_tools=True)
        events = await _drive(
            agent, fake_llm,
            ["[REASONING]thinking hard[/REASONING]\nHere is the answer."],
            tools_override=[],
        )
        reasoning = [e for e in events if e.get("type") == "llm_reasoning"]
        assert reasoning and "thinking hard" in reasoning[0]["reasoning"]
        finals = [e for e in events if e.get("type") == "final"]
        assert "Here is the answer." in finals[-1]["response"]


class TestErrorAndGuards:
    async def test_llm_exception_yields_error_event(self, fake_llm, monkeypatch):
        agent = S.make_agent(tools=[], skip_default_tools=True)

        async def _boom(*a, **k):
            raise RuntimeError("model unreachable")

        import core.react_engine as re
        monkeypatch.setattr(re, "llm_generate_response", _boom)
        from core.react_engine import run_agent_step
        events = [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[], max_turns=3)]
        assert any(e.get("type") == "error" for e in events)

    async def test_max_turns_guard(self, fake_llm):
        # Keep calling a tool forever -> the max_turns guard must stop the loop.
        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return "again"

        # Script more tool calls than max_turns allows.
        events = await _drive(
            agent, fake_llm,
            [tool_call("my_tool", x=1)] * 6,
            tools_override=[_tool_schema()], tool_executor=executor, max_turns=2,
        )
        # Loop terminated (a final/summary is produced rather than looping forever).
        assert any(e.get("type") in ("final", "error") for e in events)
        assert fake_llm.call_count <= 4  # bounded by the guard, not the script length


class TestToolLoopVariants:
    async def test_multiple_tool_calls_in_one_turn(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        seen = []

        async def executor(name, args):
            seen.append(name)
            return f"out-{name}"

        # Two tool-call JSON objects in a single LLM response, then a final.
        two_calls = tool_call("tool_a", x=1) + "\n" + tool_call("tool_b", y=2)
        events = await _drive(
            agent, fake_llm, [two_calls, "all done"],
            tools_override=[_tool_schema("tool_a"), _tool_schema("tool_b")],
            tool_executor=executor,
        )
        assert seen == ["tool_a", "tool_b"]
        assert any(e.get("type") == "final" for e in events)

    async def test_post_tool_hook_emits_extra_events(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return "ok"

        async def hook(name, raw_output):
            yield {"type": "custom_hook_event", "tool": name}

        from core.react_engine import run_agent_step
        fake_llm.script([tool_call("my_tool", x=1), "done"])
        events = [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[_tool_schema()],
            tool_executor=executor, post_tool_hook=hook, max_turns=3)]
        assert any(e.get("type") == "custom_hook_event" for e in events)

    async def test_history_override_and_images(self, fake_llm):
        agent = S.make_agent(tools=[], skip_default_tools=True)
        from core.react_engine import run_agent_step
        fake_llm.script(["final with context"])
        events = [ev async for ev in run_agent_step(
            message="describe", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[],
            history_override=[{"role": "user", "content": "earlier"},
                              {"role": "assistant", "content": "reply"}],
            images=["data:image/png;base64,iVBORw0KGgo="], max_turns=2)]
        assert any(e.get("type") == "final" for e in events)
        # The fake LLM received the overridden history + image on the first turn.
        assert fake_llm.last_call.get("history_messages")


class TestConversationHistoryAcrossTurns:
    """The conversation must reach the model on every turn, not just the first.

    `active_history` used to be `recent_history_messages if turn == 0 else []`,
    so one tool call blanked the conversation for the rest of the turn: the
    model reasoned with context, called a tool, and answered the observation
    having forgotten what was being talked about. It read as amnesia to the
    user and as a model failure to us, because turn 0's reasoning — published
    as `llm_reasoning` — still had the history the answer no longer did.

    `last_call` cannot catch this. A single-turn script makes `last_call` *be*
    turn 0, which is exactly the turn that was never broken; the assertion has
    to walk every call.
    """

    HISTORY = [
        {"role": "user", "content": "add the sun research to a google doc"},
        {"role": "assistant", "content": "The connection was revoked; here is the research."},
    ]

    async def _calls_for(self, fake_llm, script, **kwargs):
        from core.react_engine import run_agent_step
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        fake_llm.script(script)

        async def executor(name, args):
            return "TOOL-OUTPUT"

        events = [ev async for ev in run_agent_step(
            message="try again now", agent_id=agent["id"], session_id="s-hist",
            server_module=_server(), agent_override=agent,
            tools_override=[_tool_schema()], tool_executor=executor,
            history_override=self.HISTORY, max_turns=5, **kwargs)]
        assert any(e.get("type") == "final" for e in events), _types(events)
        return fake_llm.calls

    async def test_a_tool_call_does_not_erase_the_conversation(self, fake_llm):
        calls = await self._calls_for(
            fake_llm, [tool_call("my_tool", x=1), "Filed it."])
        assert len(calls) >= 2, "the script should have driven two LLM turns"
        for turn, call in enumerate(calls):
            assert call.get("history_messages") == self.HISTORY, (
                f"turn {turn} lost the conversation: {call.get('history_messages')!r}"
            )

    async def test_history_survives_several_tool_calls(self, fake_llm):
        calls = await self._calls_for(
            fake_llm,
            [tool_call("my_tool", x=1), tool_call("my_tool", x=2), "Done."])
        assert len(calls) >= 3
        assert all(c.get("history_messages") == self.HISTORY for c in calls)

    async def test_a_resumed_turn_still_carries_the_conversation(self, fake_llm):
        # A tool-approval resume seeds `context_text` and sets `_resumed_mid_step`,
        # which used to blank history on the resumed turn 0 as well — so an
        # approved tool call answered with no idea what the chat was about.
        calls = await self._calls_for(
            fake_llm, ["Resumed and answered."],
            resume_state={"context_text": "User Request: try again now\n[Turn 1 — Action]\n",
                          "turn": 1})
        assert calls
        assert all(c.get("history_messages") == self.HISTORY for c in calls)

class TestWaitingOnTheModel:
    async def test_a_slow_model_call_keeps_the_watcher_informed(self, fake_llm, monkeypatch):
        """One gateway call hung for 187s inside an agent step and the run page
        showed a single "Analyzing your request..." the whole time — a healthy
        run that read as a stuck one."""
        import core.model_wait as mw
        monkeypatch.setattr(mw, "HEARTBEAT_SECONDS", 0.05)
        monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_MIN", "0.25")
        monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_MAX", "0.25")
        monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_PROB", "1")
        agent = S.make_agent(tools=[], skip_default_tools=True)

        events = await _drive(agent, fake_llm, ["Here is the answer."], tools_override=[])

        beats = [e for e in events if e.get("type") == "thinking"
                 and "waiting on the model" in str(e.get("message", ""))]
        assert len(beats) >= 2, _types(events)
        assert any(e.get("type") == "final" for e in events)

class TestDelegateContext:
    async def test_delegate_agent_builds_roster(self, fake_llm, seed_agent):
        target = await seed_agent(id="worker", name="Worker", description="does work")
        delegate = S.make_agent(id="lead", name="Lead", type="delegate",
                                 delegate_agent_ids=[target["id"]], tools=["all"])
        await S.seed_agents([target, delegate])
        # No tools_override -> the real delegate-context injection path runs.
        from core.react_engine import run_agent_step
        fake_llm.script(["All delegated, done."])
        events = [ev async for ev in run_agent_step(
            message="handle this", agent_id="lead", session_id="s1",
            server_module=_server(), max_turns=2)]
        assert any(e.get("type") == "final" for e in events)

    async def test_a_delegate_keeps_the_conversation_and_its_sub_agent_starts_fresh(self, fake_llm, seed_agent):
        """A delegate was grouped with orchestrators and the builder as "always start
        fresh", so it forgot every earlier message in a chat and never saved its own
        turn. Its sub-agent, meanwhile, read *its own* direct chat under the same
        session id and saved the delegated task into it — on cloud, a second
        conversation row that reopening the chat could pick instead (2026-09-13)."""
        from core.react_engine import run_agent_step
        from core.session import _save_conversation_turn, get_recent_history_messages

        worker = S.make_agent(id="worker", name="Worker", description="does work")
        lead = S.make_agent(id="lead", name="Lead", type="delegate", tools=[])
        await S.seed_agents([worker, lead])
        await _save_conversation_turn(session_id="s1", agent_id="lead",
                                      user="My name is Ada.", assistant="Noted, Ada.", tools=[])
        await _save_conversation_turn(session_id="s1", agent_id="worker",
                                      user="A private direct chat.", assistant="Private reply.", tools=[])

        fake_llm.script([
            tool_call("delegate_to_agent", agent_id="worker", task="Greet the user by name: Ada."),
            "Hello, Ada.",
            "The worker said: Hello, Ada.",
        ])
        events = [ev async for ev in run_agent_step(
            message="Have the worker greet me.", agent_id="lead", session_id="s1",
            server_module=_server(), max_turns=3)]
        assert any(e.get("type") == "final" for e in events)

        lead_call, worker_call = str(fake_llm.calls[0]), str(fake_llm.calls[1])
        assert "My name is Ada." in lead_call, "the delegate forgot the conversation"
        assert "A private direct chat." not in worker_call, "the sub-agent read its own chat"

        worker_history = await get_recent_history_messages("s1", agent_id="worker")
        assert "Greet the user by name" not in str(worker_history), "the hand-off was saved as a chat"
        lead_history = await get_recent_history_messages("s1", agent_id="lead")
        assert "Have the worker greet me." in str(lead_history), "the delegate's turn was not saved"

    async def test_the_roster_is_the_workspace_agents_and_nothing_else(self, fake_llm, seed_agent):
        """The builder's sub-agents are `conversational` with a `system` marker,
        so skipping only `type == "builder"` let a delegate hand customer work to
        the builder's own saver. And "other delegate agents (no infinite loops)"
        was a comment the loop never implemented."""
        from core.react_engine import _build_delegate_context

        worker = S.make_agent(id="worker", name="Worker", description="does work")
        machinery = S.make_agent(id="builder_saver", name="Builder - Create Saver", system="native_builder")
        other_lead = S.make_agent(id="other_lead", name="Other lead", type="delegate")
        lead = S.make_agent(id="lead", name="Lead", type="delegate", tools=["all"])
        await S.seed_agents([worker, machinery, other_lead, lead])

        tool_schema_map: dict = {}
        eligible = await _build_delegate_context(lead, [], tool_schema_map, [])

        assert set(eligible) == {"worker"}
        assert tool_schema_map["delegate_to_agent"]["properties"]["agent_id"]["enum"] == ["worker"]
