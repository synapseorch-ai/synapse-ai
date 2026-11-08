"""A plain agent's turn on the fleet is saved once, by the job (F475, VOX1f, D273).

The fleet's chat job saves the turn itself (`_save_row`), every ending of it, and
the loop saved it too. The job's whole-list write used to replace the loop's copy;
now that a save appends (so a turn another writer saved meanwhile is kept), the
loop leaves the turn to the job (`run_react_loop(save_here=False)`), or the
conversation would hold the pair twice. The workflow agent's twin is
`test_chat_turn_routes.py::test_the_fleet_keeps_one_pair_and_the_same_answer`.
"""
import types

from core.store import sessions as store


async def test_a_plain_agents_fleet_turn_is_one_pair(fake_redis, fake_llm, seed_agent, monkeypatch):
    from core.scale import mcp_pool, worker
    from core.store import get_store

    import core.server as server_mod
    agent = await seed_agent(tools=[], skip_default_tools=True)
    monkeypatch.setitem(server_mod._session_tools, "_test", [])
    server = types.SimpleNamespace(agent_sessions={"_test": object()}, memory_store=None, tool_router={})

    async def acquire():
        return server

    async def nothing():
        return None
    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(worker, "_load_job_settings", nothing)
    fake_llm.script(["It is sunny."])

    ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1, "job_id": "f475-once"}
    result = await worker.run_agent_chat_job(ctx, "f475-once", agent["id"], "Weather?")

    assert result["status"] == "completed"
    row = await store.get("f475-once", agent["id"])
    assert [(m["role"], m["content"]) for m in row["messages"]] == [("user", "Weather?"),
                                                                    ("assistant", "It is sunny.")]
