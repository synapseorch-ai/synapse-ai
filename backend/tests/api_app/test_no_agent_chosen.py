"""
With no agent chosen, chat answers as the workspace's own first agent — never
as one of the AI builder's (F186).

`get_active_agent_data()` fell back to `agents[0]` of `load_user_agents()`,
which holds the builder's `system` sub-agents too, and they are seeded at first
boot, so they sort first. On a fresh install — and after every backend restart,
because the selection is module memory — a chat message with no agent ran the
builder's Agent Creator, which holds `create_agent(s)`, `create_http_tool`,
`import_openapi` and `create_skill`, while the page's header said "Synapse".
`GET /api/agents` already hid them; the fallback, `/api/status` and
`POST /api/agents/active` did not. v1 and the CLI skip them since F69.
"""
import pytest

from _fakes.fake_redis_stream import data_events
from tests._fakes import seed as _seed

CREATOR = "agent_native_builder_agent_creator"
MINE = "agent_zz_mine"


def _creator():
    return _seed.make_agent(id=CREATOR, name="Agent Creator", system="native_builder",
                            skip_default_tools=True)


def _mine():
    return _seed.make_agent(id=MINE, name="Mine", skip_default_tools=True)


@pytest.fixture(autouse=True)
def _nothing_selected(monkeypatch):
    """What a fresh install, or any restart, starts with."""
    import core.routes.agents as agents
    monkeypatch.setattr(agents, "active_agent_id", None)


async def test_the_fallback_skips_the_builders_agents():
    from core.routes.agents import get_active_agent_data, load_user_agents

    await _seed.seed_agents([_creator(), _mine()])
    # The order that made the Agent Creator the default.
    assert (await load_user_agents())[0]["id"] == CREATOR

    assert (await get_active_agent_data())["id"] == MINE


async def test_a_selection_naming_a_builder_agent_is_not_honoured(monkeypatch):
    import core.routes.agents as agents

    await _seed.seed_agents([_creator(), _mine()])
    monkeypatch.setattr(agents, "active_agent_id", CREATOR)

    assert (await agents.get_active_agent_data())["id"] == MINE


async def test_with_only_the_builders_agents_the_answer_says_what_to_do():
    from core.routes.agents import get_active_agent_data

    await _seed.seed_agents([_creator()])

    with pytest.raises(RuntimeError, match="create one in Agents"):
        await get_active_agent_data()


async def test_a_builder_agent_cannot_be_made_active(client):
    await _seed.seed_agents([_creator(), _mine()])

    resp = await client.post("/api/agents/active", json={"agent_id": CREATOR})
    assert resp.status_code == 404

    resp = await client.post("/api/agents/active", json={"agent_id": MINE})
    assert resp.status_code == 200


async def test_status_lists_only_the_workspaces_agents(client):
    await _seed.seed_agents([_creator(), _mine()])

    resp = await client.get("/api/status")

    assert resp.status_code == 200
    assert set(resp.json()["agents"]) == {MINE}


async def test_a_chat_with_no_agent_is_answered_by_the_workspaces_agent(
    client, monkeypatch, fake_llm
):
    import core.server as server

    await _seed.seed_agents([_creator(), _mine()])
    monkeypatch.setitem(server._session_tools, "_test", [])
    fake_llm.set_default("Hello from mine.")

    resp = await client.post("/chat", json={"message": "hi"})

    assert resp.status_code == 200
    assert resp.json()["response"] == "Hello from mine."
    assert fake_llm.last_call.get("agent_id") == MINE


async def test_a_chat_with_no_agent_to_answer_says_so(client, monkeypatch, fake_llm):
    import core.server as server

    await _seed.seed_agents([_creator()])
    monkeypatch.setitem(server._session_tools, "_test", [])
    fake_llm.set_default("The Agent Creator should not be answering.")

    resp = await client.post("/chat/stream", json={"message": "hi", "session_id": "s_f186"})

    errors = [e for e in data_events(resp.text.split("\n\n")) if e.get("type") == "error"]
    assert errors and "No agent to answer yet" in errors[0]["message"]
    assert fake_llm.call_count == 0
