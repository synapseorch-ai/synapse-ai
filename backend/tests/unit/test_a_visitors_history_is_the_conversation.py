"""A widget visitor's history is the conversation and nothing more (F325).

`GET /api/widget/history` returned each saved turn whole: the tools' calls
(their arguments and up to 2,000 characters of each result), the model's
reasoning and thoughts, and the older `tools` summary. The widget draws only
what was said, and its stream never lets any of the rest out (`_VISITOR_EVENTS`),
but the reload handed all of it to anyone holding the visitor's token. Cloud's
widget history already answers role and content only; so does this one now.
"""
from __future__ import annotations

import json

from core.chat_widgets import limits as _limits


async def _visitor(client, seed_agent):
    _limits.reset()
    agent = await seed_agent()
    widget = (await client.post("/api/widgets", json={
        "name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"]})).json()
    token = (await client.post("/api/widget/session", json={
        "public_key": widget["public_key"], "origin": "https://acme.com"})).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    session_id = (await client.get("/api/widget/history", headers=headers)).json()["session_id"]
    return agent, headers, session_id


async def test_history_holds_what_was_said_and_nothing_else(client, seed_agent):
    from core.session import _save_conversation_turn

    agent, headers, session_id = await _visitor(client, seed_agent)
    await _save_conversation_turn(
        session_id=session_id, agent_id=agent["id"], user="what do you charge?", assistant="Hosting is 1,200.",
        tools=["run_sql_query: SECRET SUMMARY"],
        calls=[{"tool": "run_sql_query", "args": {"q": "SELECT * FROM SECRET_TABLE"}, "result": "SECRET ROWS"}],
        reasoning=["SECRET REASONING"], thoughts=["SECRET THOUGHT"],
        images=[{"type": "image/png"}])
    body = (await client.get("/api/widget/history", headers=headers)).json()
    assert body["messages"] == [{"role": "user", "content": "what do you charge?"},
                                {"role": "assistant", "content": "Hosting is 1,200."}]
    assert "SECRET" not in json.dumps(body)
    _limits.reset()


async def test_a_turn_still_running_is_unchanged(client, seed_agent):
    _, headers, session_id = await _visitor(client, seed_agent)
    body = (await client.get("/api/widget/history", headers=headers)).json()
    assert body == {"session_id": session_id, "messages": [], "running": None}
    _limits.reset()
