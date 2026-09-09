"""
Two ways a turn used to end the run wrongly: a tool call the model wrote
badly, and a context that outgrew the window.

A response with no parsed tool call is the loop's "this is the final answer"
signal. That made a malformed call the agent's own words, and made the 400k
backstop — which kept the head and dropped the newest observations — a way to
lose exactly the results the model was about to act on.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _schema(name="my_tool"):
    return {"type": "function", "function": {"name": name, "description": "t",
                                             "parameters": {"type": "object", "properties": {}}}}


async def _echo(name, args):
    return f"OUT({name})"


async def _drive(fake_llm, script, **kw):
    from core.react_engine import run_agent_step
    fake_llm.script(script)
    kw.setdefault("tools_override", [_schema()])
    kw.setdefault("tool_executor", _echo)
    return [ev async for ev in run_agent_step(
        message="go", agent_id="a1", session_id="s1", server_module=_server(),
        agent_override=S.make_agent(tools=["all"], skip_default_tools=True),
        max_turns=kw.pop("max_turns", 6), **kw,
    )]


def _final(events):
    return next(e["response"] for e in reversed(events) if e.get("type") == "final")


class TestAMalformedToolCallIsNotAnAnswer:
    @pytest.mark.parametrize("broken,label", [
        ('{"tool": "my_tool", "arguments": {"x": 1},}', "trailing comma"),
        ("{'tool': 'my_tool', 'arguments': {}}", "single quotes"),
        ('{"tool": "my_tool", "arguments": {"x": ', "truncated"),
    ])
    async def test_it_is_fed_back_once_then_the_call_runs(self, fake_llm, broken, label):
        executed = []

        async def executor(name, args):
            executed.append(name)
            return "ok"

        events = await _drive(fake_llm, [broken, tool_call("my_tool"), "done"], tool_executor=executor)

        assert [e["type"] for e in events].count("tool_call_unparsed") == 1
        assert executed == ["my_tool"]          # the retry parsed and ran
        assert _final(events) == "done"
        assert broken not in _final(events)      # never shown to the user

    async def test_it_is_retried_only_once(self, fake_llm):
        broken = '{"tool": "my_tool", "arguments": {,}'
        events = await _drive(fake_llm, [broken, broken, "recovered"])
        assert [e["type"] for e in events].count("tool_call_unparsed") == 1
        # Second failure is accepted as the answer rather than looping.
        assert _final(events) == broken

    async def test_prose_is_still_a_final_answer_on_the_first_turn(self, fake_llm):
        events = await _drive(fake_llm, ["Here is the answer. No tools needed."])
        assert not any(e["type"] == "tool_call_unparsed" for e in events)
        assert fake_llm.call_count == 1
        assert _final(events) == "Here is the answer. No tools needed."

    async def test_prose_that_merely_mentions_braces_is_not_a_tool_call(self, fake_llm):
        answer = 'Use a dict like {"name": "x"} in your config.'
        events = await _drive(fake_llm, [answer])
        assert fake_llm.call_count == 1
        assert _final(events) == answer

    def test_the_detector_reads_the_shape_not_the_braces(self):
        from core.react_engine import looks_like_a_tool_call
        assert looks_like_a_tool_call('{"tool": "x", "arguments": {}}')
        assert looks_like_a_tool_call("<tool_call>{bad}</tool_call>")
        assert looks_like_a_tool_call("{'tool': 'x'}")
        assert not looks_like_a_tool_call("Here is JSON: {\"name\": \"x\"}")
        assert not looks_like_a_tool_call("plain prose")
        assert not looks_like_a_tool_call("")


class TestTheContextBackstopKeepsTheNewestEnd:
    async def test_the_middle_is_dropped_and_the_marker_says_so(self, fake_llm, monkeypatch):
        import core.react_engine as re_mod
        monkeypatch.setattr(re_mod, "maybe_compact", _no_compact)

        big = "Y" * 90_000

        async def fat_executor(name, args):
            return big

        # Five fat observations blow past the 400k backstop.
        script = [tool_call("my_tool") for _ in range(5)] + ["done"]
        events = await _drive(fake_llm, script, tool_executor=fat_executor, max_turns=8)

        trims = [e for e in events if e["type"] == "context_trimmed"]
        assert trims and trims[0]["chars_dropped"] > 0

        sent = fake_llm.calls[-1]["prompt_msg"]
        assert sent.startswith("User Request: go")        # the request survives
        assert "characters of earlier observations trimmed" in sent
        assert sent.rstrip().endswith(fake_llm.calls[-1]["volatile_tail"].rstrip())
        # The newest observation is still there — that is the whole point.
        assert sent.index("trimmed") < sent.rindex("Observation")

    async def test_nothing_is_trimmed_when_it_fits(self, fake_llm):
        events = await _drive(fake_llm, [tool_call("my_tool"), "done"])
        assert not any(e["type"] == "context_trimmed" for e in events)


async def _no_compact(ctx, hist, *a, **k):
    return ctx, hist, None, None


class TestCompactionCanFireMoreThanOnce:
    async def test_a_second_compaction_happens_after_the_cooldown(self, fake_llm, monkeypatch):
        import core.react_engine as re_mod
        fired: list[int] = []

        async def compact(ctx, hist, *a, **k):
            fired.append(len(ctx))
            return "User Request: go\n[compacted]\n", [], None, {"stage": "t"}

        monkeypatch.setattr(re_mod, "maybe_compact", compact)
        script = [tool_call("my_tool") for _ in range(7)] + ["done"]
        events = await _drive(fake_llm, script, max_turns=9)

        assert len(fired) >= 2, "compaction fired once and then never again"
        assert [e["type"] for e in events].count("context_compact") >= 2

    async def test_the_cooldown_keeps_it_off_consecutive_turns(self, fake_llm, monkeypatch):
        import core.react_engine as re_mod
        turns_fired: list[int] = []

        async def compact(ctx, hist, *a, **k):
            turns_fired.append(len(turns_fired))
            return ctx + "\n[compacted]\n", [], None, {"stage": "t"}

        monkeypatch.setattr(re_mod, "maybe_compact", compact)
        script = [tool_call("my_tool") for _ in range(4)] + ["done"]
        events = await _drive(fake_llm, script, max_turns=6)
        compactions = [e for e in events if e["type"] == "context_compact"]
        assert len(compactions) <= 2   # not every turn

    async def test_an_older_checkpoints_bool_is_understood(self, fake_llm, monkeypatch):
        """A run paused by a build that stored `has_compacted` must resume."""
        import core.react_engine as re_mod
        monkeypatch.setattr(re_mod, "maybe_compact", _no_compact)
        events = await _drive(
            fake_llm, ["done"],
            resume_state={"context_text": "User Request: go\n", "turn": 2,
                          "has_compacted": True, "browser_actions": [], "tools_used": []},
        )
        assert _final(events) == "done"


class TestCompactionIsOnByDefault:
    async def test_the_setting_defaults_to_enabled(self):
        from core.compaction import maybe_compact
        ctx = "x" * 200_000
        out, _hist, _path, stats = await maybe_compact(
            ctx, [], {"auto_compact_threshold": 100_000}, "m", "cloud", {}, "s", "a",
        )
        assert out != ctx and stats is not None      # it fired with no explicit enable

    async def test_it_can_still_be_turned_off(self):
        from core.compaction import maybe_compact
        ctx = "x" * 200_000
        out, _hist, _path, stats = await maybe_compact(
            ctx, [], {"auto_compact_enabled": False, "auto_compact_threshold": 100_000},
            "m", "cloud", {}, "s", "a",
        )
        assert out == ctx and stats is None
