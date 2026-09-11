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
        a = await seed_agent(name="Seeded One")
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
        a = await seed_agent(id="agent_active_1", name="Active One")
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


class TestToolPolicySurvivesASave:
    """The regression this screen exists for.

    `Agent` had no `tool_policy` field, so `create_agent`'s `agent.dict()`
    dropped it: the one documented way to reach the feature — editing the agent
    document by hand — was erased by the next save through the UI. Nothing
    failed; the policy simply stopped being there.
    """

    async def test_a_policy_round_trips(self, client):
        body = _valid_agent_body(id="agent_policy_1",
                                 tool_policy={"destructive": "confirm", "undeclared": "deny"})
        resp = await client.post("/api/agents", json=body)
        assert resp.status_code == 200
        assert resp.json()["tool_policy"] == {"destructive": "confirm", "undeclared": "deny"}

        listed = next(a for a in (await client.get("/api/agents")).json()
                      if a["id"] == "agent_policy_1")
        assert listed["tool_policy"] == {"destructive": "confirm", "undeclared": "deny"}

    async def test_junk_is_cleaned_on_the_way_in(self, client):
        """Stored clean rather than as sent: one of the readers of this document
        decides whether a chat widget may publish the agent."""
        resp = await client.post("/api/agents", json=_valid_agent_body(
            id="agent_policy_2", tool_policy={"open_world": "confirm", "wat": "deny",
                                              "destructive": "sometimes"}))
        assert resp.status_code == 200
        assert resp.json()["tool_policy"] == {"open_world": "confirm"}

    async def test_an_agent_that_says_nothing_stores_an_empty_policy(self, client):
        resp = await client.post("/api/agents", json=_valid_agent_body(id="agent_policy_3"))
        assert resp.json()["tool_policy"] == {}

    async def test_the_engine_reads_what_the_route_stored(self, client):
        """The route and `resolve_policy` have to agree about the shape, or the
        screen writes something the loop never acts on."""
        from core.tool_policy import resolve_policy

        await client.post("/api/agents", json=_valid_agent_body(
            id="agent_policy_4", tool_policy={"destructive": "deny"}))
        stored = next(a for a in (await client.get("/api/agents")).json()
                      if a["id"] == "agent_policy_4")
        assert resolve_policy(stored, None)["destructive"] == "deny"
