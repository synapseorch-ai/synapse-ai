"""A schedule's board in a turn the chat loop runs (BOARD1b, D282; MEMORY 20 §5, C2/C5).

Cloud runs a scheduled agent as the fleet's chat job (`run_agent_chat_job` → `run_react_loop`), not through the
scheduler's `run_agent_step` call, so the scope reaches the turn through the loop: `board_scope` is passed down to
`run_agent_step`, and the turn's claims are released under the scope's holder (the firing), not `session:<sid>`.
A parked turn keeps them; a turn with no scope is a plain chat turn, shared boards only, as before.
"""
from __future__ import annotations

import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.knowledge_env import use_saved_settings
from core.store import boards as store

PERSON = store.Actor(person="priya@example.com", source="person")


@pytest.fixture(autouse=True)
async def _store_ready():
    from core.store import get_store
    await get_store()


@pytest.fixture
async def boards_on(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings
    await save_settings({"boards_enabled": True})


def _agent(**kw):
    kw.setdefault("tools", [])
    kw.setdefault("skip_default_tools", True)
    kw.setdefault("max_turns", 6)
    return S.make_agent(**kw)


def _scope(schedule_id="sch_bugs", holder="schedc_firing2"):
    return {"kind": "schedule", "id": schedule_id, "holder": holder}


async def _loop(fake_llm, script, *, session_id="schedc_firing2", agent_id="nightly", **kw):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop

    fake_llm.script(script)
    server = types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})
    return [e async for e in run_react_loop(ChatRequest(message="work the board", session_id=session_id,
                                                        agent_id=agent_id),
                                            server, history_override=[], save_turn=False, **kw)]


def _offered(call) -> set[str]:
    return {t["function"]["name"] for t in (call.get("tools") or [])}


class TestAScheduledTurnOnTheChatLoop:
    async def test_it_reads_yesterdays_card_first(self, boards_on, fake_llm):
        await S.seed_agents([_agent(id="nightly")])
        board = await store.get_or_create_scoped_board("schedule", "sch_bugs")
        await store.add_card(board.id, title="Retry the Fenwick import", by=PERSON)
        await _loop(fake_llm, ["read it"], board_scope=_scope())
        first = fake_llm.calls[0]
        assert "Retry the Fenwick import" in str(first["prompt_msg"])
        assert "board_claim" in _offered(first)

    async def test_its_claim_is_the_firings_and_goes_when_it_ends(self, boards_on, fake_llm):
        await S.seed_agents([_agent(id="nightly")])
        board = await store.get_or_create_scoped_board("schedule", "sch_bugs")
        card, _ = await store.add_card(board.id, title="Clean rows", by=PERSON)
        # The firing's key is not the conversation's: the release must name the firing.
        await _loop(fake_llm, [tool_call("board_claim", card_id=card.id), "done"], session_id="schedc_conv",
                    board_scope=_scope(holder="firing_7"))
        assert "Claimed" in str(fake_llm.calls[1]["prompt_msg"])
        assert (await store.get_card(card.id)).status == "open"
        assert [e.kind for e in await store.history(card.id)][:2] == ["released", "claimed"]

    async def test_a_parked_turn_keeps_its_claim_under_the_firing(self, boards_on, fake_llm):
        await S.seed_agents([_agent(id="nightly", tools=["danger"], tool_policy={"undeclared": "confirm"})])
        board = await store.get_or_create_scoped_board("schedule", "sch_bugs")
        card, _ = await store.add_card(board.id, title="Clean rows", by=PERSON)
        events = await _loop(fake_llm, [tool_call("board_claim", card_id=card.id), tool_call("danger", rows="old")],
                             board_scope=_scope())
        assert any(e["type"] == "human_input_required" for e in events)
        held = await store.get_card(card.id)
        assert held.status == "claimed" and held.holder_key == "schedc_firing2"

    async def test_with_no_scope_it_is_a_chat_turn_with_shared_boards_only(self, boards_on, fake_llm):
        await S.seed_agents([_agent(id="nightly")])
        board = await store.get_or_create_scoped_board("schedule", "sch_bugs")
        await store.add_card(board.id, title="Retry the Fenwick import", by=PERSON)
        await _loop(fake_llm, ["nothing to do"])
        first = fake_llm.calls[0]
        assert "Retry the Fenwick import" not in str(first["prompt_msg"])
        assert not (_offered(first) & {"board_claim", "board_read", "board_add"})


class TestTheFleetChatJobPassesTheScope:
    async def test_the_scope_reaches_the_loop(self, monkeypatch):
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        import core.react_engine as re_mod

        seen: dict = {}

        async def nothing(*_a, **_kw):
            return None

        async def history(*_a):
            return []

        async def acquire(*_a, **_kw):
            return object()

        async def loop(request, server_module, should_stop=None, **kw):
            seen.update(kw)
            yield {"type": "final", "response": "ok"}

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", nothing)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)
        import fakeredis.aioredis
        ctx = {"redis": fakeredis.aioredis.FakeRedis(), "session_factory": None, "job_try": 1, "job_id": "j1"}
        await worker_mod.run_agent_chat_job(ctx, "schedc_f1", "nightly", "go", board_scope=_scope(holder="schedc_f1"))
        assert seen["board_scope"] == _scope(holder="schedc_f1")
        seen.clear()
        await worker_mod.run_agent_chat_job(ctx, "chat_m1", "nightly", "hi")
        assert seen["board_scope"] is None
