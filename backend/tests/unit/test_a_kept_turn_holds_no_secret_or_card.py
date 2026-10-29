"""A chat turn is kept with no held secret and no payment card's number in it, on every save path (F1184, BRW3 D331;
Naveen's decision 2026-10-29).

The model reads the turn as it was sent; the stored copy is what changes. A secret variable's value is kept by its
name (F854's mark), and a number that passes the engine's own card test (`gate.looks_like_card`) as "[card number]".
Ids, times and cursors are never words, so they are left alone; a number that is not a card's is kept. Masking what
was masked already changes nothing, so a turn saved twice is the same turn.

Every save path: the in-process turn (`store.append_turn`), the fleet's chat job, whose loop runs in a task of its
own (so what that task holds is what the job's saves must hold), and a turn parked on a tool approval.
"""
from __future__ import annotations

import asyncio
import json
import types

from _fakes import canary
from _fakes import seed as S

canary_secret = canary.canary_secret
CANARY, MARK = canary.CANARY, canary.MARK
_left_behind = canary.left_behind

CARD = "4111 1111 1111 1111"  # passes Luhn
NOT_A_CARD = "4111 1111 1111 1112"  # does not
SAID = f"Charge {CARD} for order {NOT_A_CARD}; the key is {CANARY}."


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


async def _agent(agent_id: str) -> dict:
    agent = S.make_agent(id=agent_id, name="Billing", tools=["all"], skip_default_tools=True,
                         system_prompt="You bill with key {{ global.API_TOKEN }}.")
    await S.seed_agents([agent])
    return agent


def _kept_right(messages: list) -> None:
    kept = json.dumps(messages)
    assert CANARY not in kept and CARD not in kept
    assert MARK in kept and "[card number]" in kept
    assert NOT_A_CARD in kept, "a number that is not a card's is kept"


def test_the_stored_copy_masks_cards_and_held_secrets_and_is_stable():
    from core.secret_scrub import for_storage

    turn = [{"role": "user", "content": SAID, "chat_turn": "4111111111111111"},
            {"role": "assistant", "content": "Charged.", "timestamp": "2026-10-29T09:00:00",
             "calls": [{"tool": "charge", "args": {"card": CARD}, "result": f"charged {CARD}"}]}]
    kept = for_storage(turn, held={"API_TOKEN": CANARY})

    _kept_right(kept)
    assert kept[1]["calls"][0] == {"tool": "charge", "args": {"card": "[card number]"},
                                   "result": "charged [card number]"}
    assert kept[0]["chat_turn"] == "4111111111111111", "an id is not words"
    assert for_storage(kept, held={"API_TOKEN": CANARY}) == kept, "a second save changes nothing"
    assert turn[0]["content"] == SAID, "the turn as sent is untouched"


async def test_in_process_the_model_reads_it_as_sent_and_the_store_keeps_it_masked(
        canary_secret, fake_llm, tmp_path):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop
    from core.store import sessions as store

    agent = await _agent("agent_kept")
    fake_llm.set_default("Charged.")
    [e async for e in run_react_loop(ChatRequest(message=SAID, session_id="chat_kept", agent_id=agent["id"]),
                                     _server())]

    assert CARD in json.dumps(fake_llm.calls[0], default=str), "the model read the turn as sent"
    _kept_right((await store.get("chat_kept", agent["id"]))["messages"])
    assert _left_behind(tmp_path) == [], _left_behind(tmp_path)


def _fleet(monkeypatch, server):
    from core.scale import mcp_pool, worker

    async def acquire():
        return server

    async def nothing():
        return None
    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(worker, "_load_job_settings", nothing)
    return worker


async def test_on_the_fleet_the_model_reads_it_as_sent_and_the_store_keeps_it_masked(
        canary_secret, fake_llm, fake_redis, tmp_path, monkeypatch):
    from core.store import get_store
    from core.store import sessions as store

    agent = await _agent("agent_kept_fleet")
    fake_llm.set_default("Charged.")
    worker = _fleet(monkeypatch, _server())
    ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1, "job_id": "chat_kept_fleet"}
    await worker.run_agent_chat_job(ctx, "chat_kept_fleet", agent["id"], SAID)

    assert CARD in json.dumps(fake_llm.calls[0], default=str), "the model read the turn as sent"
    _kept_right((await store.get("chat_kept_fleet", agent["id"]))["messages"])
    assert _left_behind(tmp_path) == [], _left_behind(tmp_path)


async def test_on_the_fleet_a_turn_parked_on_an_approval_keeps_its_question_masked(
        fake_redis, monkeypatch):
    """The loop holds the secret in its own task, as `variables.load` does; the job parks the question after it."""
    import core.react_engine as re_mod
    from core.secret_scrub import hold_secrets
    from core.store import get_store
    from core.store import sessions as store

    async def loop(request, server_module, should_stop=None, **kwargs):
        hold_secrets({"API_TOKEN": CANARY})
        yield {"type": "_step_progress", "turn": 1, "context_text": "…", "approved_calls": [], "tools_used": []}
        yield {"type": "human_input_required", "kind": "tool_approval", "prompt": "Allow charge?",
               "tool_name": "charge", "tool_args": {"amount": 5}, "category": "external", "approval_id": "ap1"}
        await asyncio.sleep(0)

    monkeypatch.setattr(re_mod, "run_react_loop", loop)
    worker = _fleet(monkeypatch, _server())
    ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1, "job_id": "chat_parked_kept"}
    result = await worker.run_agent_chat_job(ctx, "chat_parked_kept", None, SAID)

    assert result["status"] == "paused"
    pending = await store.get_pending_approval("chat_parked_kept", "default")
    assert CANARY not in pending["message"] and CARD not in pending["message"]
    assert MARK in pending["message"] and "[card number]" in pending["message"]
