"""Agent management routes (core/routes/agents.py): CRUD, active-agent, types."""
import pytest

from _fakes import seed as S


def _valid_agent_body(**over):
    body = {
        "id": over.pop("id", "agent_api_1"),
        "name": "API Agent",
        "description": "made via API",
        "type": "conversational",
        "tools": [],
        "system_prompt": "You are helpful.",
    }
    body.update(over)
    return body


class TestAgentsCrud:
    async def test_list_reflects_seeded_agents(self, client, seed_agent):
        a = seed_agent(name="Seeded One")
        resp = await client.get("/api/agents")
        assert resp.status_code == 200
        ids = [x["id"] for x in resp.json()]
        assert a["id"] in ids

    async def test_create_then_delete(self, client):
        resp = await client.post("/api/agents", json=_valid_agent_body(id="agent_create_me"))
        assert resp.status_code == 200

        resp = await client.get("/api/agents")
        assert "agent_create_me" in [x["id"] for x in resp.json()]

        resp = await client.delete("/api/agents/agent_create_me")
        assert resp.status_code == 200
        resp = await client.get("/api/agents")
        assert "agent_create_me" not in [x["id"] for x in resp.json()]

    async def test_create_invalid_body_is_422(self, client):
        # Missing required fields (name, description, tools, system_prompt).
        resp = await client.post("/api/agents", json={"id": "x"})
        assert resp.status_code == 422

    async def test_set_and_get_active_agent(self, client, seed_agent):
        a = seed_agent(id="agent_active_1", name="Active One")
        resp = await client.post("/api/agents/active", json={"agent_id": a["id"]})
        assert resp.status_code == 200
        resp = await client.get("/api/agents/active")
        assert resp.status_code == 200
        assert resp.json()["active_agent_id"] == a["id"]


class TestAgentTypes:
    async def test_agent_types_listed(self, client):
        resp = await client.get("/api/agent-types")
        assert resp.status_code == 200
        # Response is a list or dict of known agent types.
        payload = resp.json()
        assert payload  # non-empty
