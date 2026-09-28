"""A compaction keeps the question it is working on (F244).

Turn 13 of a 14-turn chat asked a pure recall question. Stage-2 compaction ran
mid-turn — 92,899 → 2,478 characters (−97%, against the ~30% its prompt asks
for) — and replaced the transcript wholesale: the question went with it. Ten
seconds later the agent's task list was "Implement missing features", and it
edited two files nobody had asked about. Stage 1 lost it too, keeping only the
newest end of the transcript, whose head is the request.

Now both stages keep the request verbatim at the head, and a summary that cut
below a fifth of what it replaced keeps the newest transcript verbatim with it.
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

QUESTION = "what's the release codename and ticket I gave you earlier?"


def _ctx(body_chars: int, newest: str = "NEWEST-OBSERVATION") -> str:
    return (f"User Request: {QUESTION}\n"
            + "".join(f"\n[Turn {i} — Observation] Tool 'grep' returned:\n" + "o" * 900 + "\n"
                      for i in range(body_chars // 950))
            + f"\n[Turn 99 — Observation] Tool 'grep' returned:\n{newest}\n")


async def _compact(ctx, history, threshold, **kw):
    from core.compaction import maybe_compact
    return await maybe_compact(ctx, history, {"auto_compact_threshold": threshold}, "m", "cloud", {},
                               "s_f244", "a_f244", **kw)


class TestStageTwo:
    async def test_the_question_survives_and_a_fifth_is_kept(self, fake_llm):
        fake_llm.set_default("SUMMARY: searched for the codename.")
        ctx = _ctx(5_000)
        history = [{"role": "user", "content": "u" * 6_000}, {"role": "assistant", "content": "a" * 6_000}]
        before = len(ctx) + 12_000

        out, hist, _archive, stats = await _compact(ctx, history, 10_000, pinned_request=QUESTION)

        assert stats["stage"] == "llm_summary"
        assert out.startswith(f"User Request: {QUESTION}\n"), out[:200]
        assert len(out) >= 0.2 * before, (len(out), before)
        assert "NEWEST-OBSERVATION" in out, "the newest observation is kept verbatim"
        assert "SUMMARY: searched for the codename." in out
        assert hist == []

    async def test_when_the_conversation_is_most_of_it_its_newest_messages_are_kept(self, fake_llm):
        """The scratch run: 9,195 characters, almost all of it one long earlier
        answer, came back as 1,142 — the transcript alone cannot reach the floor."""
        fake_llm.set_default("SUMMARY")
        history = [{"role": "user", "content": "old " * 1_500},
                   {"role": "assistant", "content": "old answer " * 600},
                   {"role": "user", "content": "remember: codename ORCA, ticket REL-4411"},
                   {"role": "assistant", "content": "Noted: ORCA, REL-4411. " + "n" * 1_500}]
        ctx = f"User Request: {QUESTION}\n[Turn 1 — Observation] Tool 'ls' returned:\nx\n"
        before = len(ctx) + sum(len(m["content"]) for m in history)

        out, hist, _archive, stats = await _compact(ctx, history, 6_000, pinned_request=QUESTION)

        assert stats["stage"] == "llm_summary"
        assert [m["role"] for m in hist] == ["user", "assistant"], "the newest exchange, whole"
        assert "REL-4411" in hist[1]["content"]
        # Whole messages only: the floor is short by at most the one that did
        # not fit (the 6,600-character older answer here).
        assert stats["chars_after"] >= min(0.2 * before, 3_000) - len(history[1]["content"]), (stats, before)
        assert stats["chars_after"] > len(out), "more than the summary alone"

    async def test_the_summariser_is_told_the_request_is_kept(self, fake_llm):
        fake_llm.set_default("SUMMARY")
        history = [{"role": "user", "content": "u" * 12_000}]
        await _compact(_ctx(3_000), history, 10_000, pinned_request=QUESTION)
        (call,) = fake_llm.calls
        assert "never add" in call["sys_prompt"].lower() or "do not add" in call["sys_prompt"].lower()


class TestStageOne:
    async def test_the_trim_keeps_the_question_at_the_head(self):
        ctx = _ctx(30_000)
        out, hist, archive, stats = await _compact(ctx, [], 10_000, pinned_request=QUESTION)
        assert stats["stage"] == "trim" and archive is None
        assert out.startswith(f"User Request: {QUESTION}\n"), out[:120]
        assert "NEWEST-OBSERVATION" in out
        assert len(out) <= 10_000


class TestALongRequest:
    async def test_it_is_pinned_head_and_tail(self, fake_llm):
        fake_llm.set_default("SUMMARY")
        request = "BEGIN " + "r" * 60_000 + " END"
        ctx = f"User Request: {request}\n" + "o" * 5_000
        history = [{"role": "user", "content": "u" * 30_000}]
        out, *_ = await _compact(ctx, history, 25_000, pinned_request=request)
        head = out.split("[CONTEXT COMPACTED", 1)[0]
        assert head.startswith("User Request: BEGIN ") and " END" in head
        assert len(head) < 21_000


class TestInTheLoop:
    async def test_the_next_model_call_still_has_the_question(self, fake_llm, monkeypatch):
        import core.llm_providers as llm
        import core.react_engine as re_mod

        async def tiny_summary(**_kw):
            return "SUMMARY"

        monkeypatch.setattr(llm, "generate_response", tiny_summary)   # the summariser's own call
        monkeypatch.setattr(re_mod, "load_settings", lambda: {
            "model": "mistral", "auto_compact_enabled": True, "auto_compact_threshold": 6_000})
        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return f"result {args.get('n')} " + "z" * 2_500

        fake_llm.script([tool_call("grep", n=i) for i in range(4)] + ["The codename is ORCA."])
        events = [ev async for ev in re_mod.run_agent_step(
            message=QUESTION, agent_id=agent["id"], session_id="s_f244_loop",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, tool_executor=executor, max_turns=6,
            tools_override=[{"type": "function", "function": {
                "name": "grep", "description": "g",
                "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}],
            history_override=[{"role": "user", "content": "h" * 3_000},
                              {"role": "assistant", "content": "k" * 3_000}])]

        compacted = [i for i, e in enumerate(events) if e.get("type") == "context_compact"]
        assert compacted, "compaction fired mid-turn"
        after = [c for c in fake_llm.calls if "CONTEXT COMPACTED" in c.get("prompt_msg", "")]
        assert after, "a model call ran on the compacted transcript"
        for call in after:
            assert call["prompt_msg"].startswith(f"User Request: {QUESTION}\n"), call["prompt_msg"][:200]
