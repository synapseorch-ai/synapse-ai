"""A gated tool call can be answered when the turn is running on a worker.

The gate itself, the snapshot and the resume have existed since tool approval
landed — but only for a turn running in the API process. `run_agent_chat_job`
drove the loop with `progress_events` off and no `resume_state`, had no branch
for `human_input_required`, and fell through to the ordinary tail. So on the
one path a shared fleet actually uses, a question to a person was answered by:

* an assistant message with no content, written into the conversation,
* the row marked `completed`, and
* nothing anywhere holding the call, so nothing could ever reply to it.

These pin the three halves: the snapshot never reaches the stream, the pause is
parked instead of being written as an answer, and a resume is given the state
that lets the loop skip the tools that already ran.
"""
import asyncio
import json

import pytest

from core.scale import mcp_pool, worker

APPROVAL = {
    "type": "human_input_required",
    "kind": "tool_approval",
    "prompt": "The agent wants to run **run_sql_query**. Allow it?",
    "tool_name": "run_sql_query",
    "tool_args": {"sql": "delete from invoices"},
    "category": "destructive",
    "approval_id": "a1b2c3d4",
}

SNAPSHOT = {
    "type": "_step_progress",
    "turn": 2,
    "context_text": "…the transcript so far…",
    "approved_calls": ["earlier"],
    "tools_used": ["read_file"],
}


class _FakeRedis:
    def __init__(self):
        self.events: list[dict] = []
        self.keys: dict[str, str] = {}

    async def xadd(self, key, fields, **_kw):
        self.events.append(json.loads(fields["data"]))

    async def expire(self, *_a, **_kw):
        return True

    async def set(self, key, value, **_kw):
        self.keys[key] = value

    async def get(self, key):
        return self.keys.get(key)

    async def delete(self, key):
        self.keys.pop(key, None)


@pytest.fixture
def chat_job(monkeypatch):
    """A chat job with everything external stubbed.

    Returns `(upserts, parked)` — what was persisted, and what was parked.
    """
    upserts: list[tuple[str, list]] = []
    parked: list[tuple[str, str, dict | None]] = []
    history = [{"role": "user", "content": "first"},
               {"role": "assistant", "content": "first answer"}]

    async def fake_load_job_settings():
        return None

    async def fake_history(session_factory, session_id, agent_id):
        return list(history)

    async def fake_upsert(session_factory, session_id, agent_id, status, messages):
        upserts.append((status, list(messages)))

    async def fake_set_pending(session_id, agent_id, pending):
        parked.append((session_id, agent_id, pending))

    async def fake_acquire(*_a, **_kw):
        return object()

    async def fake_release(*_a, **_kw):
        return None

    from core.store import sessions as sessions_mod

    monkeypatch.setattr(worker, "_load_job_settings", fake_load_job_settings)
    monkeypatch.setattr(worker, "_load_chat_history", fake_history)
    monkeypatch.setattr(worker, "_upsert_chat_session", fake_upsert)
    monkeypatch.setattr(sessions_mod, "set_pending_approval", fake_set_pending)
    monkeypatch.setattr(mcp_pool, "acquire", fake_acquire)
    monkeypatch.setattr(mcp_pool, "release", fake_release)
    monkeypatch.setattr(worker, "CHAT_CANCEL_POLL_SECONDS", 0.01)
    return upserts, parked


def _patch_loop(monkeypatch, events, seen=None):
    """Patch `run_react_loop` to yield `events`, recording its kwargs."""
    async def fake_loop(request, server_module, should_stop=None, **kwargs):
        if seen is not None:
            seen.append(kwargs)
        for ev in events:
            yield ev
            await asyncio.sleep(0)

    import core.react_engine as re_mod
    monkeypatch.setattr(re_mod, "run_react_loop", fake_loop)
    return fake_loop


class TestAPauseIsNotAnAnswer:
    async def test_the_turn_is_parked_with_its_snapshot(self, chat_job, monkeypatch):
        upserts, parked = chat_job
        _patch_loop(monkeypatch, [SNAPSHOT, APPROVAL])
        ctx = {"redis": _FakeRedis(), "session_factory": object()}

        result = await worker.run_agent_chat_job(ctx, "chat_1", None, "clean up the invoices")

        assert result["status"] == "paused"
        assert len(parked) == 1
        session_id, agent_id, pending = parked[0]
        assert (session_id, agent_id) == ("chat_1", "default")
        assert pending["tool_name"] == "run_sql_query"
        assert pending["approval_id"] == "a1b2c3d4"
        # The question that started the turn, because a resume is rebuilt
        # around it rather than around a transcript.
        assert pending["message"] == "clean up the invoices"
        # And the snapshot, without its event type.
        assert pending["progress"]["turn"] == 2
        assert "type" not in pending["progress"]

    async def test_no_empty_answer_is_written_into_the_conversation(
        self, chat_job, monkeypatch
    ):
        """The bug this whole file exists for.

        The tail appended `{"role": "assistant", "content": response_text}` with
        `response_text` still `""`, and marked the row `completed` — so the
        transcript recorded a reply nobody gave, and the next turn read that
        silence back as context.
        """
        upserts, _ = chat_job
        _patch_loop(monkeypatch, [SNAPSHOT, APPROVAL])
        ctx = {"redis": _FakeRedis(), "session_factory": object()}

        await worker.run_agent_chat_job(ctx, "chat_1", None, "clean up the invoices")

        status, messages = upserts[-1]
        assert status == "paused"
        # Exactly the history it started with: no question, no empty answer.
        assert messages == [{"role": "user", "content": "first"},
                            {"role": "assistant", "content": "first answer"}]

    async def test_the_browser_is_told_and_the_stream_is_closed(
        self, chat_job, monkeypatch
    ):
        """A pause that does not close the stream is a spinner that never stops."""
        _patch_loop(monkeypatch, [SNAPSHOT, APPROVAL])
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        await worker.run_agent_chat_job(ctx, "chat_1", None, "go")

        types = [e.get("type") for e in redis.events]
        assert types[-2:] == ["human_input_required", "done"], types

    async def test_the_snapshot_never_reaches_the_stream(self, chat_job, monkeypatch):
        """`_step_progress` carries a whole turn's context. It is engine state,
        and the browser reads this stream."""
        _patch_loop(monkeypatch, [SNAPSHOT, APPROVAL])
        redis = _FakeRedis()
        ctx = {"redis": redis, "session_factory": object()}

        await worker.run_agent_chat_job(ctx, "chat_1", None, "go")

        assert not [e for e in redis.events if e.get("type") == "_step_progress"]


class TestResuming:
    async def test_the_loop_is_asked_for_snapshots_and_given_the_resume_state(
        self, chat_job, monkeypatch
    ):
        seen: list[dict] = []
        _patch_loop(monkeypatch, [{"type": "response", "response": "done"}], seen=seen)
        ctx = {"redis": _FakeRedis(), "session_factory": object()}
        state = {"turn": 2, "approved_calls": ["a1b2c3d4"]}

        await worker.run_agent_chat_job(ctx, "chat_1", None, "go", resume_state=state)

        assert seen[0]["progress_events"] is True
        assert seen[0]["resume_state"] == state

    async def test_a_resumed_turn_that_finishes_writes_the_pair_once(
        self, chat_job, monkeypatch
    ):
        """The paused turn wrote nothing, so the turn that finishes owns both
        halves — one question, one answer, not two of either."""
        upserts, _ = chat_job
        _patch_loop(monkeypatch, [{"type": "response", "response": "deleted them"}])
        ctx = {"redis": _FakeRedis(), "session_factory": object()}

        result = await worker.run_agent_chat_job(
            ctx, "chat_1", None, "clean up the invoices",
            resume_state={"turn": 2, "approved_calls": ["a1b2c3d4"]},
        )

        assert result["status"] == "completed"
        _, messages = upserts[-1]
        assert messages[-2:] == [
            {"role": "user", "content": "clean up the invoices"},
            {"role": "assistant", "content": "deleted them"},
        ]


class TestAVisitorsTurnOnTheFleet:
    """F280 on a worker: the enqueuer says whose turn it is, as it does for
    `expand_mentions`, and the loop runs in that visitor's scope."""

    async def test_the_enqueuers_visitor_is_the_loops_scope(self, chat_job, monkeypatch):
        from core import visitor

        seen = []

        async def fake_loop(request, server_module, should_stop=None, **kwargs):
            seen.append(visitor.current())
            yield {"type": "final", "response": "ok"}

        import core.react_engine as re_mod
        monkeypatch.setattr(re_mod, "run_react_loop", fake_loop)
        ctx = {"redis": _FakeRedis(), "session_factory": object()}

        await worker.run_agent_chat_job(ctx, "chat_v", None, "hi",
                                        visitor={"surface": "widget", "tools": ["peek"]})
        await worker.run_agent_chat_job(ctx, "chat_m", None, "hi")

        assert seen == [visitor.VisitorScope("widget", frozenset({"peek"})), None]
        assert visitor.current() is None
