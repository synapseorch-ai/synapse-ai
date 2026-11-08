"""A chat turn's warning is kept with its answer, so a conversation read later says it (F584).

F582 (a) made a turn that ends still holding a board claim say so — a `step_warning` before its `final` — but only
the run page drew it: the chat page dropped the event, and neither save kept it. The in-process save ran before the
warning was even made, and the fleet's chat job, which saves the turn itself, never looked. A cloud schedule's firing
is read in its conversation afterwards, so a card left claimed was never said there at all.

Now every warning the turn itself raises (not a workflow step's, not a delegate's) is a note of the turn: on the saved
answer as `notes`, read back with the turn, and on `final` for a caller that saves. The model never reads it back:
history is built from the answer's words.
"""
from __future__ import annotations

import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.knowledge_env import use_saved_settings
from core.store import boards as boards_store
from core.store import sessions as sessions_store

PERSON = boards_store.Actor(person="priya@example.com", source="person")


@pytest.fixture(autouse=True)
async def _store_ready():
    from core.store import get_store
    await get_store()


@pytest.fixture
async def boards_on(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings
    await save_settings({"boards_enabled": True})


@pytest.fixture
async def desk():
    await S.seed_agents([S.make_agent(id="desk", name="Desk", tools=[], skip_default_tools=True, max_turns=6)])
    board = await boards_store.create_shared_board("Bugs", by=PERSON)
    await boards_store.set_access(board.id, [("agent", "desk", "work")], by=PERSON)
    card, _ = await boards_store.add_card(board.id, title="Fix login", key="BUG-584", by=PERSON)
    return card


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


async def _chat(session_id, **kw):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop

    return [e async for e in run_react_loop(ChatRequest(message="go", session_id=session_id, agent_id="desk"),
                                            _server(), **kw)]


async def _saved(session_id):
    from core.session import _get_conversation_history
    return (await _get_conversation_history(session_id, "desk"))[-1]


class TestInProcess:
    async def test_the_saved_answer_keeps_the_warning(self, boards_on, fake_llm, desk):
        fake_llm.script([tool_call("board_claim", card_id=desk.id), "I closed BUG-584"])
        events = await _chat("s_f584_kept")
        [warning] = [e["message"] for e in events if e.get("type") == "step_warning"]
        assert desk.id in warning and "without closing it" in warning
        turn = await _saved("s_f584_kept")
        assert turn["assistant"] == "I closed BUG-584"
        assert turn["notes"] == [warning]

    async def test_it_is_still_said_before_the_answer(self, boards_on, fake_llm, desk):
        fake_llm.script([tool_call("board_claim", card_id=desk.id), "done"])
        kinds = [e.get("type") for e in await _chat("s_f584_order")]
        assert kinds.index("step_warning") < kinds.index("final")

    async def test_a_turn_that_closes_its_card_keeps_no_note(self, boards_on, fake_llm, desk):
        fake_llm.script([tool_call("board_claim", card_id=desk.id),
                         tool_call("board_update", card_id=desk.id, version=1, status="done", result="Fixed"),
                         "Closed BUG-584"])
        await _chat("s_f584_closed")
        assert "notes" not in await _saved("s_f584_closed")

    async def test_a_caller_that_saves_gets_it_on_final(self, boards_on, fake_llm, desk):
        fake_llm.script([tool_call("board_claim", card_id=desk.id), "done"])
        events = await _chat("s_f584_caller", save_here=False)
        [warning] = [e["message"] for e in events if e.get("type") == "step_warning"]
        [final] = [e for e in events if e.get("type") == "final"]
        assert final["notes"] == [warning]

    async def test_the_model_never_reads_it_back(self, boards_on, fake_llm, desk):
        from core.session import get_recent_history_messages

        fake_llm.script([tool_call("board_claim", card_id=desk.id), "done"])
        await _chat("s_f584_history")
        history = await get_recent_history_messages("s_f584_history", "desk", tool_digest=True)
        assert not any("without closing" in m["content"] for m in history)


@pytest.fixture
def fleet(fake_redis, monkeypatch):
    """The fleet's chat job, in this process, on `core.server` as its tenant's session set."""
    import core.server as server
    from core.scale import mcp_pool, worker
    from core.store import get_store

    async def acquire():
        return server

    async def nothing():
        return None

    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(worker, "_load_job_settings", nothing)
    monkeypatch.setattr(server, "agent_sessions", {"x": object()}, raising=False)
    monkeypatch.setattr(server, "tool_router", {}, raising=False)

    async def run(session_id, message):
        ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1,
               "job_id": f"job-{session_id}"}
        return await worker.run_agent_chat_job(ctx, session_id, "desk", message)

    return run


class TestOnTheFleet:
    async def test_the_jobs_saved_answer_keeps_the_warning(self, boards_on, fake_llm, desk, fleet):
        fake_llm.script([tool_call("board_claim", card_id=desk.id), "I closed BUG-584"])
        assert (await fleet("s_f584_fleet", "go"))["status"] == "completed"
        answer = ((await sessions_store.get("s_f584_fleet", "desk")) or {})["messages"][-1]
        assert answer["content"] == "I closed BUG-584"
        assert len(answer["notes"]) == 1 and desk.id in answer["notes"][0]

    async def test_a_closed_card_leaves_none(self, boards_on, fake_llm, desk, fleet):
        fake_llm.script([tool_call("board_claim", card_id=desk.id),
                         tool_call("board_update", card_id=desk.id, version=1, status="done", result="Fixed"),
                         "Closed BUG-584"])
        assert (await fleet("s_f584_fleet_closed", "go"))["status"] == "completed"
        answer = ((await sessions_store.get("s_f584_fleet_closed", "desk")) or {})["messages"][-1]
        assert "notes" not in answer
