"""
A tool call can ask permission inside a conversation.

Tool policies gave the engine a real gate: a call classed `destructive` or
`open_world` can stop the step and ask a person. Only orchestration runs could
answer it, because a run resumes from its own row — and a chat turn has no run.
So in chat the gate was a dead end: the turn stopped with a question, and
nothing in the product could reply to it.

The part worth pinning is *how* it resumes. Starting the turn again would
re-run every tool that already succeeded before the pause, and those are
precisely the calls with side effects — so the turn is picked up from its last
turn-boundary snapshot instead.
"""
import pytest

from core.routes import chat as chat_routes
from core.routes.chat import ApprovalDecision
from core.store import sessions as store


def _pending(**over):
    base = {
        "tool_name": "run_sql_query",
        "tool_args": {"query": "DROP TABLE users"},
        "approval_id": "abc123",
        "category": "destructive",
        "message": "clean up the database",
        "progress": {
            "turn": 3,
            "context_text": "three tool results already happened",
            "tools_used": ["read_file", "grep"],
        },
    }
    base.update(over)
    return base


@pytest.fixture
def captured(monkeypatch):
    """Swap the streaming half out; keep the decision-shaping half real."""
    seen = {}

    def fake_guarded(request, resume_state=None, *, expand_mentions):
        seen["request"] = request
        seen["resume_state"] = resume_state
        # A resume is the member's own turn picked up again: its `@[path]`
        # mentions read the vault as the first turn's did.
        assert expand_mentions is True
        async def _empty():
            if False:
                yield b""
        return _empty()

    monkeypatch.setattr(chat_routes, "_guarded_stream", fake_guarded)
    return seen


class TestParkingThePause:
    async def test_it_round_trips(self):
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())
        got = await store.get_pending_approval("s1", "default")
        assert got["tool_name"] == "run_sql_query"
        assert got["progress"]["turn"] == 3

    async def test_clearing_it_is_how_a_question_stops_being_open(self):
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())
        await store.set_pending_approval("s1", "default", None)
        assert await store.get_pending_approval("s1", "default") is None


    async def test_a_pause_on_the_very_first_turn_still_lands(self):
        """The row is written on the first *completed* turn, so a conversation
        that stops to ask permission before it has ever finished one had
        nothing to hang the question on — and answering it said there was no
        question."""
        await store.set_pending_approval("brand_new", "default", _pending())
        got = await store.get_pending_approval("brand_new", "default")
        assert got is not None and got["approval_id"] == "abc123"

    async def test_clearing_a_session_that_does_not_exist_creates_nothing(self):
        await store.set_pending_approval("never_seen", "default", None)
        assert await store.get_pending_approval("never_seen", "default") is None


class TestAnswering:
    async def test_an_approval_carries_the_snapshot_forward(self, captured):
        """The point of keeping it: the turn continues rather than restarting,
        so the tools that already ran do not run again."""
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())

        await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))

        rs = captured["resume_state"]
        assert rs["turn"] == 3
        assert rs["context_text"] == "three tool results already happened"
        assert rs["tools_used"] == ["read_file", "grep"]

    async def test_an_approval_is_spent_on_that_call(self, captured):
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())

        await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))

        rs = captured["resume_state"]
        assert rs["approved_calls"] == ["abc123"]
        assert rs["pending_call"] == {
            "approval_id": "abc123",
            "tool": "run_sql_query",
            "arguments": {"query": "DROP TABLE users"},
            "approved": True,
            "reason": "",
        }

    async def test_a_refusal_reaches_the_model_as_a_reason(self, captured):
        """A refusal the model can read and work around, rather than a wall."""
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())

        await chat_routes.approve_chat_tool(
            "s1", ApprovalDecision(approve=False, reason="not on the production database"))

        rs = captured["resume_state"]
        assert rs["denied_calls"] == ["abc123"]
        assert "approved_calls" not in rs
        assert rs["pending_call"]["approved"] is False
        assert rs["denied_reasons"]["abc123"] == "not on the production database"

    async def test_the_original_question_is_what_the_turn_resumes_around(self, captured):
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())

        await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))

        assert captured["request"].message == "clean up the database"
        assert captured["request"].session_id == "s1"

    async def test_the_question_is_closed_before_the_turn_runs(self, captured):
        """Cleared first, not last: a resume that crashed with the row still
        set would re-open a question the person has already answered."""
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())

        await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))

        assert await store.get_pending_approval("s1", "default") is None

    async def test_answering_a_question_nobody_asked_is_a_conflict(self, captured):
        from fastapi import HTTPException
        await store.append_turn("s1", "default", [])
        with pytest.raises(HTTPException) as exc:
            await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))
        assert exc.value.status_code == 409

    async def test_answering_twice_is_a_conflict(self, captured):
        """The second click of a double-click must not run the call again."""
        from fastapi import HTTPException
        await store.append_turn("s1", "default", [])
        await store.set_pending_approval("s1", "default", _pending())

        await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))
        with pytest.raises(HTTPException) as exc:
            await chat_routes.approve_chat_tool("s1", ApprovalDecision(approve=True))
        assert exc.value.status_code == 409
