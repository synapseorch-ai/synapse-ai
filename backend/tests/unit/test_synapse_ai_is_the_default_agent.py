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


# F1412: the installer was the only thing that made Synapse AI, and a Docker install (its entrypoint runs no setup)
# or a pip install (`synapse start`'s wizard writes settings only) never runs it. So the server's first start makes
# it, once: a marker in the store says it did, so an agent the person deleted stays deleted.

_AS_MADE = ("id", "name", "description", "type", "tools", "repos", "system_prompt")


async def test_a_first_start_that_no_installer_ran_makes_it():
    assert await default_agent.create_the_assistant_once() is True
    agent = await get_agent("agent_synapse_ai")
    assert {k: agent.get(k) for k in _AS_MADE} == {k: default_agent.DEFAULT_AGENT[k] for k in _AS_MADE}
    assert await default_agent.create_the_assistant_once() is False


async def test_deleted_it_stays_deleted():
    from core.store.resources import delete_agent

    await default_agent.create_the_assistant_once()
    assert await delete_agent("agent_synapse_ai") is True
    assert await default_agent.create_the_assistant_once() is False
    assert await get_agent("agent_synapse_ai") is None


async def test_one_the_installer_made_is_only_marked_and_an_edit_stays():
    from core.store.resources import delete_agent

    await default_agent.ensure_default_agent()  # `synapse setup --apply`, before the first start
    await save_agent({**await get_agent("agent_synapse_ai"), "description": "Mine now."})
    assert await default_agent.create_the_assistant_once() is False
    assert (await get_agent("agent_synapse_ai"))["description"] == "Mine now."
    await delete_agent("agent_synapse_ai")
    assert await default_agent.create_the_assistant_once() is False  # marked on that first start
    assert await get_agent("agent_synapse_ai") is None


async def test_a_1x_docker_volume_brought_across_gets_it(tmp_path):
    """1.9's image never ran setup.py either, so its /data holds the person's agents and no Synapse AI."""
    import json

    from core.store.importer import import_data_dir

    (tmp_path / "user_agents.json").write_text(json.dumps([{"id": "agent_mine", "name": "Mine"}]))
    await import_data_dir(tmp_path)
    assert await default_agent.create_the_assistant_once() is True
    assert (await get_agent("agent_synapse_ai"))["type"] == "builder"
    assert (await get_agent("agent_mine"))["name"] == "Mine"


def test_the_server_start_makes_it_after_the_import_and_the_old_defaults_turn():
    """The Docker path: the lifespan (too heavy to run here: it starts the MCP servers) calls it after the 1.x import,
    so an imported agent is the person's, and after adopt_the_assistant, so an old default turned is only marked."""
    import inspect

    import core.server as server

    src = inspect.getsource(server.lifespan)
    assert "create_the_assistant_once()" in src
    assert (src.index("import_legacy_data_if_present()") < src.index("adopt_the_assistant()")
            < src.index("create_the_assistant_once()"))


async def test_settings_do_not_show_the_marker(client):
    await default_agent.create_the_assistant_once()
    assert "default_agent_seeded" not in (await client.get("/api/settings")).json()
