"""A chat turn that fails after its tools ran keeps those calls (F426).

Measured on the fleet (C2, `c2team`, nova-lite with `scrape_url`): turn 1 ran
`scrape_url`, then the model's next reply was reasoning only and the turn failed
`model_no_answer` (F305's rule, right). The conversation kept the question and
F133's marker with **`calls: []`**, so turn 2 said "I did not execute the
scrape_url tool in the previous step" — false — and with a tool that acts
(send, write, pay) the person retries and the model, seeing nothing, may do it
twice.

The in-process engine already saved the failed turn's calls
(`_save_failed_turn`); the fleet's chat job wrote its own failed entry without
them and its whole-column upsert replaced the engine's. Now the fleet keeps the
calls (and the question's images) as its completed path does, and the next
turn's digest says the turn failed after them.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.llm_providers import EmptyAnswer

SCRAPE = {"tool": "scrape_url", "args": {"url": "https://example.com"}, "result": "Example Domain"}
AFTER = {"tool": "read_file", "args": {"path": "notes.md"}, "result": "a note"}


def _fleet(monkeypatch, loop, acquire=None):
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod
    import core.react_engine as re_mod
    upserts: list = []

    async def history(*_a):
        return [{"role": "user", "content": "first"}, {"role": "assistant", "content": "first answer"}]

    async def upsert(_sf, _sid, _aid, status, messages):
        upserts.append((status, [dict(m) for m in messages]))

    async def nothing(*_a, **_kw):
        return None

    async def got(*_a, **_kw):
        return object()

    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(worker_mod, "_load_chat_history", history)
    monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
    monkeypatch.setattr(mcp_pool, "acquire", acquire or got)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return worker_mod, upserts


async def _job(worker_mod, **kwargs):
    import fakeredis.aioredis
    redis = fakeredis.aioredis.FakeRedis()
    return await worker_mod.run_agent_chat_job(
        {"redis": redis, "session_factory": None, "job_try": 1, "job_id": "j1"},
        "f426-fleet", "agent_x", "Scrape example.com and tell me its title.", **kwargs)


class TestOnTheFleet:
    async def test_its_failed_turn_keeps_the_calls_that_ran(self, monkeypatch):
        from core.chat_turns import FAILED_ANSWER

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "tool_execution", "tool_name": "scrape_url", "args": SCRAPE["args"]}
            yield {"type": "tool_result", "tool_name": "scrape_url", "preview": SCRAPE["result"]}
            yield {"type": "_step_progress", "turn": 1, "context_text": "x", "calls": [SCRAPE]}
            # One more after the snapshot: it comes from the events.
            yield {"type": "tool_execution", "tool_name": "read_file", "args": AFTER["args"]}
            yield {"type": "tool_result", "tool_name": "read_file", "preview": AFTER["result"]}
            raise EmptyAnswer("Error: The model returned only reasoning, no answer.", reasoning="hmm")

        worker_mod, upserts = _fleet(monkeypatch, loop)
        with pytest.raises(EmptyAnswer):
            await _job(worker_mod)

        status, messages = upserts[-1]
        assert status == "failed"
        question, answer = messages[-2:]
        assert question == {"role": "user", "content": "Scrape example.com and tell me its title."}
        assert answer["content"] == FAILED_ANSWER
        assert answer["calls"] == [SCRAPE, AFTER], "the calls that ran are kept"
        assert answer["tools"] == ["scrape_url: Example Domain", "read_file: a note"]
        assert messages[:2] == [{"role": "user", "content": "first"},
                                {"role": "assistant", "content": "first answer"}]

    async def test_a_failure_with_no_calls_saves_the_marker_alone(self, monkeypatch):
        from core.chat_turns import FAILED_ANSWER

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "status", "message": "working"}
            raise EmptyAnswer("Error: The model returned an empty response.")

        worker_mod, upserts = _fleet(monkeypatch, loop)
        with pytest.raises(EmptyAnswer):
            await _job(worker_mod)
        assert upserts[-1][1][-1] == {"role": "assistant", "content": FAILED_ANSWER}

    async def test_a_failure_before_the_loop_still_saves_the_failed_turn(self, monkeypatch):
        """The calls are read in the failed save, so they must exist before the
        loop does: a failure at the pool's acquire left them unbound, and the
        save's own `except` would have dropped the whole failed row."""
        from core.chat_turns import FAILED_ANSWER

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "final", "response": "never"}

        async def broken(*_a, **_kw):
            raise RuntimeError("the pool is down")

        worker_mod, upserts = _fleet(monkeypatch, loop, acquire=broken)
        with pytest.raises(RuntimeError):
            await _job(worker_mod)
        status, messages = upserts[-1]
        assert status == "failed"
        assert messages[-1] == {"role": "assistant", "content": FAILED_ANSWER}

    async def test_the_failed_question_keeps_its_images(self, monkeypatch):
        """As the completed save and the in-process failed save keep them (F245)."""
        from _fakes.images import data_uri, png

        async def loop(request, server_module, should_stop=None, **_kw):
            raise EmptyAnswer("Error: The model returned an empty response.")
            yield  # pragma: no cover

        worker_mod, upserts = _fleet(monkeypatch, loop)
        with pytest.raises(EmptyAnswer):
            await _job(worker_mod, images=[data_uri(png())])
        question = upserts[-1][1][-2]
        assert question["role"] == "user" and question.get("images"), "the image notes are kept"


class TestInProcess:
    async def test_the_engines_failed_save_keeps_the_calls(self, fake_llm):
        """Pinned: the in-process path already kept them (F133's `_save_failed_turn`)."""
        from core.chat_turns import FAILED_ANSWER
        from core.react_engine import run_agent_step
        from core.session import _get_conversation_history

        async def scrape(name, args):
            return "Example Domain"

        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        fake_llm.script([tool_call("scrape_url", url="https://example.com"),
                         "[REASONING]thinking[/REASONING]", "[REASONING]still[/REASONING]"])
        with pytest.raises(EmptyAnswer):
            async for _ in run_agent_step(
                message="title?", agent_id=agent["id"], session_id="s_f426", agent_override=agent,
                server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
                tool_executor=scrape, max_turns=5, tools_override=[{"type": "function", "function": {
                    "name": "scrape_url", "description": "s",
                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}]):
                pass
        (turn,) = await _get_conversation_history("s_f426", agent["id"])
        assert turn["assistant"] == FAILED_ANSWER
        assert [c["tool"] for c in turn["calls"]] == ["scrape_url"]


class TestTheNextTurn:
    async def _saved(self, session_id, agent_id, answer, calls):
        from core.session import _save_conversation_turn
        await _save_conversation_turn(session_id=session_id, agent_id=agent_id, user="scrape it",
                                      assistant=answer, tools=[f"{c['tool']}: {c['result']}" for c in calls],
                                      calls=calls)

    async def test_its_digest_says_the_turn_failed_after_them(self):
        from core.chat_turns import FAILED_ANSWER
        from core.session import get_recent_history_messages
        await self._saved("s_f426_d", "agent_d", FAILED_ANSWER, [SCRAPE])

        _, answer = await get_recent_history_messages("s_f426_d", agent_id="agent_d", tool_digest=True)
        assert answer["content"].startswith(FAILED_ANSWER)
        assert "failed after these tool calls ran" in answer["content"]
        assert "scrape_url" in answer["content"] and "Example Domain" in answer["content"]

    async def test_a_stopped_turns_digest_says_it_was_stopped(self):
        from core.chat_turns import STOPPED_ANSWER
        from core.session import get_recent_history_messages
        await self._saved("s_f426_s", "agent_s", STOPPED_ANSWER, [SCRAPE])

        _, answer = await get_recent_history_messages("s_f426_s", agent_id="agent_s", tool_digest=True)
        assert "stopped after these tool calls ran" in answer["content"]

    async def test_an_answered_turns_digest_is_unchanged(self):
        from core.session import get_recent_history_messages
        await self._saved("s_f426_a", "agent_a", "The title is Example Domain.", [SCRAPE])

        _, answer = await get_recent_history_messages("s_f426_a", agent_id="agent_a", tool_digest=True)
        assert "[Tool calls you made for this answer" in answer["content"]

    async def test_session_history_lists_them(self):
        from core.chat_turns import FAILED_ANSWER
        from core.session import render_session_history
        await self._saved("s_f426_h", "agent_h", FAILED_ANSWER, [SCRAPE])

        text = await render_session_history("s_f426_h", "agent_h", turn=1)
        assert "Tool calls:\n1. scrape_url" in text and "Example Domain" in text
