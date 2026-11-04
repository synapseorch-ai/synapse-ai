"""A cost answer starts from who spends (F1311, REACH1; prod-08's first answer on FF16 63dfb505, 79 11-03).

"How can I cut my costs?" was answered with screen how-to: the turn went to the help answer, which could not read the
workspace, and on the follow-up the savings topic said there was nothing to save, since Greeter's $0.36 over 30 days
is under the floor a saving must clear. Only usage named Greeter. These pin the data a cost answer starts from:

- the savings topic always carries `top_spenders`: the agents that spent most in its window, each with its model,
  what it spent and what that is a month, and the workflows whose runs cost most; whether or not a saving clears
  the floor, and never Synapse AI's own agents;
- a workspace that spent nothing says so, with no spenders made up.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.builder_tools import execute_builder_tool_with_events


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _row(**values):
    from core.store import usage as store

    stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await store.append(store.record_values({"timestamp": stamp, "provider": "openai", "input_tokens": 1000,
                                            "output_tokens": 100, "context_chars": 0, **values}))


@pytest.fixture
async def prod_08():
    """prod-08's workspace: Greeter's 40 chat calls on gpt-4.1, a cheaper agent, and Synapse AI's own calls."""
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    await save_agent(make_agent(id="agent_greeter", name="Greeter", model="gpt-4.1"))
    await save_agent(make_agent(id="agent_digest", name="Digest", model="gpt-4.1-mini"))
    await save_agent(make_agent(id="agent_native_builder_reporter", name="Reporter", system=True))
    for _ in range(40):
        await _row(model="gpt-4.1", estimated_cost=0.009, agent_id="agent_greeter", source="chat")
    for _ in range(3):
        await _row(model="gpt-4.1-mini", estimated_cost=0.001, agent_id="agent_digest", source="chat")
    for _ in range(10):
        await _row(model="claude-haiku-4-5", estimated_cost=0.004, agent_id="agent_native_builder_reporter",
                   source="chat")


async def _savings(days: int = 30) -> dict:
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "savings", "days": days}, _Server())
    return json.loads(raw)


class TestTopSpenders:
    async def test_the_costliest_agent_is_named_below_the_floor(self, client, prod_08):
        got = await _savings(30)
        top = got["top_spenders"]["agents"]
        assert top[0] == {"agent": "Greeter", "model": "gpt-4.1", "spent_usd": pytest.approx(0.36),
                          "usd_month": pytest.approx(0.36), "calls": 40}
        assert [a["agent"] for a in top] == ["Greeter", "Digest"], "costliest first, Synapse AI's own left out"
        assert got["items"] == [], "no saving clears the floor here: the spenders are said all the same"
        assert "top_spenders" in got["note"]

    async def test_nothing_spent_is_said_with_no_spenders(self, client):
        got = await _savings(30)
        assert got["top_spenders"] == {"agents": [], "workflows": []}
