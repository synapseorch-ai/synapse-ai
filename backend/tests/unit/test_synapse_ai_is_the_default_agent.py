"""Synapse AI, the product's assistant, is the agent a self-hosted install starts with (ASSIST1, D291, F686).

Naveen: "let our builder be the default agent… no other things needed". The installer's default agent was an all-tools
chat agent named Synapse AI; it is the assistant now (a builder-typed agent: Chat talks to it through the assistant's
own conversation, with its cards and its plan reviews). An install whose old default is exactly as the installer made
it has it turned into the assistant at start-up, under the same id, so its old conversations stay in History. One the
person edited is theirs: it is left as it is.

The assistant is Chat's, not the Agents screen's: it has no prompt or tools to edit there, and a channel, a schedule
or a widget cannot be answered by it, so the agent list the screens and pickers read leaves it out.
"""
from __future__ import annotations

import pytest

from core import default_agent
from core.store.resources import get_agent, save_agent


@pytest.fixture(autouse=True)
async def _store_ready():
    from core.store import get_store
    await get_store()


def test_the_default_agent_is_the_assistant():
    agent = default_agent.DEFAULT_AGENT
    assert agent["id"] == "agent_synapse_ai" and agent["name"] == "Synapse AI"
    assert agent["type"] == "builder" and agent["tools"] == []


async def test_a_new_install_starts_with_it():
    assert await default_agent.ensure_default_agent() is True
    assert (await get_agent("agent_synapse_ai"))["type"] == "builder"
    assert await default_agent.ensure_default_agent() is False


async def test_an_untouched_old_default_becomes_the_assistant():
    await save_agent(dict(default_agent.LEGACY_DEFAULT_AGENT))
    assert await default_agent.adopt_the_assistant() is True
    agent = await get_agent("agent_synapse_ai")
    assert agent["type"] == "builder" and agent["tools"] == [] and agent["name"] == "Synapse AI"
    assert await default_agent.adopt_the_assistant() is False  # once


async def test_an_edited_old_default_is_the_persons_own():
    mine = {**default_agent.LEGACY_DEFAULT_AGENT, "system_prompt": "You answer in French."}
    await save_agent(mine)
    assert await default_agent.adopt_the_assistant() is False
    agent = await get_agent("agent_synapse_ai")
    assert agent["type"] == "conversational" and agent["system_prompt"] == "You answer in French."


async def test_chat_lists_it_and_the_agent_screens_do_not(client):
    await default_agent.ensure_default_agent()
    listed = (await client.get("/api/agents")).json()
    assert "agent_synapse_ai" not in {a["id"] for a in listed}
    status = (await client.get("/api/status")).json()
    assert status["agents"]["agent_synapse_ai"]["type"] == "builder"


async def test_status_names_the_agent_chat_answers_with_before_anyone_picks_one(client, monkeypatch):
    """Found in ASSIST1's browser pass: with nothing picked, status said `active_agent_id: null`, so Chat could not
    tell it was talking to Synapse AI and drew a plain chat under the settings' name. It names the agent the backend
    answers with (`get_active_agent_data`): the one picked, else the workspace's first."""
    from core.routes import agents

    monkeypatch.setattr(agents, "active_agent_id", None)
    await default_agent.ensure_default_agent()
    status = (await client.get("/api/status")).json()
    assert status["active_agent_id"] == "agent_synapse_ai"
